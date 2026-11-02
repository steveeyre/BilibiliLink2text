"""应用状态模型与任务列表。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QObject, Signal

from .pipeline import VideoResult


@dataclass
class Job:
    MAX_RETRIES = 2               # 失败任务最多自动/手动重试次数

    url: str
    title: str = ""
    status: str = "排队中"
    stage: str = ""
    progress: float = 0.0
    error: str = ""
    retries: int = 0                # 失败次数（用于重试上限，避免无限自动重试）
    result: Optional[VideoResult] = None
    export_path: str = ""           # 最近一次导出的 .md 绝对路径（行内「打开文件」用）
    created: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    _fine_prev: Optional[str] = field(default=None, repr=False)   # 精校前文稿（撤销用）
    _fine_done_once: bool = field(default=False, repr=False)      # 是否已精校过

    def display_title(self) -> str:
        return self.title or self.url


class AppState(QObject):
    job_updated = Signal(object)          # Job
    log = Signal(str)
    summary_ready = Signal(object, str)   # Job, summary text
    settings_changed = Signal()
