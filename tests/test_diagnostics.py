"""诊断信息：#5 失败现场、阶段耗时、日志尾部。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili import config as cfg
from bili.config import Settings
from bili.gui.diagnostics import build_report, _fmt_duration
from bili.models import Job
from bili.pipeline import VideoResult


class ReportTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        p = mock.patch.object(cfg, "data_dir", lambda: self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def _write_log(self, lines: int) -> None:
        d = self.tmp / "logs"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.log").write_text(
            "\n".join(f"line {i}" for i in range(1, lines + 1)) + "\n", encoding="utf-8")

    def test_failed_job_shows_full_error_and_log_tail(self):
        self._write_log(120)
        err = "大模型调用失败（qwen2.5:7b）：请确认 Ollama 已启动" + "，很长的原因" * 20
        job = Job(url="https://www.bilibili.com/video/BV1xx411c7mD", title="某视频",
                  status="失败", error=err)
        report = build_report(job, Settings(), log_lines=50)
        self.assertIn("BV1xx411c7mD", report)
        self.assertIn(err, report)               # 错误原文不截断
        self.assertIn("== 环境 ==", report)
        self.assertIn("SenseVoice", report)
        self.assertIn("line 120", report)        # 日志尾部
        self.assertNotIn("line 60", report)      # 只取最后 50 行

    def test_result_shows_timings_and_counts(self):
        job = Job(url="https://www.bilibili.com/video/BV1xx411c7mD", status="完成")
        job.result = VideoResult(
            info={"bvid": "BV1xx411c7mD", "title": "t"},
            segments=[{"start": 0, "end": 1, "text": "x"}] * 3,
            proofread="正文" * 100,
            chapters=[{"title": "一", "text": "正文"}],
            summary="- 要点",
            source="asr",
            timings={"获取文本": 61.2, "AI 校对": 12.0},
        )
        report = build_report(job, Settings(), log_lines=5)
        self.assertIn("阶段耗时：获取文本 1m01s · AI 校对 12.0s", report)
        self.assertIn("来源：语音转写", report)
        self.assertIn("章节：1", report)
        self.assertIn("（暂无日志）", report)

    def test_glossary_counts_and_provider(self):
        s = Settings(llm_provider="api", api_base="https://x/v1", api_model="m")
        s.replace_rules = [{"from": "a", "to": "b"}]
        s.glossary_terms = ["B站", "昇腾"]
        report = build_report(Job(url="u", status="失败"), s, log_lines=1)
        self.assertIn("LLM：api · m", report)
        self.assertIn("替换词典：1 条", report)
        self.assertIn("专有名词：2 个", report)
        self.assertIn("精校：未配置", report)


class LogTailTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        p = mock.patch.object(cfg, "data_dir", lambda: self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def test_missing_log(self):
        self.assertEqual(cfg.read_log_tail(10), "")

    def test_returns_last_lines_only(self):
        d = self.tmp / "logs"
        d.mkdir(parents=True)
        (d / "app.log").write_text("\n".join(str(i) for i in range(500)), encoding="utf-8")
        tail = cfg.read_log_tail(5).splitlines()
        self.assertEqual(tail, ["495", "496", "497", "498", "499"])

    def test_reads_only_tail_bytes(self):
        d = self.tmp / "logs"
        d.mkdir(parents=True)
        (d / "app.log").write_text("旧" * 100_000 + "\n最后一行", encoding="utf-8")
        tail = cfg.read_log_tail(3, max_bytes=1000)
        self.assertIn("最后一行", tail)
        self.assertLess(len(tail), 2000)


class DurationTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(_fmt_duration(9.4), "9.4s")
        self.assertEqual(_fmt_duration(61.2), "1m01s")
        self.assertEqual(_fmt_duration("bad"), "?")


if __name__ == "__main__":
    unittest.main()
