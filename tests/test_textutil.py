"""textutil 纯函数测试。

重点覆盖 #1：章节只覆盖开头时，复制 Markdown / 导出 .md 不得静默丢正文。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili.textutil import (build_export, build_markdown, ensure_chapters_cover,
                           fmt_ts, is_heading_line, merge_segments, slice_by_anchors,
                           split_chunks)

HEAD = "开场部分。" * 60          # 300 字
TAIL = "结尾部分内容。" * 60      # 420 字
FULL = HEAD + "\n\n" + TAIL
PARTIAL_CHAPTERS = [{"title": "开场", "text": HEAD}]
UNMATCHED_CHAPTERS = [{"title": "对不上", "text": "完全另一段文字。" * 40}]


class EnsureChaptersCoverTest(unittest.TestCase):
    def test_full_coverage_returns_chapters_unchanged(self):
        ch = [{"title": "全文", "text": FULL}]
        self.assertEqual(ensure_chapters_cover(FULL, ch), ch)

    def test_no_chapters_returns_none(self):
        self.assertIsNone(ensure_chapters_cover(FULL, None))
        self.assertIsNone(ensure_chapters_cover(FULL, []))

    def test_empty_proofread_returns_chapters(self):
        self.assertEqual(ensure_chapters_cover("", PARTIAL_CHAPTERS), PARTIAL_CHAPTERS)

    def test_short_chapters_get_uncovered_tail(self):
        out = ensure_chapters_cover(FULL, PARTIAL_CHAPTERS)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 2)
        self.assertIn("未分段", out[-1]["title"])
        self.assertIn("结尾部分内容。", out[-1]["text"])

    def test_unlocatable_anchor_returns_none(self):
        # 定位不到尾部时宁可丢章节标题也不能丢正文 -> 返回 None 让调用方回退全文
        self.assertIsNone(ensure_chapters_cover(FULL, UNMATCHED_CHAPTERS))


class SliceByAnchorsTest(unittest.TestCase):
    """#1 根治：模型只给锚点，正文由程序切分，覆盖率 100%。"""

    DOC = ("开场部分内容一。\n\n中间章节的开头二。\n\n结尾部分内容三。")

    def _joined(self, chapters):
        return "".join(c["text"] for c in chapters).replace("\n", "").replace(" ", "")

    def test_slices_cover_whole_doc(self):
        chapters = slice_by_anchors(self.DOC, [("第一章", "开场部分内容一"),
                                               ("第二章", "中间章节的开头二"),
                                               ("第三章", "结尾部分内容三")])
        self.assertEqual(len(chapters), 3)
        self.assertEqual([c["title"] for c in chapters], ["第一章", "第二章", "第三章"])
        self.assertEqual(self._joined(chapters), self.DOC.replace("\n", "").replace(" ", ""))
        self.assertTrue(chapters[0]["text"].startswith("开场部分内容一"))
        self.assertTrue(chapters[-1]["text"].endswith("结尾部分内容三。"))

    def test_first_chapter_always_starts_at_zero(self):
        # 模型漏掉开头时，前面那段归入第一章而不是丢弃
        chapters = slice_by_anchors(self.DOC, [("第一章", "中间章节的开头二"),
                                               ("第二章", "结尾部分内容三")])
        self.assertTrue(chapters[0]["text"].startswith("开场部分内容一"))
        self.assertEqual(len(chapters), 2)

    def test_unlocatable_anchor_drops_its_own_title(self):
        chapters = slice_by_anchors(self.DOC, [("第一章", "开场部分内容一"),
                                               ("找不到", "原文里根本没有这句"),
                                               ("第三章", "结尾部分内容三")])
        self.assertEqual([c["title"] for c in chapters], ["第一章", "第三章"])
        self.assertIn("中间章节的开头二", chapters[0]["text"])

    def test_too_few_chapters_returns_empty(self):
        self.assertEqual(slice_by_anchors(self.DOC, [("唯一", "开场部分内容一")]), [])
        self.assertEqual(slice_by_anchors(self.DOC, []), [])
        self.assertEqual(slice_by_anchors("", [("章", "x")]), [])

    def test_anchor_with_extra_punctuation_still_matches(self):
        chapters = slice_by_anchors(self.DOC, [("一", "开场部分内容一。"),
                                               ("二", "中间章节的开头，二")])
        self.assertEqual(len(chapters), 2)

    def test_anchor_shortened_when_model_adds_words(self):
        chapters = slice_by_anchors(self.DOC, [("一", "开场部分内容一。这里有模型加的废话"),
                                               ("二", "中间章节的开头二")])
        self.assertEqual(len(chapters), 2)


class BuildMarkdownTest(unittest.TestCase):
    def test_keeps_head_and_tail(self):
        md = build_markdown("测试标题", FULL, PARTIAL_CHAPTERS)
        self.assertIn("# 测试标题", md)
        self.assertIn("## 开场", md)
        self.assertIn("开场部分。", md)
        self.assertIn("结尾部分内容。", md)

    def test_falls_back_to_full_text(self):
        md = build_markdown("测试标题", FULL, UNMATCHED_CHAPTERS)
        self.assertIn("开场部分。", md)
        self.assertIn("结尾部分内容。", md)

    def test_no_chapters_uses_plain_proofread(self):
        md = build_markdown("标题", "只有一段正文。", None)
        self.assertIn("只有一段正文。", md)


class _Settings:
    export_summary = True
    export_raw = True
    export_timestamps = True


INFO = {"bvid": "BV1xx411c7mD", "title": "测试视频", "owner": "UP主", "duration": 3725}


class BuildExportTest(unittest.TestCase):
    def test_sections_and_tail(self):
        out = build_export(INFO, FULL, PARTIAL_CHAPTERS,
                           [{"start": 3725, "end": 3730, "text": "最后一句"}], "总结内容", _Settings())
        self.assertIn("- 来源：https://www.bilibili.com/video/BV1xx411c7mD", out)
        self.assertIn("- 时长：01:02:05", out)
        self.assertIn("## AI 总结", out)
        self.assertIn("## 原文稿", out)
        self.assertIn("## 原片跳转时间戳", out)
        self.assertIn("- [01:02:05] 最后一句", out)
        self.assertIn("结尾部分内容。", out)

    def test_flags_off_omit_sections(self):
        class S:
            export_summary = False
            export_raw = True
            export_timestamps = False

        out = build_export(INFO, FULL, None, [], "不该出现", S())
        self.assertNotIn("AI 总结", out)
        self.assertNotIn("原片跳转时间戳", out)
        self.assertIn("## 原文稿", out)


class SplitChunksTest(unittest.TestCase):
    def test_lossless_and_within_limit(self):
        text = "\n\n".join(f"第{i}段" + "内容" * 30 for i in range(20))
        chunks = split_chunks(text, 200)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertEqual("\n\n".join(chunks), text)

    def test_single_overlong_paragraph_kept_whole(self):
        para = "很长" * 300
        self.assertEqual(split_chunks(para, 100), [para])

    def test_empty_text(self):
        self.assertEqual(split_chunks("", 100), [])
        self.assertEqual(split_chunks("   \n\n  ", 100), [])


class MergeSegmentsTest(unittest.TestCase):
    def test_merges_small_gaps_only(self):
        segs = [{"start": 0, "end": 1, "text": "甲"},
                {"start": 2, "end": 3, "text": "乙"},
                {"start": 10, "end": 11, "text": "丙"}]
        out = merge_segments(segs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["text"], "甲乙")
        self.assertEqual(out[0]["end"], 3)
        self.assertEqual(out[1]["text"], "丙")

    def test_empty(self):
        self.assertEqual(merge_segments([]), [])


class MiscTest(unittest.TestCase):
    def test_fmt_ts(self):
        self.assertEqual(fmt_ts(0), "00:00")
        self.assertEqual(fmt_ts(65), "01:05")
        self.assertEqual(fmt_ts(3725), "01:02:05")
        self.assertEqual(fmt_ts(-5), "00:00")

    def test_is_heading_line(self):
        self.assertTrue(is_heading_line("第一章 开场"))
        self.assertTrue(is_heading_line("第三部分"))
        self.assertTrue(is_heading_line("Part 2 内容"))
        self.assertFalse(is_heading_line("这就是正文。"))


if __name__ == "__main__":
    unittest.main()
