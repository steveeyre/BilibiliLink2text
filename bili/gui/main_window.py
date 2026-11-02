"""主窗口。"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QInputDialog, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from .. import __version__, glossary
from ..config import Settings, resolve_export_dir, save_settings
from ..models import AppState, Job
from ..store import load_jobs, save_jobs
from ..textutil import build_markdown
from .diagnostics import DiagnosticsDialog
from .scan_dialog import ScanDialog
from .settings_dialog import SettingsDialog
from .widgets import JobRowWidget
from .worker import CacheWorker, FineWorker, QueueWorker, sanitize_filename

# 从任意粘贴文本中提取 B站 链接：兼容纯链接、带标题文字、Markdown 链接等
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
BILI_HOST_RE = re.compile(r"(bilibili\.com|b23\.tv|bili22\.cn|bili23\.cn)", re.IGNORECASE)
# 链接尾部可能粘连的标点 / 括号 / 标题分隔符（Markdown [x](url)、" | 标题"、中文标点等）
URL_TRAIL = r".,;:!?，。；：！？)\]}>【】\"'“”’|"


def _trim_url(u: str) -> str:
    """剥掉链接尾部粘连的标题文字 / 全角标点（如“...EEEE，看完这篇”）。"""
    while u and (u[-1] in URL_TRAIL or ord(u[-1]) >= 0x2E80):  # 0x2E80 起为 CJK/全角
        u = u[:-1]
    return u


def _dedupe_key(u: str) -> tuple:
    """队列去重键：同一个稿件的不同写法（BV 号 / 完整链接 / 带 p 参数）应视为同一条。"""
    m = BV_RE.search(u)
    if not m:
        return ("url", u)
    mp = re.search(r"[?&]p=(\d+)", u)
    return ("bv", m.group(0), mp.group(1) if mp else "1")

EMPTY_SUMMARY = """### AI 总结

处理完成后，这里会显示本视频的 AI 总结，可先阅读它再决定是否细读全文。

> 总结颗粒度可在「设置 → AI 总结」中调节。
"""

EMPTY_DOC = """### 校对文稿

选择左侧队列中的任务，转写并校对的全文会显示在这里。

- 在顶部粘贴 **B站 链接**（可多个），点击「添加」
- 点击「▶ 开始处理」，逐条生成文稿
- 无字幕的视频会先下载音频再做本地语音转写
"""


class MainWindow(QMainWindow):
    def __init__(self, state: AppState, settings: Settings):
        super().__init__()
        self.state = state
        self.settings = settings
        self.jobs: list[Job] = load_jobs()
        self._worker: QueueWorker | None = None
        self._fine_worker: FineWorker | None = None
        self._cache_worker: CacheWorker | None = None
        self._stopping = False
        self._current: Job | None = None
        self._items: dict[str, QListWidgetItem] = {}
        self._rows: dict[str, JobRowWidget] = {}
        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(800)
        self._persist_timer.timeout.connect(self._persist)
        self.setWindowTitle(f"BiliLink2Text v{__version__} — B站链接转校对文字稿")
        self.resize(1080, 720)
        self._build_ui()
        self.state.job_updated.connect(self._on_job_updated)
        self.state.summary_ready.connect(self._on_summary_ready)
        self.state.log.connect(lambda m: self.statusBar().showMessage(m, 8000))
        if self.jobs:
            self._rebuild_queue()
            self.statusBar().showMessage(
                f"已恢复上次的 {len(self.jobs)} 条任务，可直接查看/打开已导出的文稿", 8000)

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_brand())
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)
        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

    def _build_brand(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("brandBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 9, 14, 9)
        title = QLabel("BiliLink2Text")
        title.setObjectName("brandTitle")
        tag = QLabel("B站 链接 → 校对文字稿")
        tag.setObjectName("brandTag")
        chip = QLabel(self._engine_chip_text())
        chip.setProperty("role", "chip")
        lay.addWidget(title)
        lay.addSpacing(10)
        lay.addWidget(tag)
        lay.addStretch(1)
        lay.addWidget(chip)
        return bar

    def _engine_chip_text(self) -> str:
        s = self.settings
        asr = "SenseVoice" if s.asr_engine == "sherpa" else f"whisper {s.whisper_model}"
        if s.llm_provider == "ollama":
            llm = s.ollama_model
        else:
            llm = s.api_model or "API"
        return f"ASR {asr} · LLM {llm}"

    def _build_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)

        self.url_input = QPlainTextEdit()
        self.url_input.setPlaceholderText("粘贴 B站 视频链接，每行一条，可一次粘贴多条（Ctrl+Enter 添加）\n兼容带标题的文字（如“链接 | 标题”或 Markdown [标题](链接)），自动提取链接")
        self.url_input.setFixedHeight(64)
        esc_add = QShortcut(QKeySequence("Ctrl+Return"), self.url_input)
        esc_add.activated.connect(self._add_input)
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._add_input)
        scan_btn = QPushButton("扫描列表")
        scan_btn.clicked.connect(self._scan)
        row = QHBoxLayout()
        row.addWidget(self.url_input, 1)
        col = QVBoxLayout()
        col.addWidget(add_btn)
        col.addWidget(scan_btn)
        row.addLayout(col)
        lay.addLayout(row)

        self.queue = QListWidget()
        self.queue.currentItemChanged.connect(self._on_select)
        lay.addWidget(self.queue, 1)

        ctrl = QHBoxLayout()
        self.start_btn = QPushButton("开始处理")
        self.start_btn.setProperty("role", "primary")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self._clear_done)
        ctrl.addWidget(self.start_btn)
        ctrl.addWidget(self.stop_btn)
        ctrl.addWidget(clear_btn)
        ctrl.addStretch(1)
        lay.addLayout(ctrl)
        return w

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        self.tabs = QTabWidget()
        self.summary_view = QTextBrowser()
        self.summary_view.setOpenExternalLinks(True)
        self.doc_view = QTextBrowser()
        self.doc_view.setOpenExternalLinks(True)
        # 选中错词 → 右键加入替换词典（Vocotype 式闭环）
        self.doc_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.doc_view.customContextMenuRequested.connect(self._doc_menu)
        self.tabs.addTab(self.summary_view, "AI 总结")
        self.tabs.addTab(self.doc_view, "校对文稿")
        lay.addWidget(self.tabs, 1)

        actions = QHBoxLayout()
        self.fine_btn = QPushButton("精校（云端）")
        self.fine_btn.setToolTip("用设置中配置的云端 API 对文稿做二遍校对，\n修正本地模型漏掉的同音错字。")
        self.fine_btn.clicked.connect(self._fine_proofread)
        self.undo_fine_btn = QPushButton("撤销精校")
        self.undo_fine_btn.setToolTip("恢复本次精校之前的文稿（仅保留最近一次）。")
        self.undo_fine_btn.clicked.connect(self._undo_fine)
        self.copy_btn = QPushButton("复制为 MarkDown")
        self.copy_btn.clicked.connect(self._copy_md)
        self.export_btn = QPushButton("导出 .md")
        self.export_btn.clicked.connect(self._export_now)
        open_btn = QPushButton("打开输出文件夹")
        open_btn.clicked.connect(self._open_export_dir)
        clean_btn = QPushButton("清理缓存")
        clean_btn.setToolTip("删除 data/cache 下的音频中间文件（wav/mp3 等），释放磁盘。已导出的 .md 不受影响。")
        clean_btn.clicked.connect(self._clean_cache)
        log_btn = QPushButton("打开日志")
        log_btn.setToolTip("打开 data/logs/app.log，排查异常时把它发出来即可。")
        log_btn.clicked.connect(self._open_log)
        diag_btn = QPushButton("诊断")
        diag_btn.setToolTip("当前任务的阶段耗时、错误原文与日志尾部，可一键复制。")
        diag_btn.clicked.connect(lambda: self._show_diagnostics(self._current))
        set_btn = QPushButton("设置")
        set_btn.clicked.connect(self._open_settings)
        actions.addWidget(self.fine_btn)
        actions.addWidget(self.undo_fine_btn)
        actions.addWidget(self.copy_btn)
        actions.addWidget(self.export_btn)
        actions.addWidget(open_btn)
        actions.addWidget(clean_btn)
        actions.addWidget(log_btn)
        actions.addWidget(diag_btn)
        actions.addStretch(1)
        actions.addWidget(set_btn)
        lay.addLayout(actions)
        return w

    # ---------- 队列 ----------
    def _add_input(self):
        text = self.url_input.toPlainText()
        self.url_input.clear()
        self.add_urls(text)

    def add_urls(self, text: str):
        found = 0
        skipped = 0
        urls = []
        seen = set()
        # 1) 提取文本中的 B站 网址（自动剥离尾部标点 / Markdown 括号 / 标题分隔符）
        for m in URL_RE.finditer(text):
            u = _trim_url(m.group(0))
            if not BILI_HOST_RE.search(u):
                continue
            if u not in seen:
                seen.add(u)
                urls.append(u)
        # 2) 补抓裸 BV 号（不含在上一步 URL 内的）
        for m in BV_RE.finditer(text):
            if any(m.group(0) in u for u in urls):
                continue
            bv = m.group(0)
            if bv not in seen:
                seen.add(bv)
                urls.append(bv)
        # 3) 与既有队列按 (bvid, p) 去重：同一稿件重复粘贴不再入队
        existing = {_dedupe_key(j.url) for j in self.jobs}
        for u in urls:
            k = _dedupe_key(u)
            if k in existing:
                skipped += 1
                continue
            existing.add(k)
            self.jobs.append(Job(url=u))
            found += 1
        self._rebuild_queue()
        if found or skipped:
            msg = f"已添加 {found} 个视频到队列"
            if skipped:
                msg += f"（跳过 {skipped} 个已在队列中的）"
            self.statusBar().showMessage(msg, 4000)
        self._schedule_persist()

    def _rebuild_queue(self):
        self.queue.blockSignals(True)
        self.queue.clear()
        self._items = {}
        self._rows = {}
        width = self.queue.viewport().width()
        for i, job in enumerate(self.jobs, 1):
            it = QListWidgetItem()
            it.setData(Qt.UserRole, id(job))
            row = JobRowWidget()
            row.update_job(job, i, width)
            row.diagnose_requested.connect(self._show_diagnostics)
            row.remove_requested.connect(self._remove_job)
            row.retry_requested.connect(self._retry_job)
            row.copy_requested.connect(self._copy_job_url)
            row.open_requested.connect(self._open_job_export)
            it.setSizeHint(row.sizeHint())
            self.queue.addItem(it)
            self.queue.setItemWidget(it, row)
            self._items[job.id] = it
            self._rows[job.id] = row
        self.queue.blockSignals(False)
        if self.jobs:
            self.queue.setCurrentRow(len(self.jobs) - 1)
        self._sync_selection()

    def _relide_rows(self):
        width = self.queue.viewport().width()
        for i, job in enumerate(self.jobs, 1):
            row = self._rows.get(job.id)
            if row is not None:
                row.update_job(job, i, width)

    def _sync_selection(self):
        cur = self.queue.currentItem()
        sel_id = id(self._current) if self._current else None
        for job in self.jobs:
            row = self._rows.get(job.id)
            if row is not None:
                row.set_selected(job is self._current or (cur is not None and id(job) == sel_id))

    def _on_job_updated(self, job: Job):
        if job.id in self._rows:
            idx = next((i for i, j in enumerate(self.jobs, 1) if j.id == job.id), 0)
            self._rows[job.id].update_job(job, idx, self.queue.viewport().width())
        else:
            self._rebuild_queue()
        if self._current is job:
            self._show_job(job)
        self._schedule_persist()

    def _on_select(self, _cur, _prev):
        cur = self.queue.currentItem()
        if cur is None:
            return
        job = self._job_at_id(cur.data(Qt.UserRole))
        if job is not None:
            self._current = job
            self._show_job(job)
            self._sync_selection()

    def _job_at_id(self, oid: int) -> Job | None:
        for j in self.jobs:
            if id(j) == oid:
                return j
        return None

    # ---------- 队列行操作 ----------
    def _remove_job(self, job: Job):
        if job.status == "处理中":
            QMessageBox.information(self, "提示", "任务正在处理中，请先点「停止」再移除。")
            return
        self.jobs = [j for j in self.jobs if j is not job]
        if self._current is job:
            self._current = self.jobs[-1] if self.jobs else None
        self._rebuild_queue()
        if self._current is None:
            self.summary_view.setMarkdown(EMPTY_SUMMARY)
            self.doc_view.setMarkdown(EMPTY_DOC)
        else:
            self._show_job(self._current)
        self.statusBar().showMessage(f"已移除: {job.display_title() or job.url}", 3000)
        self._schedule_persist()

    def _retry_job(self, job: Job):
        if job.status == "处理中":
            return
        job.status = "排队中"
        job.error = ""
        job.stage = ""
        job.progress = 0.0
        job.retries = 0   # 手动重试视为用户明确要求，重置自动重试计数
        self._rebuild_queue()
        self._schedule_persist()
        if self._worker is None or not self._worker.isRunning():
            self._start()

    def _copy_job_url(self, job: Job):
        QApplication.clipboard().setText(job.url)
        self.statusBar().showMessage("链接已复制到剪贴板", 3000)

    def _open_job_export(self, job: Job):
        p = job.export_path
        if not p or not Path(p).exists():
            QMessageBox.information(self, "提示", "该任务还没有导出的 Markdown 文件。")
            return
        os.startfile(p)

    def _show_job(self, job: Job):
        if not job.result:
            self.summary_view.setMarkdown(EMPTY_SUMMARY)
            self.doc_view.setMarkdown(EMPTY_DOC)
            self.statusBar().showMessage(f"{job.display_title()}: 尚未生成结果", 4000)
            return
        r = job.result
        self.summary_view.setMarkdown(r.summary or "（未生成总结，可在设置中开启）")
        md = build_markdown(r.info.get("title", ""), r.proofread or r.raw_text, r.chapters)
        self.doc_view.setMarkdown(md)
        src = "B站字幕" if r.source == "subtitle" else "语音转写"
        self.statusBar().showMessage(f"来源: {src} · 打开「校对文稿」查看全文", 6000)

    def _on_summary_ready(self, job: Job, summary: str):
        if self._current is job:
            self.summary_view.setMarkdown(summary or "（未生成总结）")
            self.tabs.setCurrentIndex(0)

    # ---------- 运行 ----------
    def _start(self):
        if self._worker is not None and self._worker.isRunning():
            return
        to_run = [j for j in self.jobs if j.status == "排队中"]
        # 手动点开始时才允许失败任务重试一次，且有上限（防永久失败无限循环）
        retryable = [j for j in self.jobs
                     if j.status == "失败" and j.retries < Job.MAX_RETRIES]
        if not to_run and not retryable:
            if any(j.status == "失败" for j in self.jobs):
                QMessageBox.information(
                    self, "提示",
                    f"失败任务已达重试上限（{Job.MAX_RETRIES} 次），不再自动重试。\n"
                    "请检查原因（Ollama 是否启动、视频是否有效）后清空重加。")
            else:
                QMessageBox.information(self, "提示", "队列中没有待处理的任务")
            return
        to_run = to_run + retryable
        for j in to_run:
            j.status = "排队中"
            j.error = ""
        self._rebuild_queue()
        self._worker = QueueWorker(self.state, self.settings, to_run, self)
        self._worker.finished.connect(self._on_worker_done)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._worker.start()

    def _stop(self):
        running = self._worker is not None and self._worker.isRunning()
        if not running:
            return  # 空闲时不置 _stopping，避免污染下次运行的完成分支（#22）
        self._stopping = True
        for j in self.jobs:
            if j.status == "排队中":
                j.status = "已停止"
        if self._worker is not None:
            self._worker.cancel.set()
        self._schedule_persist()

    def _on_worker_done(self):
        self._worker = None
        self._schedule_persist()
        self._rebuild_queue()
        if self._stopping:
            self._stopping = False
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.statusBar().showMessage("队列处理结束", 6000)
            return
        # 自动续跑：只续跑运行期间新增的排队任务，不拉起失败任务
        # （失败需用户手动点开始重试，且有 MAX_RETRIES 上限，避免永久失败无限循环）
        if any(j.status == "排队中" for j in self.jobs):
            self.statusBar().showMessage("队列中还有待处理任务，自动继续…", 3000)
            self._start()
        else:
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.statusBar().showMessage("队列处理结束", 6000)

    def _clear_done(self):
        self.jobs = [j for j in self.jobs if j.status not in ("完成", "失败")]
        self._current = None
        self._rebuild_queue()
        self._schedule_persist()

    # ---------- 持久化 ----------
    def _schedule_persist(self):
        """800ms 内的多次变更合并为一次写盘，避免频繁 IO。"""
        self._persist_timer.start()

    def _persist(self):
        try:
            save_jobs(self.jobs)
        except Exception as e:
            self.state.log.emit(f"队列保存失败: {e}")

    # ---------- 操作 ----------
    def _scan(self):
        dlg = ScanDialog(self.state, self.settings, self)
        if not dlg.exec():
            return
        urls = dlg.selected_urls()
        if not urls:
            self.statusBar().showMessage("未选择任何视频", 3000)
            return
        # 走 add_urls 以复用队列去重逻辑（扫描结果可能与已有队列重复）
        self.add_urls("\n".join(urls))

    def _copy_md(self):
        if self._current is None or not self._current.result:
            QMessageBox.information(self, "提示", "请先选择已完成的任务")
            return
        r = self._current.result
        md = build_markdown(r.info.get("title", ""), r.proofread or r.raw_text, r.chapters)
        QApplication.clipboard().setText(md)
        self.statusBar().showMessage("已复制为 MarkDown（无时间戳，保留章节）", 4000)

    def _export_now(self):
        if self._current is None or not self._current.result:
            QMessageBox.information(self, "提示", "请先选择已完成的任务")
            return
        r = self._current.result
        from ..textutil import build_export
        md = build_export(r.info, r.proofread, r.chapters, r.segments, r.summary, self.settings)
        d = resolve_export_dir(self.settings)
        fname = sanitize_filename(r.info.get("title") or r.info["bvid"]) + ".md"
        path = d / fname
        path.write_text(md, encoding="utf-8")
        self._current.export_path = str(path)
        self._schedule_persist()
        box = QMessageBox(self)
        box.setWindowTitle("已导出")
        box.setText(f"已导出到：\n{path}")
        open_btn = box.addButton("打开文件夹", QMessageBox.ActionRole)
        box.addButton("好的", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            os.startfile(str(d))

    def _open_export_dir(self):
        d = resolve_export_dir(self.settings)
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(str(d))

    def _clean_cache(self):
        if self._cache_worker is not None and self._cache_worker.isRunning():
            return
        w = CacheWorker(clear=False, parent=self)
        w.scanned.connect(self._on_cache_scanned)
        w.finished.connect(self._on_cache_worker_finished)
        self._cache_worker = w
        w.start()
        self.statusBar().showMessage("正在统计缓存大小…", 4000)

    def _on_cache_worker_finished(self):
        if self.sender() is self._cache_worker:
            self._cache_worker = None

    def _on_cache_scanned(self, n: int, total: int):
        mb = total / 1024 / 1024
        if n == 0:
            QMessageBox.information(self, "清理缓存", "缓存为空，无需清理。")
            return
        r = QMessageBox.question(
            self, "清理缓存",
            f"缓存共 {n} 个文件，约 {mb:.1f} MB。\n"
            "将删除音频中间文件（wav/mp3 等），已导出的 .md 不受影响。\n继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if r != QMessageBox.Yes:
            return
        w = CacheWorker(clear=True, parent=self)
        w.cleared.connect(self._on_cache_cleared)
        w.finished.connect(self._on_cache_worker_finished)
        self._cache_worker = w
        w.start()
        self.statusBar().showMessage("正在清理缓存…", 4000)

    def _on_cache_cleared(self, n: int, total: int):
        mb = total / 1024 / 1024
        QMessageBox.information(self, "清理缓存", f"已清理 {n} 个文件，释放约 {mb:.1f} MB。")

    def _open_log(self):
        from ..config import log_file
        f = log_file()
        if not f.exists():
            f.write_text("", encoding="utf-8")
        os.startfile(str(f))

    # ---------- 诊断 / 词典闭环 ----------
    def _show_diagnostics(self, job: Job | None):
        if job is None:
            QMessageBox.information(self, "诊断", "请先在队列中选择一个任务。")
            return
        DiagnosticsDialog(job, self.settings, self).exec()

    def _doc_menu(self, pos):
        cursor = self.doc_view.textCursor()
        sel = cursor.selectedText().replace("\u2029", "\n").strip()
        menu = QMenu(self.doc_view)
        act_copy = menu.addAction("复制", self.doc_view.copy)
        act_copy.setEnabled(cursor.hasSelection())
        menu.addSeparator()
        act_rule = menu.addAction("把选中内容加入替换词典…",
                                  lambda: self._add_rule_from_selection(sel))
        act_rule.setEnabled(bool(sel) and "\n" not in sel)
        menu.exec(self.doc_view.mapToGlobal(pos))

    def _add_rule_from_selection(self, wrong: str):
        wrong = (wrong or "").strip()
        if not wrong:
            return
        right, ok = QInputDialog.getText(
            self, "加入替换词典", f"选中「{wrong}」\n以后每次转写后自动替换为：")
        if not ok:
            return
        right = right.strip()
        if not right or right == wrong:
            return
        rules = glossary.rules_from_settings(self.settings.replace_rules)
        if any(r.src == wrong and not r.regex for r in rules):
            QMessageBox.information(self, "加入替换词典", "该词已在词典中。")
            return
        rules.append(glossary.Rule(wrong, right))
        self.settings.replace_rules = [r.to_dict() for r in rules]
        try:
            save_settings(self.settings)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"替换词典未能写入配置：{e}")
            return
        self.statusBar().showMessage(
            f"已加入替换词典：{wrong} → {right}（下次任务生效）", 6000)

    # ---------- 精校 ----------
    def _fine_proofread(self):
        if self._fine_worker is not None and self._fine_worker.isRunning():
            return
        job = self._current
        if job is None or not job.result or not (job.result.proofread or job.result.raw_text):
            QMessageBox.information(self, "提示", "请先选择已完成的任务")
            return
        if not (self.settings.fine_base.strip() and self.settings.fine_model.strip()):
            QMessageBox.information(
                self, "提示",
                "精校尚未配置。\n\n请到 设置 → 精校（云端 API） 填写 API 地址、Key 与模型名\n"
                "（支持任意 OpenAI 兼容接口，如 DeepSeek、通义千问等）")
            return
        if getattr(job, "_fine_done_once", False):
            ret = QMessageBox.question(
                self, "再次精校",
                "该任务已经精校过，再次精校会覆盖上次结果。\n继续吗？（上次结果可在本窗口撤销一次）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
        text = job.result.proofread or job.result.raw_text
        self.fine_btn.setEnabled(False)
        self.statusBar().showMessage(f"精校中…（{job.display_title()}）", 20000)
        self._fine_worker = FineWorker(self.state, self.settings, job, text, self)
        self._fine_worker.done.connect(self._on_fine_done)
        self._fine_worker.failed.connect(self._on_fine_failed)
        self._fine_worker.start()

    def _on_fine_done(self, job: Job, text: str):
        if job.result:
            job._fine_prev = job.result.proofread  # 保留一次撤销
            job.result.proofread = text
            job.progress = 1.0
            # 精校改了正文：章节/总结按新文本重算，避免展示过期内容
            try:
                from .. import llm as _llm
                from ..textutil import ensure_chapters_cover as _cover
                job.result.chapters = _cover(
                    text, _llm.chapterize(self.settings, text))
                if self.settings.summary_granularity != "off":
                    job.result.summary = _llm.summarize(
                        self.settings, text, self.settings.summary_granularity)
            except Exception as e:
                job.result.chapters = None
                self.statusBar().showMessage(f"精校已应用，章节/总结重算失败：{e}，已保留新文稿", 8000)
            else:
                self.statusBar().showMessage("精校完成，文稿/章节/总结已更新并重新导出", 6000)
            job._fine_done_once = True
            self._re_export(job)
        self._fine_worker = None
        self.fine_btn.setEnabled(True)
        self.state.job_updated.emit(job)
        self._schedule_persist()

    def _undo_fine(self):
        job = self._current
        if job is None or not job.result or not getattr(job, "_fine_prev", None):
            QMessageBox.information(self, "提示", "没有可撤销的精校结果")
            return
        job.result.proofread = job._fine_prev
        job._fine_prev = None
        job._fine_done_once = False
        try:
            from .. import llm as _llm
            from ..textutil import ensure_chapters_cover as _cover
            job.result.chapters = _cover(
                job.result.proofread,
                _llm.chapterize(self.settings, job.result.proofread))
            if self.settings.summary_granularity != "off":
                job.result.summary = _llm.summarize(
                    self.settings, job.result.proofread,
                    self.settings.summary_granularity)
        except Exception as e:
            self.statusBar().showMessage(f"已撤销精校，章节/总结重算失败：{e}", 8000)
        self._re_export(job)
        self.state.job_updated.emit(job)
        self.statusBar().showMessage("已撤销精校，回到上次文稿并重新导出", 6000)

    def _re_export(self, job: Job) -> None:
        """精校/撤销后若此前已自动导出过，则用新文本静默重导出，保持 .md 不过期。"""
        if not job.result or not self.settings.export_enabled:
            return
        try:
            from ..textutil import build_export
            r = job.result
            md = build_export(r.info, r.proofread, r.chapters, r.segments,
                              r.summary, self.settings)
            d = resolve_export_dir(self.settings)
            fname = sanitize_filename(r.info.get("title") or r.info["bvid"]) + ".md"
            path = d / fname
            path.write_text(md, encoding="utf-8")
            job.export_path = str(path)
            self.state.log.emit(f"精校后已重新导出: {path}")
            self._schedule_persist()
        except Exception as e:
            self.statusBar().showMessage(f"重新导出失败：{e}，请手动点“导出 .md”", 8000)

    def _on_fine_failed(self, job: Job, error: str):
        self._fine_worker = None
        self.fine_btn.setEnabled(True)
        self.state.job_updated.emit(job)
        QMessageBox.critical(self, "精校失败", error)

    def _open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.state.settings_changed.emit()
            self.chip_label_update()
            self.statusBar().showMessage("设置已保存", 4000)

    def closeEvent(self, ev):
        self._persist()
        for w in (self._worker, self._fine_worker, self._cache_worker):
            try:
                if w is not None and w.isRunning():
                    w.cancel.set() if hasattr(w, "cancel") else None
                    if isinstance(w, FineWorker):
                        w.cancel_event.set()
                    w.wait(5000)
            except Exception:
                pass
        try:
            from .. import llm_server
            llm_server.stop()
        except Exception:
            pass
        super().closeEvent(ev)

    def chip_label_update(self):
        for w in self.findChildren(QLabel):
            if w.property("role") == "chip":
                w.setText(self._engine_chip_text())

    def resizeEvent(self, _ev):
        super().resizeEvent(_ev)
        self._relide_rows()
