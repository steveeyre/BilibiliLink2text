"""sherpa-onnx ASR engine: SenseVoice + Silero VAD segmentation.

模型来源（按优先级）：
  1. 项目内 sherpa-onnx/ 目录（用户自带 model.int8.onnx + tokens.txt + silero_vad.onnx）
  2. 否则从 HF 镜像下载标准 SenseVoice 模型
"""
from __future__ import annotations

import threading
import wave
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import app_dir, models_dir

HF_MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]

# 本地自带模型目录（用户放入了 sherpa-onnx/）
LOCAL_MODEL_DIR = app_dir() / "sherpa-onnx"

MODELS = {
    "sense-voice": {
        "dir": "sherpa-sense-voice",
        "files": {
            "model.int8.onnx": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx",
            "tokens.txt": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt",
        },
    },
    "silero-vad": {
        "dir": "sherpa-vad",
        "files": {
            "silero_vad.onnx": "csukuangfj/silero-vad-onnx/resolve/main/silero_vad.onnx",
        },
    },
}


class AsrError(Exception):
    pass


class AsrCancelled(Exception):
    """用户主动取消 ASR。"""
    pass


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise AsrCancelled("已取消")


def _download(base: str, rel: str, dest: Path, progress: Optional[Callable[[float], None]]) -> None:
    url = f"{base}/{rel}"
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


def _local_model_dir() -> Optional[Path]:
    """返回本地自带模型目录（须包含 model.onnx/model.int8.onnx + tokens.txt）。"""
    if not LOCAL_MODEL_DIR.is_dir():
        return None
    tok = LOCAL_MODEL_DIR / "tokens.txt"
    has_model = (LOCAL_MODEL_DIR / "model.int8.onnx").exists() or (LOCAL_MODEL_DIR / "model.onnx").exists()
    if tok.exists() and has_model:
        return LOCAL_MODEL_DIR
    return None


def ensure_model_files(name: str, progress: Optional[Callable[[float], None]] = None) -> Path:
    spec = MODELS[name]
    d = models_dir() / spec["dir"]
    d.mkdir(parents=True, exist_ok=True)
    for fname, rel in spec["files"].items():
        dest = d / fname
        if dest.exists() and dest.stat().st_size > 1000:
            continue
        tmp = d / (fname + ".part")
        if tmp.exists():
            tmp.unlink()
        last_err: Optional[Exception] = None
        for base in HF_MIRRORS:
            try:
                _download(base, rel, tmp, progress)
                tmp.replace(dest)
                last_err = None
                break
            except Exception as e:
                last_err = e
                continue
        if last_err or not dest.exists():
            raise AsrError(f"模型下载失败（已尝试 hf-mirror/huggingface）: {last_err}")
    return d


class SherpaASR:
    engine_label = "SenseVoice（本地）"

    def __init__(self, engine: str = "sense-voice", num_threads: int = 8):
        self.engine = engine
        self.num_threads = num_threads
        self._recognizer = None
        self._vad = None
        self._model_dir: Optional[Path] = None

    def ensure_ready(self, progress: Optional[Callable[[float], None]] = None) -> None:
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            raise AsrError("未安装 sherpa-onnx，请先 pip install sherpa-onnx")
        if self._recognizer is not None:
            return
        import sherpa_onnx

        local = _local_model_dir()
        if local is not None:
            self._model_dir = local
        else:
            self._model_dir = ensure_model_files(self.engine, progress)
        if self._vad is None:
            local_vad = LOCAL_MODEL_DIR / "silero_vad.onnx"
            if local_vad.exists():
                self._vad = self._make_vad(local_vad)
            else:
                vad_dir = ensure_model_files("silero-vad", progress)
                self._vad = self._make_vad(vad_dir / "silero_vad.onnx")

        tokens = str(self._model_dir / "tokens.txt")
        model_file = self._model_dir / ("model.int8.onnx" if (self._model_dir / "model.int8.onnx").exists() else "model.onnx")
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_file), tokens=tokens,
            language="zh", use_itn=True,
            num_threads=self.num_threads, debug=False)

    def _make_vad(self, model_path: Path):
        import sherpa_onnx
        vad_cfg = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(model_path), threshold=0.5,
                min_silence_duration=0.5, min_speech_duration=0.25,
                max_speech_duration=20),
            sample_rate=16000, num_threads=self.num_threads)
        return sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=60)

    def transcribe(self, wav_path: Path, progress: Optional[Callable[[float], None]] = None,
                   cancel: Optional[threading.Event] = None) -> list[dict]:
        self.ensure_ready()
        import struct as _struct
        import array as _array
        vad = self._vad
        vad.reset()
        out: list[dict] = []
        total_frames = 0
        with wave.open(str(wav_path), "rb") as w:
            sr = w.getframerate()
            duration = w.getnframes() / sr
            if sr != 16000:
                raise AsrError(f"WAV 采样率应为 16000，实际 {sr}")
            # 流式读取（每块 1 秒），边喂 VAD 边排空，避免长视频内存暴涨
            while True:
                _check_cancel(cancel)
                raw = w.readframes(16000)
                if not raw:
                    break
                n = len(raw) // 2
                vals = _struct.unpack(f"<{n}h", raw)
                samples = _array.array("f", (v / 32768.0 for v in vals))
                vad.accept_waveform(samples)
                self._drain(vad, sr, out)
                total_frames += n
                if progress and duration:
                    progress(min(1.0, total_frames / (duration * sr)))
        vad.flush()
        self._drain(vad, sr, out)
        if progress:
            progress(1.0)
        return out

    def _drain(self, vad, sample_rate: int, out: list) -> None:
        while not vad.empty():
            seg = vad.front
            start = seg.start / sample_rate  # 本版本 seg.start 以采样点计
            end = start + len(seg.samples) / sample_rate
            text = self._recognize(seg.samples)
            text = text.strip()
            if text:
                out.append({"start": round(start, 3), "end": round(end, 3), "text": text})
            vad.pop()

    def _recognize(self, samples) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        self._recognizer.decode_stream(stream)
        return stream.result.text

    def release(self) -> None:
        self._recognizer = None
        self._vad = None
