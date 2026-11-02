"""单视频处理管线：解析 → 字幕优先 → 下载/ASR → 校对 → 章节 → 总结."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import bili_api
from . import glossary
from .asr_sherpa import SherpaASR
from .asr_whispercpp import WhisperCppASR
from .config import Settings, cache_dir
from .cookies import cookie_header
from .downloader import download_audio
from . import llm
from .textutil import ensure_chapters_cover, merge_segments

Progress = Callable[[float], None]
Status = Callable[[str], None]


class PipelineError(Exception):
    pass


class PipelineCancelled(Exception):
    """用户主动取消，worker 应标"已停止"而非"失败"。"""
    pass


@dataclass
class VideoResult:
    info: dict
    segments: list = field(default_factory=list)      # 原始字幕段（含时间戳）
    paragraphs: list = field(default_factory=list)    # 合并后的自然段
    raw_text: str = ""
    proofread: str = ""
    chapters: Optional[list] = None
    summary: Optional[str] = None
    source: str = "asr"                               # subtitle | asr
    timings: dict = field(default_factory=dict)      # 阶段名 -> 耗时（秒），诊断面板用

    @property
    def display_text(self) -> str:
        return self.proofread or self.raw_text


def make_asr(settings: Settings):
    if settings.asr_engine == "whispercpp":
        return WhisperCppASR(model_size=settings.whisper_model,
                             prompt=glossary.build_asr_prompt(
                                 glossary.terms_from_settings(settings.glossary_terms)))
    return SherpaASR(engine="sense-voice")


def _cleanup_workdir(bvid: str, p: int) -> None:
    """成功后删除该视频的音频缓存（wav/mp3/m4a 等，只留结果 .md）。"""
    try:
        workdir = cache_dir() / f"{bvid}_p{p}"
        if not workdir.is_dir():
            return
        for f in workdir.glob("*"):
            try:
                if f.is_file() and f.suffix.lower() in (
                        ".wav", ".mp3", ".m4a", ".mp4", ".webm", ".part", ".ytdl"):
                    f.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            next(workdir.iterdir())
        except StopIteration:
            workdir.rmdir()
    except OSError:
        pass


def cache_usage() -> tuple[int, int]:
    """返回 (文件数, 字节数)，供“清理缓存”确认框展示。"""
    try:
        files = [p for p in cache_dir().glob("*") if p.is_file()] + \
                [p for d in cache_dir().iterdir() if d.is_dir() for p in d.glob("*") if p.is_file()]
    except OSError:
        return 0, 0
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return len(files), total


def clear_cache() -> None:
    """清空 data/cache（含 cookie 复用文件），模型目录不受影响。"""
    root = cache_dir()
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir():
                import shutil
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            pass


def process_video(url: str, settings: Settings,
                  on_status: Status = None,
                  on_progress: Progress = None,
                  cancel: threading.Event = None) -> VideoResult:
    """处理单个视频。on_status 每阶段调用一次；on_progress 为 0..1 的阶段进度。"""
    stage = {"p": 0.0}
    timings: dict[str, float] = {}
    mark = [time.monotonic()]

    def lap(name: str) -> None:
        now = time.monotonic()
        timings[name] = round(now - mark[0], 1)
        mark[0] = now

    def status(text):
        if on_status:
            on_status(text)

    def prog(p):
        if on_progress:
            on_progress(p)

    try:
        bvid, p = bili_api.parse_video_url(url)
        status("获取视频信息…")
        ck = cookie_header(settings)
        info = bili_api.fetch_video_info(bvid, p, ck)

        segments: Optional[list] = None
        source = "asr"
        if not settings.always_asr:
            status("尝试获取字幕…")
            try:
                segments = bili_api.fetch_subtitles(bvid, info["cid"], ck)
                if segments:
                    source = "subtitle"
                    status("已使用 B站 自带字幕")
            except bili_api.BiliError as e:
                status(f"字幕获取失败（{e}），改用语音转写")

        if segments is None:
            status("下载音频…")
            workdir = cache_dir() / f"{bvid}_p{p}"
            workdir.mkdir(parents=True, exist_ok=True)
            wav = download_audio(bvid, p, settings, workdir, progress=prog, cancel=cancel)
            if cancel and cancel.is_set():
                raise PipelineCancelled("已取消")
            engine = make_asr(settings)
            status(f"语音转写中（{engine.engine_label}）…")
            try:
                segments = engine.transcribe(wav, progress=prog, cancel=cancel)
            finally:
                engine.release()

        if not segments:
            raise PipelineError("未获得任何转写文本")
        lap("获取文本")

        # 替换词典在合并段落之前生效：确定性修正，比让模型猜同音字可靠
        segments = glossary.apply_rules_to_segments(
            segments, glossary.rules_from_settings(settings.replace_rules))
        if not segments:
            raise PipelineError("替换词典把文本全部改成了空，请检查规则")

        paragraphs = merge_segments(segments)
        raw_text = "\n\n".join(s["text"].strip() for s in paragraphs)
        if not raw_text.strip():
            raise PipelineError("转写文本为空")

        status("AI 校对中…")
        stage["p"] = 0.0
        proofread = llm.proofread(settings, raw_text, progress=lambda p: prog(p),
                                  on_status=status, cancel=cancel)
        lap("AI 校对")

        chapters = None
        status("划分章节…")
        chapters = ensure_chapters_cover(
            proofread, llm.chapterize(settings, proofread, progress=lambda p: prog(p),
                                      cancel=cancel))
        lap("划分章节")

        summary = None
        if settings.summary_granularity != "off":
            status("生成 AI 总结…")
            summary = llm.summarize(settings, proofread, settings.summary_granularity,
                                    progress=lambda p: prog(p), cancel=cancel)
            lap("AI 总结")

        status("完成")
        prog(1.0)
        result = VideoResult(info=info, segments=segments, paragraphs=paragraphs,
                             raw_text=raw_text, proofread=proofread,
                             chapters=chapters, summary=summary, source=source,
                             timings=timings)
        # #11：成功后删掉大音频（中间产物），只留 .md 结果
        _cleanup_workdir(bvid, p)
        return result
    except PipelineError:
        raise
    except PipelineCancelled:
        raise
    except (llm.CancelledError, Exception) as e:
        from .asr_sherpa import AsrCancelled
        if isinstance(e, (llm.CancelledError, AsrCancelled, PipelineCancelled)) or \
                (cancel is not None and cancel.is_set()):
            raise PipelineCancelled("已取消") from e
        raise PipelineError(str(e)) from e
