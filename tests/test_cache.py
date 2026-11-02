"""缓存统计与清理测试（#11）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili import config as cfg
from bili.pipeline import cache_usage, clear_cache


class CacheTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._patch = mock.patch.object(cfg, "app_dir", lambda: self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        cache = cfg.cache_dir()
        work = cache / "BV1xx411c7mD_p1"
        work.mkdir(parents=True)
        (work / "audio.wav").write_bytes(b"0" * 1000)
        (work / "audio.mp3").write_bytes(b"0" * 500)
        (cache / "yt_cookies.txt").write_text("# Netscape", encoding="utf-8")

    def test_usage_counts_nested_and_flat_files(self):
        n, total = cache_usage()
        self.assertEqual(n, 3)
        self.assertEqual(total, 1500 + len("# Netscape"))

    def test_clear_removes_everything(self):
        clear_cache()
        self.assertEqual(list(cfg.cache_dir().iterdir()), [])
        self.assertEqual(cache_usage(), (0, 0))

    def test_usage_on_empty_cache(self):
        clear_cache()
        self.assertEqual(cache_usage(), (0, 0))


if __name__ == "__main__":
    unittest.main()
