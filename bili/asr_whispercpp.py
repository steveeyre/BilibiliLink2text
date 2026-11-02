"""whisper.cpp ASR engine: ggml model auto-download, whisper-cli.exe auto-download."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import models_dir, tools_dir
from .asr_sherpa import AsrCancelled, AsrError, ensure_model_files

WHISPER_CPU_FILES = {
    "tiny": "ggerganov/whisper.cpp/resolve/main/ggml-tiny-q5_1.bin",
    "base": "ggerganov/whisper.cpp/resolve/main/ggml-base-q5_1.bin",
    "small": "ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin",
    "medium": "ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin",
    "large-v3": "ggerganov/whisper.cpp/resolve/main/ggml-large-v3-q5_0.bin",
}
HF_MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]
PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")

BIN_DIR = tools_dir() / "whisper-cli"
EXE = BIN_DIR / "whisper-whisper-cli.exe"


CLI_RE = re.compile(r"(?:^|/)(?:main|whisper(?:-whisper)*-cli)\.exe$")
_HELP_CACHE: dict[str, str] = {}


def _exe() -> Path:
    """在 whisper-cli 目录中找真正的 CLI 可执行文件（排除弃用壳/server 等工具）。"""
    if BIN_DIR.exists():
        exes = [p for p in BIN_DIR.glob("*.exe")]
        cli = [p for p in exes if CLI_RE.search(p.name) and "deprecat" not in p.name.lower()]
        pool = cli or [p for p in exes if "server" not in p.name.lower()]
        if pool:
            return max(pool, key=lambda p: p.stat().st_size)
    return EXE


FALLBACK_TAG = "b4938"
GH_MIRRORS = [
    "https://github.com",                       # 直连
    "https://gh-proxy.com/https://github.com",  # 国内镜像
    "https://ghproxy.net/https://github.com",
]


def _latest_tag() -> str:
    try:
        r = requests.get("https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest",
                         timeout=20)
        if r.status_code == 200:
            return r.json().get("tag_name") or FALLBACK_TAG
    except Exception:
        pass
    return FALLBACK_TAG


def _download_whisper_cli(progress: Optional[Callable[[float], None]] = None) -> Path:
    tag = _latest_tag()
    url = f"https://github.com/ggml-org/whisper.cpp/releases/download/{tag}/whisper-bin-x64.zip"
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BIN_DIR / "whisper-cli.zip"
    tmp.unlink(missing_ok=True)
    last_err: Optional[Exception] = None
    for base in GH_MIRRORS:
        try:
            _stream_download(base + url.removeprefix("https://github.com"), tmp, progress)
            last_err = None
            break
        except Exception as e:
            last_err = e
            continue
    if last_err or not tmp.exists():
        raise AsrError(f"whisper.cpp 下载失败: {last_err}")
    with zipfile.ZipFile(tmp) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # 丢弃 Release/ 前缀，全部平铺
            with z.open(info) as src, open(BIN_DIR / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    tmp.unlink(missing_ok=True)
    cli = [p for p in BIN_DIR.glob("*.exe")
           if CLI_RE.search(p.name) and "deprecat" not in p.name.lower()]
    if not cli:
        raise AsrError("压缩包中未找到 CLI 可执行文件")
    return max(cli, key=lambda p: p.stat().st_size)


def _stream_download(url: str, dest: Path, progress: Optional[Callable[[float], None]]) -> None:
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = requests.get(url, stream=True, timeout=60)
            if r.status_code != 200:
                raise AsrError(f"下载失败 HTTP {r.status_code}: {url}")
            total = int(r.headers.get("content-length") or 0)
            got = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(min(1.0, got / total))
            return
        except Exception as e:
            last_err = e
            dest.unlink(missing_ok=True)
            continue
    raise AsrError(f"下载失败（已重试 3 次）: {last_err}")


def _hint_supported(help_text: str) -> bool:
    """whisper.cpp 只在较新版本支持 --carry-initial-prompt；老版本会因未知参数直接失败。"""
    return "--carry-initial-prompt" in help_text


def _help_text(exe: Path) -> str:
    key = str(exe)
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    text = ""
    try:
        r = subprocess.run([str(exe), "--help"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = (r.stdout or "") + (r.stderr or "")
    except Exception:
        text = ""
    _HELP_CACHE[key] = text
    return text


class WhisperCppASR:
    engine_label = "whisper.cpp（本地）"

    def __init__(self, model_size: str = "medium", threads: int = 8, prompt: str = ""):
        self.model_size = model_size if model_size in WHISPER_CPU_FILES else "medium"
        self.threads = threads
        self.prompt = (prompt or "").strip()
        self._model_path: Optional[Path] = None

    def ensure_ready(self, progress: Optional[Callable[[float], None]] = None) -> None:
        exe = _exe()
        if not exe.exists():
            exe = _download_whisper_cli(progress)
        d = models_dir() / "whisper-ggml"
        d.mkdir(parents=True, exist_ok=True)
        rel = WHISPER_CPU_FILES[self.model_size]
        model = d / rel.rsplit("/", 1)[-1]
        if not model.exists() or model.stat().st_size < 1000:
            model = _download_ggml(rel, d, progress)
        self._model_path = model

    def transcribe(self, wav_path: Path, progress: Optional[Callable[[float], None]] = None,
                   cancel: Optional[threading.Event] = None) -> list[dict]:
        exe = _exe()
        if self._model_path is None:
            self.ensure_ready()
        out_base = wav_path.with_suffix("")
        cmd = [str(exe), "-m", str(self._model_path), "-f", str(wav_path), "-l", "auto",
               "-t", str(self.threads), "-oj", "-of", str(out_base)]
        if self.prompt:
            cmd += ["--prompt", self.prompt]
            # 不带这个开关时提示只影响第一个 30s 窗口，长视频的专有名词等于没给
            if _hint_supported(_help_text(exe)):
                cmd.append("--carry-initial-prompt")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert p.stderr is not None
        for line in p.stderr:
            if cancel is not None and cancel.is_set():
                p.kill()
                p.wait()
                raise AsrCancelled("已取消")
            m = PROGRESS_RE.search(line)
            if m and progress:
                progress(min(1.0, int(m.group(1)) / 100.0))
        rc = p.wait()
        if cancel is not None and cancel.is_set():
            raise AsrCancelled("已取消")
        if rc != 0:
            raise AsrError(f"whisper.cpp 执行失败 (exit {rc})")
        jf = out_base.with_suffix(".json")
        if not jf.exists():
            jf = Path(str(out_base) + ".json")
        data = json.loads(jf.read_text(encoding="utf-8"))
        out = []
        for seg in data.get("transcription", []):
            offs = seg.get("offsets", {})
            text = (seg.get("text") or "").strip()
            if text:
                out.append({"start": offs.get("from", 0) / 1000.0,
                            "end": offs.get("to", 0) / 1000.0, "text": text})
        if progress:
            progress(1.0)
        return out

    def release(self) -> None:
        self._model_path = None


def _download_ggml(rel: str, dest_dir: Path, progress: Optional[Callable[[float], None]]) -> Path:
    dest = dest_dir / rel.rsplit("/", 1)[-1]
    tmp = dest.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    last_err: Optional[Exception] = None
    for base in HF_MIRRORS:
        try:
            _stream_download(f"{base}/{rel}", tmp, progress)
            tmp.replace(dest)
            last_err = None
            break
        except Exception as e:
            last_err = e
    if last_err or not dest.exists():
        raise AsrError(f"whisper 模型下载失败: {last_err}")
    return dest


def download_sherpa_models_if_needed(progress: Optional[Callable[[float], None]] = None) -> None:
    """预下载 SenseVoice 模型（用于检查/准备阶段）。"""
    ensure_model_files("sense-voice", progress)
    ensure_model_files("silero-vad", progress)
