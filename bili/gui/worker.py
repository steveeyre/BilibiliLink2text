"""后台队列线程。"""
from __future__ import annotations

import logging
import re
import threading

from PySide6.QtCore import QThread, Signal

from .. import llm
from ..asr_sherpa import AsrCancelled
from ..config import Settings, resolve_export_dir
from ..downloader import DownloadError
from ..models import AppState, Job
from ..pipeline import PipelineCancelled, process_video
from ..textutil import build_export

log = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip() or "untitled"


class QueueWorker(QThread):
    def __init__(self, state: AppState, settings: Settings, jobs: list[Job], parent=None):
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self.jobs = jobs
        self.cancel = threading.Event()

    def run(self):
        for job in self.jobs:
            if self.cancel.is_set():
                job.status = "已停止"
                self.state.job_updated.emit(job)
                break
            job.status = "处理中"
            job.progress = 0.0
            self.state.job_updated.emit(job)

            def on_status(t):
                job.stage = t
                self.state.job_updated.emit(job)

            def on_progress(p):
                job.progress = p
                self.state.job_updated.emit(job)

            try:
                result = process_video(job.url, self.settings, on_status, on_progress, self.cancel)
                job.result = result
                job.status = "完成"
                job.progress = 1.0
                job.stage = ""
                self.state.job_updated.emit(job)
                self.state.summary_ready.emit(job, result.summary or "")
                self._export(job)
            except (PipelineCancelled, AsrCancelled, llm.CancelledError):
                job.status = "已停止"
                job.error = ""
                job.stage = ""
                self.state.job_updated.emit(job)
                break  # 取消后不再继续下一任务
            except DownloadError as e:
                if self.cancel.is_set() or str(e) == "已取消":
                    job.status = "已停止"
                    job.error = ""
                    job.stage = ""
                    self.state.job_updated.emit(job)
                    break
                job.status = "失败"
                job.error = str(e)
                job.retries += 1
                job.stage = ""
                self.state.job_updated.emit(job)
            except Exception as e:
                if self.cancel.is_set():
                    job.status = "已停止"
                    job.error = ""
                else:
                    job.status = "失败"
                    job.error = str(e)
                    job.retries += 1
                    log.exception("任务失败 %s", job.url)
                job.stage = ""
                self.state.job_updated.emit(job)

    def _export(self, job: Job):
        s = self.settings
        if not s.export_enabled or not job.result:
            return
        md = build_export(job.result.info, job.result.proofread, job.result.chapters,
                          job.result.segments, job.result.summary, s)
        d = resolve_export_dir(s)
        fname = sanitize_filename(job.result.info.get("title") or job.result.info["bvid"]) + ".md"
        path = d / fname
        path.write_text(md, encoding="utf-8")
        job.export_path = str(path)
        self.state.log.emit(f"已导出: {path}")


class FineWorker(QThread):
    """精校线程：用云端 API 对已完成文稿做二遍校对，不阻塞界面。"""
    done = Signal(object, str)    # Job, 精校后文本
    failed = Signal(object, str)  # Job, 错误信息

    def __init__(self, state: AppState, settings: Settings, job: Job, text: str, parent=None):
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self.job = job
        self.text = text
        self.cancel_event = threading.Event()

    def run(self):
        def on_status(t):
            self.job.stage = t
            self.state.job_updated.emit(self.job)

        try:
            out = llm.fineproofread(self.settings, self.text, on_status=on_status,
                                    cancel=self.cancel_event)
            self.done.emit(self.job, out)
        except Exception as e:
            self.failed.emit(self.job, str(e))
        finally:
            self.job.stage = ""
            self.state.job_updated.emit(self.job)


class CacheWorker(QThread):
    """后台统计/清理 data/cache：文件多时不让 GUI 线程卡住。"""

    scanned = Signal(int, int)    # (文件数, 字节数)
    cleared = Signal(int, int)    # (删除的文件数, 释放字节数)

    def __init__(self, clear: bool = False, parent=None):
        super().__init__(parent)
        self._clear = clear

    def run(self):
        from ..pipeline import cache_usage, clear_cache
        n, total = cache_usage()
        if self._clear:
            clear_cache()
            self.cleared.emit(n, total)
        else:
            self.scanned.emit(n, total)


class GgufDownloadWorker(QThread):
    """后台下载 GGUF 模型。"""
    progress = Signal(int, int)   # (已下载字节, 总字节)
    done = Signal(str)            # 文件名
    failed = Signal(str)          # 错误信息

    def __init__(self, url: str, target, parent=None):
        super().__init__(parent)
        self._url = url
        self._target = target
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        import requests
        tmp = self._target.with_name(self._target.name + ".part")
        done_bytes = 0
        try:
            with requests.get(self._url, stream=True, timeout=30,
                              headers={"User-Agent": "BiliLink2Text"}) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if self._cancel.is_set():
                            f.close()
                            tmp.unlink(missing_ok=True)
                            return
                        f.write(chunk)
                        done_bytes += len(chunk)
                        self.progress.emit(done_bytes, total)
            tmp.replace(self._target)
            self.done.emit(self._target.name)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            self.failed.emit(str(e))
