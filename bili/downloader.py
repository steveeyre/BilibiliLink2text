"""yt-dlp download with Bilibili 412-risk patch, plus ffmpeg helpers."""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

from .config import Settings, tools_dir
from .cookies import write_cookiefile

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class DownloadError(Exception):
    pass


def _rand_dm() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def _apply_bili_dm_patch() -> None:
    """B站 412 风控补丁（借鉴 BiliNote）：为 playurl 注入 web_location / dm_img 参数。"""
    try:
        from yt_dlp.extractor.bilibili import BilibiliBaseIE
    except Exception:
        return
    if getattr(BilibiliBaseIE, "_bili_dm_patched", False):
        return
    orig = BilibiliBaseIE._download_playinfo

    def patched(self, *args, **kwargs):
        try:
            if args and isinstance(args[1], dict):
                args[1].update({
                    "web_location": "1550101",
                    "dm_img_str": _rand_dm(),
                    "dm_cover_img_str": _rand_dm(),
                    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
                })
            elif "query" in kwargs and isinstance(kwargs["query"], dict):
                kwargs["query"].update({
                    "web_location": "1550101",
                    "dm_img_str": _rand_dm(),
                    "dm_cover_img_str": _rand_dm(),
                    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
                })
        except Exception:
            pass
        return orig(self, *args, **kwargs)

    BilibiliBaseIE._download_playinfo = patched
    BilibiliBaseIE._bili_dm_patched = True


_apply_bili_dm_patch()


def find_ffmpeg() -> Optional[Path]:
    cands = [tools_dir() / "ffmpeg.exe"]
    env = shutil.which("ffmpeg")
    if env:
        cands.append(Path(env))
    for c in cands:
        if c and c.exists():
            return c
    return None


def ensure_ffmpeg(progress: Callable[[float], None] = None) -> Path:
    """下载 ffmpeg.exe 到 tools 目录（首次运行自动完成）。"""
    ff = find_ffmpeg()
    if ff:
        return ff
    import io
    import zipfile
    import requests as _req
    tools_dir().mkdir(parents=True, exist_ok=True)
    urls = [
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    ]
    last_err = None
    for url in urls:
        try:
            r = _req.get(url, stream=True, timeout=60)
            if r.status_code != 200:
                last_err = Exception(f"HTTP {r.status_code}")
                continue
            total = int(r.headers.get("content-length") or 0)
            got = 0
            buf = io.BytesIO()
            for chunk in r.iter_content(1 << 16):
                buf.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(min(1.0, got / total))
            buf.seek(0)
            with zipfile.ZipFile(buf) as z:
                target = next((n for n in z.namelist() if n.endswith("/ffmpeg.exe")), None)
                if target is None:
                    raise DownloadError("压缩包中未找到 ffmpeg.exe")
                with z.open(target) as src, open(tools_dir() / "ffmpeg.exe", "wb") as dst:
                    shutil.copyfileobj(src, dst)
            ff = find_ffmpeg()
            if ff:
                return ff
            last_err = Exception("ffmpeg.exe 未生成")
        except Exception as e:
            last_err = e
            continue
    raise DownloadError(f"ffmpeg 下载失败: {last_err}")


def _no_window_flag() -> int:
    """窗口化打包运行时压住 ffmpeg 控制台闪窗（llm_server 同款做法）。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _kill(p: subprocess.Popen) -> None:
    try:
        p.kill()
        p.wait(timeout=5)
    except Exception:
        pass


def ffmpeg_to_wav(src: Path, dst: Path, cancel: Optional[threading.Event] = None,
                  timeout: int = 600) -> None:
    """转 16k 单声道 WAV。用 Popen 轮询取消，避免 subprocess.run 期间停止无效（最长卡满 timeout）。"""
    ff = ensure_ffmpeg()
    cmd = [str(ff), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(src), "-ar", "16000", "-ac", "1",
           "-c:a", "pcm_s16le", str(dst)]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace",
                         creationflags=_no_window_flag())
    deadline = time.monotonic() + timeout
    while True:
        try:
            p.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                _kill(p)
                dst.unlink(missing_ok=True)
                raise DownloadError("已取消")
            if time.monotonic() > deadline:
                _kill(p)
                dst.unlink(missing_ok=True)
                raise DownloadError(f"音频转 WAV 超时（{timeout}s），已终止")
    err = ""
    if p.stderr is not None:
        try:
            err = p.stderr.read() or ""
        except Exception:
            err = ""
        p.stderr.close()
    if cancel is not None and cancel.is_set():
        dst.unlink(missing_ok=True)
        raise DownloadError("已取消")
    if p.returncode != 0 or not dst.exists():
        raise DownloadError(f"音频转 WAV 失败: {err[-500:]}")


def download_audio(bvid: str, p: int, settings: Settings, workdir: Path,
                   progress: Callable[[float], None] = None,
                   cancel: Optional[threading.Event] = None) -> Path:
    """Download best audio of one video, convert once to 16k mono WAV, return wav path.

    直接下载原始音轨再转一次 16k 单声道 WAV：既避免“mp3@64k → 解码 WAV”的双重
    有损路径，也让转码走可取消的 ffmpeg_to_wav（yt-dlp 的后处理器无法中断）。
    """
    url = f"https://www.bilibili.com/video/{bvid}"
    if p > 1:
        url += f"?p={p}"
    cf = write_cookiefile(settings)
    ff = find_ffmpeg()
    if ff is None:
        ff = ensure_ffmpeg()

    def hook(d):
        if cancel is not None and cancel.is_set():
            raise DownloadError("已取消")
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if total:
                progress(min(1.0, got / total))

    opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {"Referer": "https://www.bilibili.com", "User-Agent": UA},
        "progress_hooks": [hook] if progress else [],
        # #7：ffmpeg 若来自 PATH 则目录不是 tools_dir，必须用实际所在目录
        "ffmpeg_location": str(ff.parent),
        # 不再交给 yt-dlp 的 FFmpegExtractAudio：后处理器内部跑 ffmpeg 时无法中断，
        # 下载原始音轨后由本模块的 ffmpeg_to_wav 转一次（可取消、不做双重有损转码）
    }
    if cf:
        opts["cookiefile"] = str(cf)
    if settings.proxy:
        opts["proxy"] = settings.proxy

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        msg = str(e)
        if "412" in msg or "Precondition" in msg:
            raise DownloadError("B站触发了下载风控(412)，请配置登录 cookies 后重试") from e
        raise DownloadError(f"下载失败: {msg[-400:]}") from e

    vid = info.get("id") or bvid
    wav = workdir / f"{vid}.wav"
    if not wav.exists():
        src = next((c for c in sorted(workdir.glob(f"{vid}.*"))
                    if c.suffix.lower() not in (".wav", ".part")), None)
        if src is None:
            raise DownloadError("下载后未找到音频文件")
        ffmpeg_to_wav(src, wav, cancel)
        src.unlink(missing_ok=True)
    if cancel is not None and cancel.is_set():
        raise DownloadError("已取消")
    # 删掉中间产物，只留 16k WAV（#11 缓存 GC 的一部分）
    for p_ in workdir.glob(f"{vid}.*"):
        if p_ != wav and p_.suffix.lower() not in (".part",):
            p_.unlink(missing_ok=True)
    return wav
