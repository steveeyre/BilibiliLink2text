"""下载器测试：ffmpeg 转码期间“停止”必须立刻生效（#2）。"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili import downloader


class FakeProcess:
    """模拟一个不主动结束的 ffmpeg；只在被 kill 后返回。"""

    def __init__(self, finish_immediately: bool = False, write_dst: Path | None = None):
        self.killed = False
        self.waits = 0
        self.returncode = None
        self.stderr = io.StringIO("")
        self._finish = finish_immediately
        self._write_dst = write_dst

    def wait(self, timeout=None):
        self.waits += 1
        if self.killed:
            self.returncode = -9
            return self.returncode
        if self._finish:
            self.returncode = 0
            if self._write_dst is not None:
                self._write_dst.write_bytes(b"RIFF")
            return 0
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)

    def kill(self):
        self.killed = True


class FfmpegToWavTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.src = self.tmp / "in.m4a"
        self.src.write_bytes(b"src")
        self.dst = self.tmp / "out.wav"
        p = mock.patch.object(downloader, "ensure_ffmpeg", lambda: Path("ffmpeg.exe"))
        p.start()
        self.addCleanup(p.stop)

    def _run(self, fake, cancel=None, timeout: int = 600):
        with mock.patch.object(downloader.subprocess, "Popen", lambda *a, **k: fake):
            return downloader.ffmpeg_to_wav(self.src, self.dst, cancel=cancel, timeout=timeout)

    def test_cancel_kills_process_promptly(self):
        fake = FakeProcess()
        ev = threading.Event()
        ev.set()
        with self.assertRaises(downloader.DownloadError) as ctx:
            self._run(fake, cancel=ev)
        self.assertIn("已取消", str(ctx.exception))
        self.assertTrue(fake.killed)
        self.assertFalse(self.dst.exists())
        self.assertLess(fake.waits, 5)      # 没有傻等到 timeout

    def test_timeout_kills_process(self):
        fake = FakeProcess()
        with self.assertRaises(downloader.DownloadError) as ctx:
            self._run(fake, timeout=0.05)
        self.assertIn("超时", str(ctx.exception))
        self.assertTrue(fake.killed)
        self.assertFalse(self.dst.exists())

    def test_success_returns_normally(self):
        fake = FakeProcess(finish_immediately=True, write_dst=self.dst)
        self._run(fake)
        self.assertTrue(self.dst.exists())
        self.assertFalse(fake.killed)

    def test_missing_output_raises(self):
        fake = FakeProcess(finish_immediately=True)   # 返回码 0 但没产出文件
        with self.assertRaises(downloader.DownloadError):
            self._run(fake)


class DownloadAudioTest(unittest.TestCase):
    """转码由本模块负责（可取消），不再走 yt-dlp 后处理器。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.workdir = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.converted: list[tuple[Path, Path]] = []
        patches = [
            mock.patch.object(downloader, "find_ffmpeg", lambda: Path("ffmpeg.exe")),
            mock.patch.object(downloader, "ensure_ffmpeg", lambda: Path("ffmpeg.exe")),
            mock.patch.object(downloader, "write_cookiefile", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_ydl(self):
        workdir = self.workdir

        class FakeYDL:
            opts = {}

            def __init__(self, opts):
                FakeYDL.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                (workdir / "BV1xx.m4a").write_bytes(b"audio")
                return {"id": "BV1xx"}

        return FakeYDL

    def _convert(self, src, dst, cancel=None, timeout=600):
        self.converted.append((src, dst))
        dst.write_bytes(b"RIFF")

    def test_downloads_then_converts_itself(self):
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", self._fake_ydl()), \
                mock.patch.object(downloader, "ffmpeg_to_wav", self._convert):
            out = downloader.download_audio("BV1xx", 1, downloader.Settings(), self.workdir)
        self.assertEqual(out, self.workdir / "BV1xx.wav")
        self.assertTrue(out.exists())
        self.assertEqual([s.name for s, _ in self.converted], ["BV1xx.m4a"])
        self.assertFalse((self.workdir / "BV1xx.m4a").exists())   # 中间产物已清理

    def test_no_ytdlp_audio_postprocessor(self):
        fake = self._fake_ydl()
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", fake), \
                mock.patch.object(downloader, "ffmpeg_to_wav", self._convert):
            downloader.download_audio("BV1xx", 1, downloader.Settings(), self.workdir)
        opts = fake.opts
        self.assertNotIn("postprocessors", opts)
        self.assertEqual(opts["ffmpeg_location"], ".")   # ffmpeg 实际所在目录

    def test_cancelled_conversion_propagates(self):
        ev = threading.Event()

        def boom(src, dst, cancel=None, timeout=600):
            raise downloader.DownloadError("已取消")

        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", self._fake_ydl()), \
                mock.patch.object(downloader, "ffmpeg_to_wav", boom):
            with self.assertRaises(downloader.DownloadError):
                downloader.download_audio("BV1xx", 1, downloader.Settings(), self.workdir,
                                          cancel=ev)


if __name__ == "__main__":
    unittest.main()
