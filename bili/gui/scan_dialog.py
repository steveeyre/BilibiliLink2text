"""稍后再看 / 收藏夹 扫描对话框（收藏夹支持下钻查看子文件夹内视频）。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QVBoxLayout)

from .. import bili_api
from ..config import Settings
from ..cookies import cookie_header
from ..models import AppState

# 对话框关闭时仍在上网的线程不能随对话框一起析构，否则会 “QThread: Destroyed
# while thread is still running”。脱钩后由模块级集合暂存，跑完自动移除。
_DETACHED: set = set()


def _detach(worker: QThread) -> None:
    _DETACHED.add(worker)
    worker.setParent(None)
    worker.finished.connect(lambda: _DETACHED.discard(worker))


def _fmt_dur(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _ScanWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except bili_api.BiliError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 — 线程里任何异常都要回传，否则静默失败
            self.failed.emit(f"{type(e).__name__}: {e}")


class ScanDialog(QDialog):
    def __init__(self, state: AppState, settings: Settings, parent=None):
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self._urls: list[str] = []
        self._folders: list[dict] = []
        self._view = "folders"            # folders | videos
        self._cur_folder: dict | None = None
        self._worker: _ScanWorker | None = None
        self._gen = 0
        self.setWindowTitle("扫描 B站 列表")
        self.setMinimumSize(520, 460)
        self._build()
        self._refresh()

    def _build(self):
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("列表类型"))
        self.mode = QComboBox()
        self.mode.addItem("稍后再看", "watchlater")
        self.mode.addItem("收藏夹", "fav")
        self.mode.currentIndexChanged.connect(lambda *_: self._refresh())
        row.addWidget(self.mode)
        row.addStretch(1)
        self.nav_btn = QPushButton()
        self.nav_btn.setVisible(False)
        self.nav_btn.clicked.connect(self._nav)
        row.addWidget(self.nav_btn)
        lay.addLayout(row)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.MultiSelection)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.setToolTip("Ctrl / Shift 可多选；双击收藏夹进入其中视频。")
        self.list.itemSelectionChanged.connect(self._update_count)
        lay.addWidget(self.list, 1)
        bottom = QHBoxLayout()
        self.count_label = QLabel()
        bottom.addWidget(self.count_label, 1)
        self.sel_all_btn = QPushButton("全选")
        self.sel_all_btn.clicked.connect(lambda: self._select_all(True))
        self.inv_btn = QPushButton("反选")
        self.inv_btn.clicked.connect(self._invert)
        bottom.addWidget(self.sel_all_btn)
        bottom.addWidget(self.inv_btn)
        lay.addLayout(bottom)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.add_btn = QPushButton("加入队列")
        self.add_btn.setProperty("role", "primary")
        self.add_btn.clicked.connect(self._accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(self.add_btn)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    # ---------- 异步取数 ----------

    def _run_async(self, fn, on_done, on_fail=None):
        self._gen += 1
        gen = self._gen
        w = _ScanWorker(fn, self)
        self._worker = w
        self._busy(True)
        on_fail = on_fail or self._fail

        def _ok(result, _gen=gen):
            if _gen == self._gen:
                on_done(result)

        def _bad(msg, _gen=gen):
            if _gen == self._gen:
                on_fail(msg)

        def _finished(_w=w, _gen=gen):
            if _gen == self._gen:
                self._busy(False)

        w.done.connect(_ok)
        w.failed.connect(_bad)
        w.finished.connect(_finished)
        w.start()

    def _busy(self, busy: bool):
        for w in (self.mode, self.nav_btn, self.add_btn,
                  self.sel_all_btn, self.inv_btn):
            w.setEnabled(not busy)
        if busy:
            self.count_label.setText("加载中…")

    def _fail(self, msg: str):
        QMessageBox.warning(self, "扫描失败", msg)

    def _refresh(self):
        mode = self.mode.currentData()
        self._view = "folders"
        self._cur_folder = None
        self.nav_btn.setVisible(False)
        self.list.clear()
        self._urls = []
        if mode == "watchlater":
            self.hint.setText("将扫描「稍后再看」全部视频，勾选要加入队列的项。")
            self._run_async(
                lambda: bili_api.fetch_watch_later(cookie_header(self.settings)),
                self._show_watch_later)
        else:
            self.hint.setText("选择收藏夹（可多选），双击进入查看其中的视频。")
            self._run_async(
                lambda: bili_api.fetch_fav_folders(cookie_header(self.settings)),
                self._show_folders)

    def _show_watch_later(self, items: list[dict]):
        for it in items:
            self._add_item(it["bvid"], it["title"], it["duration"])
        self.count_label.setText(f"共 {len(items)} 个视频")

    def _show_folders(self, folders: list[dict]):
        self._view = "folders"
        self._cur_folder = None
        self.nav_btn.setVisible(False)
        self.list.clear()
        self._folders = folders
        for f in folders:
            it = QListWidgetItem(f"{f['title']}（{f['media_count']} 个）")
            it.setData(Qt.UserRole, f)
            it.setToolTip(f["title"])
            self.list.addItem(it)
        self.count_label.setText(f"共 {len(folders)} 个收藏夹")

    def _on_double_click(self, item: QListWidgetItem):
        if self._view == "folders" and self.mode.currentData() == "fav":
            folder = item.data(Qt.UserRole)
            if isinstance(folder, dict):
                self._open_folder(folder)

    def _nav(self):
        if self._view != "videos":
            return
        self._view = "folders"
        self._cur_folder = None
        self.list.clear()
        self.nav_btn.setVisible(False)
        self.count_label.setText("加载中…")
        self._run_async(
            lambda: bili_api.fetch_fav_folders(cookie_header(self.settings)),
            self._show_folders)

    def _open_folder(self, folder: dict):
        self.list.clear()
        self.nav_btn.setText("← 返回收藏夹")
        self.nav_btn.setVisible(True)
        self.hint.setText(f"「{folder['title']}」：勾选要加入队列的视频。")
        self._view = "videos"
        self._cur_folder = folder
        self._run_async(
            lambda: bili_api.fetch_fav_list(folder["media_id"], cookie_header(self.settings)),
            lambda videos: self._show_folder_videos(folder, videos))

    def _show_folder_videos(self, folder: dict, videos: list[dict]):
        for v in videos:
            self._add_item(v["bvid"], v["title"], v["duration"])
        text = f"「{folder['title']}」共 {len(videos)} 个视频"
        total = int(folder.get("media_count") or 0)
        if total and len(videos) < total:
            # 接口翻页失败或稿件已失效时会少于收藏夹计数，必须让用户看到
            text += f"（收藏夹显示 {total} 个，可能有失效稿件或翻页受限）"
        self.count_label.setText(text)

    def _add_item(self, bvid, title, duration):
        it = QListWidgetItem(f"{title}  [{bvid}]")
        it.setData(Qt.UserRole, f"https://www.bilibili.com/video/{bvid}")
        tip = [title, f"BV 号：{bvid}"]
        if duration:
            tip.append(f"时长：{_fmt_dur(duration)}")
        it.setToolTip("\n".join(tip))
        self.list.addItem(it)

    # ---------- 选择辅助 ----------

    def _select_all(self, on: bool):
        for i in range(self.list.count()):
            self.list.item(i).setSelected(on)
        self._update_count()

    def _invert(self):
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setSelected(not it.isSelected())
        self._update_count()

    def _selected_items(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count())
                if self.list.item(i).isSelected()]

    def _update_count(self):
        n, total = len(self._selected_items()), self.list.count()
        if self._view == "folders" and self.mode.currentData() != "watchlater":
            self.count_label.setText(f"已选 {n} / {total} 个收藏夹")
        else:
            self.count_label.setText(f"已选 {n} / {total} 个视频")

    # ---------- 收尾 ----------

    def _accept(self):
        mode = self.mode.currentData()
        if mode == "watchlater" or self._view == "videos":
            urls = [it.data(Qt.UserRole) for it in self._selected_items()]
            if not urls:
                QMessageBox.information(self, "提示", "请先勾选要加入队列的视频")
                return
            self._urls = urls
            self.accept()
            return

        folders = [it.data(Qt.UserRole) for it in self._selected_items()]
        if not folders:
            QMessageBox.information(self, "提示", "请先选择收藏夹")
            return

        def _collect():
            ck = cookie_header(self.settings)
            urls = []
            for folder in folders:
                for v in bili_api.fetch_fav_list(folder["media_id"], ck):
                    urls.append(f"https://www.bilibili.com/video/{v['bvid']}")
            return urls

        self.hint.setText(f"正在读取 {len(folders)} 个收藏夹的视频…")
        self._run_async(_collect, self._accept_collected)

    def _accept_collected(self, urls: list[str]):
        if not urls:
            QMessageBox.information(self, "提示", "所选收藏夹没有可用视频")
            return
        self._urls = urls
        self.accept()

    def reject(self):
        self._gen += 1              # 作废在途结果
        w = self._worker
        if w is not None and w.isRunning():
            _detach(w)
        self._worker = None
        super().reject()

    def closeEvent(self, event):
        self.reject()
        super().closeEvent(event)

    def selected_urls(self) -> list[str]:
        return self._urls
