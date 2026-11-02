"""自定义控件：队列行的五段流水线进度条 + 行组件。"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMenu, QPushButton,
                               QVBoxLayout, QWidget)

from .theme import C

STAGES = ["获取", "转写", "校对", "章节", "总结"]

# 阶段文案 -> 段位。顺序敏感：后面的阶段在前，避免「获取/视频」这类宽泛词抢匹配。
_STAGE_KEYS = (
    (5, ("完成", "已停止", "已取消")),
    (4, ("总结",)),
    (3, ("章节", "分段")),
    (2, ("校对", "精校")),
    (1, ("转写", "识别", "语音")),
    (0, ("获取", "下载", "字幕", "信息", "视频")),
)


def stage_index(stage: str) -> int:
    """把流水线阶段描述映射为进度条段位（0..4），完成=5，未知=-1。"""
    s = stage or ""
    for idx, keys in _STAGE_KEYS:
        if any(k in s for k in keys):
            return idx
    return -1


class StageBar(QWidget):
    """五段进度条：绿=已完成，粉=当前，暗=待办；失败时当前段变红。

    仅靠颜色表达状态对色觉障碍用户不友好，因此条尾再画一个 ✓/✕ 字形，
    并把阶段文字挂在 tooltip 上。
    """

    _GLYPH_W = 14
    _GAP = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "idle"   # idle | run | done | fail
        self._current = -1
        self.setFixedHeight(16)
        self._refresh_tip()

    def set_state(self, state: str, current: int = -1):
        if (state, current) == (self._state, self._current):
            return
        self._state = state
        self._current = current
        self._refresh_tip()
        self.update()

    def state_text(self) -> str:
        if self._state == "done":
            return "全部完成"
        if self._state == "fail":
            if 0 <= self._current < len(STAGES):
                return f"{STAGES[self._current]} 阶段失败"
            return "失败（失败阶段未知）"
        if self._state == "run":
            if 0 <= self._current < len(STAGES):
                return f"进行中：{STAGES[self._current]}"
            return "进行中"
        return "未开始"

    def _refresh_tip(self):
        self.setToolTip(self.state_text())

    def sizeHint(self) -> QSize:
        return QSize(180, 16)

    def _seg_color(self, i: int) -> str:
        if self._state == "done":
            return C["done"]
        if self._state in ("fail", "run"):
            # 阶段未知时不假装知道停在哪一段，只留轨道 + 条尾字形
            if self._current < 0:
                return C["track"]
            if i == self._current:
                return C["err"] if self._state == "fail" else C["accent"]
            return C["done"] if i < self._current else C["track"]
        return C["track"]

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(STAGES)
        glyph_w = self._GLYPH_W if self._state in ("done", "fail") else 0
        track_w = max(0, self.width() - (glyph_w + self._GAP if glyph_w else 0))
        # 每段最窄 2px，否则窗口极窄时算出负宽度画不出东西
        seg_w = max(2.0, (track_w - self._GAP * (n - 1)) / n)
        p.setPen(Qt.NoPen)
        for i in range(n):
            p.setBrush(QColor(self._seg_color(i)))
            p.drawRoundedRect(QRectF(i * (seg_w + self._GAP), 5, seg_w, 6), 3, 3)
        if glyph_w:
            f = QFont(self.font())
            f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(C["done"] if self._state == "done" else C["err"]))
            p.drawText(QRectF(self.width() - glyph_w, 0, glyph_w, self.height()),
                       Qt.AlignCenter, "✓" if self._state == "done" else "✕")
        p.end()


class JobRowWidget(QWidget):
    """队列行：序号+标题 / 状态+百分比 / 阶段进度条，附行内操作菜单。"""

    remove_requested = Signal(object)
    retry_requested = Signal(object)
    copy_requested = Signal(object)
    open_requested = Signal(object)
    diagnose_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected = False
        self._job = None
        self._last_stage = -1
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.title = QLabel()
        self.title.setProperty("role", "rowtitle")
        self.pct = QLabel()
        self.pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.pct.setProperty("role", "muted")
        self.menu_btn = QPushButton("⋯")
        self.menu_btn.setToolTip("行操作：复制链接 / 打开文件 / 诊断 / 重试 / 移除")
        self.menu_btn.setFixedWidth(26)
        self.menu_btn.setFocusPolicy(Qt.NoFocus)
        self.menu_btn.clicked.connect(self._popup_menu)
        r1.addWidget(self.title, 1)
        r1.addWidget(self.pct)
        r1.addWidget(self.menu_btn)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.status = QLabel()
        self.status.setProperty("role", "muted")
        self.bar = StageBar()
        r2.addWidget(self.status, 1)
        r2.addWidget(self.bar, 0, Qt.AlignVCenter)
        lay.addLayout(r2)

    # ---------- 展示 ----------
    def _set_tone(self, tone: str):
        self.status.setProperty("tone", tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _set_text(self, label: QLabel, text: str):
        label.setText(text)
        # 省略号截断后必须能看全文，否则长报错信息完全不可读
        label.setToolTip(text)

    def update_job(self, job, index: int, width: int):
        self._job = job
        title = job.display_title() or job.url
        fm = self.title.fontMetrics()
        budget = max(120, width - 130)
        self._set_text(self.title, fm.elidedText(f"{index}. {title}", Qt.ElideRight, budget))

        if job.status == "处理中":
            idx = stage_index(job.stage)
            if idx >= 0:
                self._last_stage = idx
            else:
                idx = self._last_stage   # 未知子阶段沿用上一段，避免进度条乱跳
            self._set_text(self.status, job.stage or "…")
            self._set_tone("accent")
            self.pct.setText(f"{int(job.progress * 100)}%")
            self.bar.set_state("run", idx)
        elif job.status == "完成":
            self._set_text(self.status, "完成")
            self._set_tone("done")
            self.pct.setText("")
            self.bar.set_state("done")
        elif job.status == "失败":
            err = job.error or "未知错误"
            fm2 = self.status.fontMetrics()
            self._set_text(self.status, fm2.elidedText(f"失败: {err}", Qt.ElideRight,
                                                       max(120, width - 130)))
            self._set_tone("err")
            self.pct.setText("")
            # 失败时 worker 已清空 stage，此时 stage_index 返回 -1：不夹到 0 假装第一段出错
            self.bar.set_state("fail", stage_index(job.stage or ""))
        else:
            self._set_text(self.status, job.status)
            self._set_tone("muted")
            self.pct.setText("")
            self.bar.set_state("idle")

    def set_selected(self, sel: bool):
        if sel != self._selected:
            self._selected = sel
            self.update()

    # ---------- 行操作 ----------
    def _popup_menu(self):
        job = self._job
        if job is None:
            return
        menu = QMenu(self)
        menu.addAction("复制链接", lambda: self.copy_requested.emit(job))
        act_open = menu.addAction("打开导出文件", lambda: self.open_requested.emit(job))
        act_open.setEnabled(bool(getattr(job, "export_path", "")))
        menu.addAction("诊断信息…", lambda: self.diagnose_requested.emit(job))
        menu.addSeparator()
        act_retry = menu.addAction("重试", lambda: self.retry_requested.emit(job))
        act_retry.setEnabled(job.status in ("失败", "已停止"))
        menu.addSeparator()
        act_del = menu.addAction("从队列移除", lambda: self.remove_requested.emit(job))
        act_del.setEnabled(job.status != "处理中")
        menu.exec(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft()))

    def contextMenuEvent(self, ev):
        self._popup_menu()
        ev.accept()

    def paintEvent(self, _ev):
        if not self._selected:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(C["sel"]))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 6, 6)
        p.end()
