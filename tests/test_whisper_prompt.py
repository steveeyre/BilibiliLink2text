"""whisper.cpp 专有名词提示：参数探测与初始化。"""
import unittest

from bili.asr_whispercpp import WhisperCppASR, _hint_supported


class HintSupportTest(unittest.TestCase):
    def test_new_cli_supports_carry_prompt(self):
        help_text = "usage: whisper-cli [options]\n  --carry-initial-prompt  carry prompt\n"
        self.assertTrue(_hint_supported(help_text))

    def test_old_cli_or_empty_help(self):
        self.assertFalse(_hint_supported(""))
        self.assertFalse(_hint_supported("--prompt PROMPT  initial prompt"))


class PromptInitTest(unittest.TestCase):
    def test_prompt_is_stripped(self):
        asr = WhisperCppASR(model_size="small", prompt="  术语A、术语B ")
        self.assertEqual(asr.prompt, "术语A、术语B")

    def test_no_prompt_by_default(self):
        self.assertEqual(WhisperCppASR().prompt, "")

    def test_unknown_model_size_falls_back(self):
        self.assertEqual(WhisperCppASR(model_size="huge").model_size, "medium")


if __name__ == "__main__":
    unittest.main()
