"""专有名词 / 替换词典：解析、应用与提示词构建。"""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from bili import config as cfg
from bili.config import Settings
from bili.glossary import (GlossaryError, Rule, apply_rules, apply_rules_to_segments,
                           build_asr_prompt, build_glossary_hint, format_rules,
                           format_terms, fuzzy_fix_segments, fuzzy_fix_terms,
                           merge_rules, parse_rules, parse_terms, rules_from_settings,
                           term_matches, terms_from_settings)


class ParseRulesTest(unittest.TestCase):
    def test_literal_and_regex(self):
        rules = parse_rules("比西 => B站\n/昇腾|升腾/ => 昇腾")
        self.assertEqual(rules[0], Rule("比西", "B站", False))
        self.assertEqual(rules[1], Rule("昇腾|升腾", "昇腾", True))

    def test_arrow_variants(self):
        rules = parse_rules("甲乙 -> 甲\n丙丁 → 丙")
        self.assertEqual([r.src for r in rules], ["甲乙", "丙丁"])

    def test_comments_and_blank_lines_ignored(self):
        rules = parse_rules("# 注释\n\n比西 => B站\n   \n")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].src, "比西")

    def test_missing_separator_reports_line(self):
        with self.assertRaises(GlossaryError) as ctx:
            parse_rules("比西 => B站\n这行没有分隔符")
        self.assertIn("第 2 行", str(ctx.exception))

    def test_empty_source_rejected(self):
        with self.assertRaises(GlossaryError):
            parse_rules("  => B站")

    def test_invalid_regex_reports_line(self):
        with self.assertRaises(GlossaryError) as ctx:
            parse_rules("a => b\n/(/ => x")
        self.assertIn("第 2 行", str(ctx.exception))

    def test_format_roundtrip(self):
        text = "比西 => B站\n/昇腾|升腾/ => 昇腾"
        self.assertEqual(format_rules(parse_rules(text)), text)

    def test_empty_text(self):
        self.assertEqual(parse_rules(""), [])


class ParseTermsTest(unittest.TestCase):
    def test_dedupe_keeps_order(self):
        self.assertEqual(parse_terms("B站\n昇腾\nB站\n\n# 注释\nTransformer"),
                         ["B站", "昇腾", "Transformer"])

    def test_format_terms(self):
        self.assertEqual(format_terms(["a", "b"]), "a\nb")


class ApplyRulesTest(unittest.TestCase):
    def test_literal(self):
        rules = [Rule("比西", "B站")]
        self.assertEqual(apply_rules("我在比西看视频", rules), "我在B站看视频")

    def test_literal_is_not_regex(self):
        rules = [Rule("1+1", "二")]
        self.assertEqual(apply_rules("1+1=2", rules), "二=2")

    def test_regex_with_backreference(self):
        rules = [Rule(r"(\d+)块", r"\1 元", True)]
        self.assertEqual(apply_rules("花了 30块", rules), "花了 30 元")

    def test_declared_order_wins(self):
        # 先写的规则先执行：第二条的输入依赖第一条的输出
        rules = [Rule("甲", "乙"), Rule("乙", "丙")]
        self.assertEqual(apply_rules("甲", rules), "丙")

    def test_empty_rules_is_identity(self):
        self.assertEqual(apply_rules("原文", []), "原文")

    def test_segments_keep_timestamps_and_drop_emptied(self):
        segments = [{"start": 0.0, "end": 1.5, "text": "比西真好看"},
                    {"start": 1.5, "end": 2.0, "text": "嗯"},
                    {"start": 2.0, "end": 3.0, "text": "好的"}]
        out = apply_rules_to_segments(segments, [Rule("比西", "B站"), Rule("嗯", "")])
        self.assertEqual([s["text"] for s in out], ["B站真好看", "好的"])
        self.assertEqual((out[0]["start"], out[0]["end"]), (0.0, 1.5))

    def test_segments_not_mutated(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "比西"}]
        apply_rules_to_segments(segments, [Rule("比西", "B站")])
        self.assertEqual(segments[0]["text"], "比西")


class SettingsCoercionTest(unittest.TestCase):
    def test_dirty_rules_are_skipped(self):
        rules = rules_from_settings([
            {"from": "比西", "to": "B站"},
            {"from": "(", "to": "x", "regex": True},   # 非法正则
            {"to": "缺左值"},
            "垃圾",
            ["甲乙", "甲"],
        ])
        self.assertEqual([(r.src, r.dst, r.regex) for r in rules],
                         [("比西", "B站", False), ("甲乙", "甲", False)])

    def test_non_list_input(self):
        self.assertEqual(rules_from_settings(None), [])
        self.assertEqual(rules_from_settings("比西 => B站"), [])

    def test_terms_from_string(self):
        self.assertEqual(terms_from_settings("B站\n昇腾"), ["B站", "昇腾"])

    def test_settings_roundtrip(self):
        s = Settings()
        s.glossary_terms = ["B站", "昇腾"]
        s.replace_rules = [{"from": "比西", "to": "B站", "regex": False}]
        again = Settings(**asdict(s))
        self.assertEqual(again.glossary_terms, ["B站", "昇腾"])
        self.assertEqual(again.replace_rules, s.replace_rules)


class SettingsPersistenceTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._patch = mock.patch.object(cfg, "app_dir", lambda: self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_save_and_load_roundtrip(self):
        s = Settings()
        s.glossary_terms = ["B站", "昇腾"]
        s.replace_rules = [{"from": "比西", "to": "B站", "regex": False},
                           {"from": r"(\d+)块", "to": r"\1 元", "regex": True}]
        cfg.save_settings(s)
        again = cfg.load_settings()
        self.assertEqual(again.glossary_terms, ["B站", "昇腾"])
        self.assertEqual(again.replace_rules, s.replace_rules)
        self.assertEqual(rules_from_settings(again.replace_rules),
                         [Rule("比西", "B站", False), Rule(r"(\d+)块", r"\1 元", True)])

    def test_hand_edited_dirty_values_are_normalized(self):
        data = asdict(Settings())
        data["glossary_terms"] = "B站\nB站\n\n昇腾"
        data["replace_rules"] = "不是列表"
        (self.tmp / "config.json").write_text(json.dumps(data, ensure_ascii=False),
                                              encoding="utf-8")
        s = cfg.load_settings()
        self.assertEqual(s.glossary_terms, ["B站", "昇腾"])
        self.assertEqual(s.replace_rules, [])


class MergeRulesTest(unittest.TestCase):
    def test_appends_new_and_skips_duplicates(self):
        current = [Rule("比西", "B站")]
        incoming = [Rule("比西", "B站"), Rule("比西", "bilibili"),
                    Rule("昇腾", "昇腾"), Rule("比西", "B站", True)]
        merged = merge_rules(current, incoming)
        self.assertEqual([(r.src, r.dst, r.regex) for r in merged],
                         [("比西", "B站", False), ("昇腾", "昇腾", False),
                          ("比西", "B站", True)])

    def test_order_preserved(self):
        merged = merge_rules([Rule("a", "1")], [Rule("b", "2"), Rule("c", "3")])
        self.assertEqual([r.src for r in merged], ["a", "b", "c"])


class PromptTest(unittest.TestCase):
    def test_asr_prompt_empty_without_terms(self):
        self.assertEqual(build_asr_prompt([]), "")
        self.assertEqual(build_glossary_hint([""]), "")

    def test_asr_prompt_contains_terms(self):
        prompt = build_asr_prompt(["B站", "昇腾"])
        self.assertIn("B站、昇腾", prompt)

    def test_asr_prompt_respects_limit(self):
        terms = [f"术语{i:03d}" for i in range(200)]
        prompt = build_asr_prompt(terms, limit=40)
        self.assertLessEqual(len(prompt) - len("本视频会提到这些专有名词：。"), 40 + 12)
        self.assertIn("术语000", prompt)
        self.assertNotIn("术语199", prompt)

    def test_glossary_hint_mentions_keep_as_is(self):
        hint = build_glossary_hint(["Transformer"])
        self.assertIn("Transformer", hint)
        self.assertIn("不要改写成同音字", hint)


class ApplyRulesIdempotencyTest(unittest.TestCase):
    """正则规则只在校对前跑一次：校对后重复应用不再安全。"""

    def test_regex_skipped_when_include_regex_false(self):
        rules = [Rule("比西", "B站"), Rule(r"(\d+)块", r"\1 元", True)]
        out = apply_rules("比西 30块", rules, include_regex=False)
        self.assertEqual(out, "B站 30块")

    def test_literal_rules_still_apply(self):
        rules = [Rule("比西", "B站")]
        self.assertEqual(apply_rules("比西", rules, include_regex=False), "B站")


class FuzzyTermTest(unittest.TestCase):
    """SenseVoice 无热词：用拼音同音同调兜底把错写改回术语。"""

    def test_same_sound_same_tone_replaced(self):
        self.assertEqual(fuzzy_fix_terms("我们用的是升腾的芯片", ["昇腾"]), "我们用的是昇腾的芯片")

    def test_different_tone_not_replaced(self):
        # 省(sheng3) 与 昇(sheng1) 不同调，不能乱改
        self.assertEqual(fuzzy_fix_terms("我们用的是省腾的芯片", ["昇腾"]), "我们用的是省腾的芯片")

    def test_correct_term_untouched(self):
        self.assertEqual(fuzzy_fix_terms("昇腾很好", ["昇腾"]), "昇腾很好")

    def test_two_char_difference_untouched(self):
        self.assertEqual(fuzzy_fix_terms("我们用的是高腾", ["昇腾"]), "我们用的是高腾")

    def test_single_char_term_ignored(self):
        self.assertEqual(fuzzy_fix_terms("甲乙", ["甲"]), "甲乙")

    def test_multiple_occurrences_all_fixed(self):
        self.assertEqual(fuzzy_fix_terms("升腾和升腾", ["昇腾"]), "昇腾和昇腾")

    def test_matches_requires_single_char_diff(self):
        self.assertFalse(term_matches("昇腾", "昇腾"))
        self.assertFalse(term_matches("高腾", "昇腾"))
        self.assertTrue(term_matches("升腾", "昇腾"))

    def test_segments_keep_timestamps(self):
        segs = [{"start": 0.0, "end": 1.0, "text": "升腾发布会"}]
        out = fuzzy_fix_segments(segs, ["昇腾"])
        self.assertEqual(out[0]["text"], "昇腾发布会")
        self.assertEqual((out[0]["start"], out[0]["end"]), (0.0, 1.0))
        self.assertEqual(segs[0]["text"], "升腾发布会")   # 不改原对象

    def test_no_terms_is_identity(self):
        self.assertEqual(fuzzy_fix_terms("原文", []), "原文")
        self.assertEqual(fuzzy_fix_segments([{"start": 0, "end": 1, "text": "原文"}], []),
                         [{"start": 0, "end": 1, "text": "原文"}])


if __name__ == "__main__":
    unittest.main()
