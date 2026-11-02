"""诊断信息对话框：失败现场、阶段耗时、日志尾部集中到一处。

窗口化构建没有控制台，用户报错时只看到一行 elided 文字。这里把能自证的
信息一次给全，并提供「复制诊断信息」，省去来回问“日志在哪”。
"""
from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from .. import __version__
from ..config import Settings, read_log_tail


def _fmt_duration(sec) -> str:
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "?"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    return f"{m}m{s:02d}s"


def build_report(job, settings: Settings, log_lines: int = 50) -> str:
    r = job.result
    lines = [
        "== 任务 ==",
        f"标题：{getattr(job, 'title', '') or '（未获取）'}",
        f"链接：{job.url}",
        f"状态：{job.status}　重试：{job.retries}",
    ]
    if getattr(job, "export_path", ""):
        lines.append(f"导出：{job.export_path}")
    if r is not None:
        lines.append(f"来源：{'B站字幕' if r.source == 'subtitle' else '语音转写'}")
        if r.timings:
            parts = [f"{k} {_fmt_duration(v)}" for k, v in r.timings.items()]
            lines.append("阶段耗时：" + " · ".join(parts))
        lines.append(f"文稿：{len(r.proofread or r.raw_text)} 字　"
                     f"章节：{len(r.chapters or [])}　段：{len(r.segments)}")
    else:
        lines.append("阶段耗时：（无结果，任务未完成）")

    lines += ["", "== 环境 =="]
    lines.append(f"版本：BiliLink2Text v{__version__}")
    if settings.asr_engine == "sherpa":
        lines.append("ASR：sherpa-onnx · SenseVoice")
    else:
        lines.append(f"ASR：whisper.cpp · {settings.whisper_model}")
    lines.append(f"LLM：{settings.llm_provider} · {settings.llm_model()}　地址：{settings.llm_base_url()}")
    fine = "已配置" if (settings.fine_base.strip() and settings.fine_model.strip()) else "未配置"
    lines.append(f"精校：{fine}")
    lines.append(f"替换词典：{len(settings.replace_rules)} 条　"
                 f"专有名词：{len(settings.glossary_terms)} 个")

    if job.error:
        lines += ["", "== 错误 ==", job.error]

    tail = read_log_tail(log_lines)
    lines += ["", f"== 日志尾部（最后 {log_lines} 行）=="]
    lines.append(tail or "（暂无日志）")
    return "\n".join(lines)


class DiagnosticsDialog(QDialog):
    def __init__(self, job, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("诊断信息")
        self.resize(760, 560)
        self._report = build_report(job, settings)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        hint = QLabel("复制下面全部内容，连同问题描述一起发出来即可定位。")
        hint.setProperty("role", "hint")
        lay.addWidget(hint)

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(self._report)
        lay.addWidget(view, 1)

        row = QHBoxLayout()
        copy_btn = QPushButton("复制诊断信息")
        copy_btn.setProperty("role", "primary")
        copy_btn.clicked.connect(self._copy)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(copy_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        lay.addLayout(row)

    def _copy(self):
        QGuiApplication.clipboard().setText(self._report)
