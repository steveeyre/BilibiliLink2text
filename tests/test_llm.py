"""LLM 层测试：分块校对、精校、总结 map-reduce、章节锚点切分。

全部用假 client 注入，不联网、不依赖 Ollama。锁住 P0 修复（#1 章节丢正文、#3 总结只覆盖开头）。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili import llm
from bili.config import Settings
from bili.textutil import split_chunks

PARA = "这是第{n}段正文，用来验证分块与章节切分是否完整保留全部内容。"


def make_text(n: int) -> str:
    return "\n\n".join(PARA.format(n=i) for i in range(n))


class _FakeStream:
    """记录 _chat_stream_client / _chat_stream 的调用参数。"""

    def __init__(self, reply=None):
        self.calls: list[dict] = []
        self.reply = reply or (lambda user, i: f"输出{i}")

    def client_side(self, client, model, system, user,
                    max_tokens: int = 4096, on_delta=None, cancel=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if on_delta:
            out = self.reply(user, len(self.calls) - 1)
            on_delta(out)
            return out
        return self.reply(user, len(self.calls) - 1)

    def stream_side(self, settings, system, user, max_tokens: int = 4096,
                    on_delta=None, cancel=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        out = self.reply(user, len(self.calls) - 1)
        if on_delta:
            on_delta(out)
        return out


class ProofreadTest(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeStream()
        patches = [mock.patch.object(llm, "_client", lambda s: "CLIENT"),
                   mock.patch.object(llm, "_chat_stream_client", self.fake.client_side)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_chunks_are_sent_separately_then_joined(self):
        text = make_text(60)
        chunks = split_chunks(text, llm.CHUNK_LIMIT)
        self.assertGreater(len(chunks), 1)
        out = llm.proofread(Settings(), text)
        self.assertEqual(len(self.fake.calls), len(chunks))
        self.assertEqual([c["user"] for c in self.fake.calls], chunks)
        self.assertEqual(out, "\n\n".join(f"输出{i}" for i in range(len(chunks))))

    def test_empty_reply_falls_back_to_original_chunk(self):
        self.fake.reply = lambda user, i: ""
        text = make_text(3)
        self.assertEqual(llm.proofread(Settings(), text), text)

    def test_glossary_hint_goes_into_system_prompt(self):
        s = Settings()
        s.glossary_terms = ["昇腾", "B站"]
        llm.proofread(s, make_text(2))
        system = self.fake.calls[0]["system"]
        self.assertTrue(system.startswith(llm.PROOF_SYSTEM))
        self.assertIn("昇腾、B站", system)

    def test_no_terms_keeps_system_prompt_clean(self):
        llm.proofread(Settings(), make_text(2))
        self.assertEqual(self.fake.calls[0]["system"], llm.PROOF_SYSTEM)

    def test_cancel_before_start(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(llm.CancelledError):
            llm.proofread(Settings(), make_text(2), cancel=ev)

    def test_progress_reaches_one(self):
        seen: list[float] = []
        llm.proofread(Settings(), make_text(8), progress=seen.append)
        self.assertAlmostEqual(seen[-1], 1.0)


class FineProofreadTest(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeStream(reply=lambda user, i: "精校结果")
        p = mock.patch.object(llm, "_chat_stream_client", self.fake.client_side)
        p.start()
        self.addCleanup(p.stop)

    def test_unconfigured_raises(self):
        with self.assertRaises(llm.LLMError):
            llm.fineproofread(Settings(), make_text(2))

    def test_uses_fine_endpoint_and_glossary(self):
        s = Settings(fine_base="https://api.example.com/v1", fine_model="m", fine_key="k")
        s.glossary_terms = ["昇腾"]
        captured = {}

        class FakeOpenAI:
            def __init__(self, base_url=None, api_key=None, timeout=None):
                captured.update(base_url=base_url, api_key=api_key)

        with mock.patch.object(llm, "OpenAI", FakeOpenAI):
            out = llm.fineproofread(s, make_text(2))
        self.assertEqual(out, "精校结果")
        self.assertEqual(captured["base_url"], "https://api.example.com/v1")
        self.assertIn("昇腾", self.fake.calls[0]["system"])


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeStream(reply=lambda user, i: f"- 要点{i}")
        p = mock.patch.object(llm, "_chat_stream", self.fake.stream_side)
        p.start()
        self.addCleanup(p.stop)

    def test_off_returns_none_without_call(self):
        self.assertIsNone(llm.summarize(Settings(), "正文", "off"))
        self.assertEqual(self.fake.calls, [])

    def test_short_text_single_call_with_full_text(self):
        text = make_text(3)
        out = llm.summarize(Settings(), text, "bullets")
        self.assertEqual(out, "- 要点0")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIn(text, self.fake.calls[0]["user"])

    def test_long_text_uses_map_reduce_over_whole_text(self):
        text = make_text(1200)          # 远超 SUMMARY_INPUT_LIMIT
        self.assertGreater(len(text), llm.SUMMARY_INPUT_LIMIT)
        chunks = split_chunks(text, llm.SUMMARY_INPUT_LIMIT)
        out = llm.summarize(Settings(), text, "bullets")
        self.assertEqual(len(self.fake.calls), len(chunks) + 1)
        # 每个分块都真的送进去了，不是只总结开头
        for i, chunk in enumerate(chunks):
            self.assertIn(chunk, self.fake.calls[i]["user"])
        last_user = self.fake.calls[-1]["user"]
        self.assertIn("合并", last_user)
        self.assertTrue(out.startswith("（注：全文较长"))

    def test_cancel_checked_between_chunks(self):
        ev = threading.Event()

        def reply(user, i):
            ev.set()                    # 第一个分块结束后就取消
            return "- x"

        self.fake.reply = reply
        with self.assertRaises(llm.CancelledError):
            llm.summarize(Settings(), make_text(1200), "bullets", cancel=ev)


class ChapterizeTest(unittest.TestCase):
    DOC = ("开场部分内容一，讲的是背景。\n\n"
           "中间章节的开头二，讲的是方法。\n\n"
           "结尾部分内容三，讲的是结论。\n\n" + "补充说明。" * 700)   # 超过 CHAPTER_MIN_LEN

    def setUp(self):
        self.fake = _FakeStream()
        p = mock.patch.object(llm, "_chat_stream", self.fake.stream_side)
        p.start()
        self.addCleanup(p.stop)

    def test_short_text_skips_llm(self):
        self.assertIsNone(llm.chapterize(Settings(), "太短了"))
        self.assertEqual(self.fake.calls, [])

    def test_anchor_output_slices_full_text(self):
        self.fake.reply = lambda user, i: (
            "### 背景\n@ 开场部分内容一，讲的是背景\n"
            "### 方法\n@ 中间章节的开头二，讲的是方法\n"
            "### 结论\n@ 结尾部分内容三，讲的是结论\n")
        chapters = llm.chapterize(Settings(), self.DOC)
        self.assertEqual([c["title"] for c in chapters], ["背景", "方法", "结论"])
        joined = "".join(c["text"] for c in chapters).replace("\n", "").replace(" ", "")
        self.assertEqual(joined, self.DOC.replace("\n", "").replace(" ", ""))

    def test_echo_format_falls_back(self):
        self.fake.reply = lambda user, i: "### 第一章\n回显正文一段。\n### 第二章\n回显正文二段。"
        chapters = llm.chapterize(Settings(), self.DOC)
        self.assertEqual([c["title"] for c in chapters], ["第一章", "第二章"])
        self.assertEqual(chapters[0]["text"], "回显正文一段。")

    def test_unparsable_output_returns_none(self):
        self.fake.reply = lambda user, i: "我无法完成这个任务。"
        self.assertIsNone(llm.chapterize(Settings(), self.DOC))

    def test_max_tokens_is_bounded(self):
        self.fake.reply = lambda user, i: "### 一\n@ 开场部分内容一\n### 二\n@ 中间章节的开头二"
        llm.chapterize(Settings(), self.DOC)
        self.assertLessEqual(self.fake.calls[0]["max_tokens"], llm.CHAPTER_MAX_TOKENS)
        self.assertLessEqual(llm.CHAPTER_MAX_TOKENS, 4096)


class ParallelProofreadTest(unittest.TestCase):
    """#6：云端 API 分块并发，本地保持串行。"""

    def setUp(self):
        self.lock = threading.Lock()
        self.calls: list[str] = []

        def fake(client, model, system, user, max_tokens=4096, on_delta=None, cancel=None):
            with self.lock:
                self.calls.append(user)
            return f"OUT[{user[:6]}]"

        patches = [mock.patch.object(llm, "_client", lambda s: "CLIENT"),
                   mock.patch.object(llm, "_chat_stream_client", fake)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def api_settings() -> Settings:
        return Settings(llm_provider="api", api_base="https://api.example.com/v1",
                        api_model="m", api_key="k")

    def test_worker_count_by_provider(self):
        self.assertEqual(llm._proofread_workers(self.api_settings()), llm.PROOFREAD_MAX_WORKERS)
        self.assertEqual(llm._proofread_workers(Settings(llm_provider="ollama")), 1)
        self.assertEqual(llm._proofread_workers(Settings(llm_provider="llamacpp")), 1)

    def test_all_chunks_sent_and_order_preserved(self):
        text = make_text(300)
        chunks = split_chunks(text, llm.CHUNK_LIMIT)
        self.assertGreater(len(chunks), 3)
        out = llm.proofread(self.api_settings(), text)
        self.assertEqual(sorted(self.calls), sorted(chunks))
        self.assertEqual(out, "\n\n".join(f"OUT[{c[:6]}]" for c in chunks))

    def test_progress_is_monotonic_and_complete(self):
        seen: list[float] = []
        llm.proofread(self.api_settings(), make_text(300), progress=seen.append)
        self.assertGreater(len(seen), 1)
        self.assertEqual(seen, sorted(seen))
        self.assertAlmostEqual(seen[-1], 1.0)

    def test_status_reports_completed_chunks(self):
        seen: list[str] = []
        llm.proofread(self.api_settings(), make_text(300), on_status=seen.append)
        self.assertTrue(any("已完成" in s for s in seen), seen)

    def test_cancel_stops_parallel_path(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(llm.CancelledError):
            llm.proofread(self.api_settings(), make_text(60), cancel=ev)


class RulesAfterProofreadTest(unittest.TestCase):
    """#4：校对后再兜一次替换词典，防止模型把术语改回同音字。"""

    def setUp(self):
        self.fake = _FakeStream(reply=lambda user, i: "模型把它写成了比西，还有昇腾。")
        p = mock.patch.object(llm, "_chat_stream_client", self.fake.client_side)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(llm, "_client", lambda s: "CLIENT")
        p2.start()
        self.addCleanup(p2.stop)

    def test_literal_rule_applied_to_proofread_output(self):
        s = Settings()
        s.replace_rules = [{"from": "比西", "to": "B站", "regex": False}]
        self.assertEqual(llm.proofread(s, make_text(2)), "模型把它写成了B站，还有昇腾。")

    def test_regex_rule_not_reapplied_after_proofread(self):
        # 正则规则无法保证幂等，只在校对前跑一次；校对后只补字面规则
        s = Settings()
        s.replace_rules = [{"from": "比西|昇腾", "to": "B站", "regex": True}]
        self.assertEqual(llm.proofread(s, make_text(2)), "模型把它写成了比西，还有昇腾。")

    def test_no_rules_is_identity(self):
        self.assertEqual(llm.proofread(Settings(), make_text(2)),
                         "模型把它写成了比西，还有昇腾。")

    def test_fineproofread_applies_rules_too(self):
        s = Settings(fine_base="https://api.example.com/v1", fine_model="m", fine_key="k")
        s.replace_rules = [{"from": "比西", "to": "B站", "regex": False}]
        with mock.patch.object(llm, "OpenAI", lambda **kw: object()):
            self.assertEqual(llm.fineproofread(s, make_text(2)),
                             "模型把它写成了B站，还有昇腾。")


class MultiWindowChapterizeTest(unittest.TestCase):
    """章节不再只覆盖前 12000 字：按窗口多次取锚点，尾部也细分。"""

    def setUp(self):
        self.calls: list[str] = []
        self.doc = "".join(f"第{i:04d}段内容，讲一些与主题相关的事情。" for i in range(2000))

    def _fake(self, settings, system, user, max_tokens=4096, on_delta=None, cancel=None):
        self.calls.append(user)
        first = user[:14]
        second = user[-40:-20]
        return (f"### A{len(self.calls)}\n@ {first}\n"
                f"### B{len(self.calls)}\n@ {second}")

    def test_every_window_is_used_and_tail_is_chaptered(self):
        windows = (len(self.doc) + llm.CHAPTER_INPUT_LIMIT - 1) // llm.CHAPTER_INPUT_LIMIT
        self.assertGreaterEqual(windows, 3)
        with mock.patch.object(llm, "_chat_stream", self._fake):
            chapters = llm.chapterize(Settings(), self.doc)
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertLessEqual(len(self.calls), windows)
        self.assertGreater(len(chapters), len(self.calls))
        joined = "".join(c["text"] for c in chapters).replace("\n", "").replace(" ", "")
        self.assertEqual(joined, self.doc.replace("\n", "").replace(" ", ""))
        self.assertIn("第1999段", chapters[-1]["text"])

    def test_stops_after_first_window_when_format_ignored(self):
        calls: list[str] = []

        def fake(settings, system, user, max_tokens=4096, on_delta=None, cancel=None):
            calls.append(user)
            return "### 标题\n正文回显"

        with mock.patch.object(llm, "_chat_stream", fake):
            chapters = llm.chapterize(Settings(), self.doc)
        self.assertEqual(len(calls), 1)
        self.assertEqual(chapters, [{"title": "标题", "text": "正文回显"}])


class ParseTest(unittest.TestCase):
    def test_anchor_parser_needs_title_before_anchor(self):
        self.assertEqual(llm.parse_anchor_chapters("@ 孤立锚点"), [])
        self.assertEqual(llm.parse_anchor_chapters("# 标题\n@ 锚点\n@ 第二个"), [("标题", "锚点")])

    def test_anchor_parser_accepts_fullwidth_at(self):
        self.assertEqual(llm.parse_anchor_chapters("### 标题\n＠ 锚点"), [("标题", "锚点")])

    def test_anchor_parser_drops_trailing_title(self):
        self.assertEqual(llm.parse_anchor_chapters("### 标题\n@ 锚点\n### 只有标题"),
                         [("标题", "锚点")])

    def test_echo_parser_requires_body(self):
        self.assertIsNone(llm.parse_echo_chapters("### 只有标题"))
        self.assertIsNone(llm.parse_echo_chapters(""))


if __name__ == "__main__":
    unittest.main()
