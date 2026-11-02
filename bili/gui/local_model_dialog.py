"""初始化时的本地大模型选择对话框。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)

from ..local_models import LocalModel


class LocalModelDialog(QDialog):
    """列出 .ollama/models 等目录里的可用模型，让用户挑一款。"""

    def __init__(self, models: list[LocalModel], preferred: Optional[LocalModel] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择本地大模型")
        self.setMinimumSize(520, 420)
        self._chosen: Optional[LocalModel] = None
        self._models = models

        lay = QVBoxLayout(self)
        title = QLabel("检测到本机已有大模型，请选择要调用的模型：")
        title.setWordWrap(True)
        lay.addWidget(title)

        self.listw = QListWidget()
        for m in models:
            item = QListWidgetItem(m.display)
            item.setData(Qt.UserRole, m.key)
            item.setToolTip(f"{m.name}\n来源：Ollama\n模型名：{m.path}")
            self.listw.addItem(item)
        lay.addWidget(self.listw, 1)

        if preferred:
            for i in range(self.listw.count()):
                if self.listw.item(i).data(Qt.UserRole) == preferred.key:
                    self.listw.setCurrentRow(i)
                    break
            else:
                self.listw.setCurrentRow(0)
        elif self.listw.count():
            self.listw.setCurrentRow(0)

        self.remember = QCheckBox("记住本次选择，下次不再询问")
        self.remember.setChecked(True)
        lay.addWidget(self.remember)

        tip = QLabel("仅列出 Ollama 已安装模型。若列表为空，请先运行 installLocalLLM.bat 安装模型。"
                     if models else
                     "未检测到任何 Ollama 模型。请先运行 installLocalLLM.bat，"
                     "或点「稍后在设置中配置」改用在线 API。")
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
        lay.addWidget(tip)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self._ok = QPushButton("使用所选模型")
        self._ok.setProperty("role", "primary")
        self._ok.setEnabled(bool(models))
        self._ok.clicked.connect(self._accept)
        cancel = QPushButton("稍后在设置中配置")
        cancel.clicked.connect(self.reject)
        btns.addWidget(self._ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

        self.listw.itemDoubleClicked.connect(lambda *_: self._accept())

    @property
    def chosen(self) -> Optional[LocalModel]:
        return self._chosen

    @property
    def should_remember(self) -> bool:
        return self.remember.isChecked()

    def _accept(self):
        row = self.listw.currentRow()
        if not (0 <= row < len(self._models)):
            return  # 无有效选择时保持对话框打开，避免静默返回空结果
        self._chosen = self._models[row]
        self.accept()
