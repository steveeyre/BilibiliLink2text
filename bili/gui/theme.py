"""视觉主题：深夜阅读桌（深炭底 · 暖白纸感 · B站粉单点强调）。"""

# 设计标记：整体安静，唯一有辨识度的是队列行内的五段流水线进度条（见 widgets.StageBar）。
# 强调色 #FB7299 取自 B站 品牌粉——这个工具的核心题材就是 B站 视频。
C = {
    "bg": "#14161B",        # 窗口底色（墨黑偏蓝）
    "panel": "#1C1F26",     # 面板
    "raised": "#262A33",    # 悬浮/选中
    "hover": "#2E333D",     # hover 加深
    "border": "#2E333D",    # 细分隔线
    "ink": "#EAE7E0",       # 主文字（暖白纸感）
    "ink_dim": "#D8D6D0",   # 次级文字
    "muted": "#9A9DA6",     # 弱化文字
    "faint": "#868B96",     # 占位符/禁用（panel 上 ≈4.8:1；原 #5A5E67 仅 2.5:1）
    "accent": "#FB7299",    # B站粉：当前阶段/主操作/焦点
    "accent_hover": "#FF8AAB",
    "done": "#5FCB8B",      # 成功/已完成阶段
    "err": "#F2645C",       # 失败
    "warn": "#E8B64C",
    "track": "#2A2E37",     # 阶段条轨道
    "sel": "#2A2F3A",       # 队列行选中底色
    "sunken": "#171A20",    # 品牌栏/分组框等内凹底
    "border_hi": "#3A4049", # hover 边框 / 滚动条把手
    "press": "#333947",     # 按钮按下
    "accent_mute": "#4A3440",      # 主按钮禁用底
    "accent_mute_text": "#8B6B78", # 主按钮禁用文字
}

QSS = f"""
QMainWindow, QDialog {{ background: {C['bg']}; }}
QWidget {{
  color: {C['ink']}; font-size: 13px;
  font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", system-ui, sans-serif;
}}
QDialog {{ padding: 4px; }}

QLabel {{ background: transparent; color: {C['ink']}; }}
QLabel[role="muted"] {{ color: {C['muted']}; }}
QLabel[role="hint"] {{ color: {C['muted']}; font-size: 12px; }}
QLabel[role="chip"] {{
  color: {C['accent']}; background: {C['raised']};
  border: 1px solid {C['border']}; border-radius: 10px;
  padding: 2px 10px; font-size: 11px;
}}
QLabel[role="rowtitle"] {{ color: {C['ink']}; font-size: 13px; }}
/* 状态文字用动态属性 tone 上色，避免每帧 setStyleSheet 覆盖 role */
QLabel[tone="accent"] {{ color: {C['accent']}; }}
QLabel[tone="done"] {{ color: {C['done']}; }}
QLabel[tone="err"] {{ color: {C['err']}; }}
QLabel[tone="muted"] {{ color: {C['muted']}; }}

#brandBar {{ background: {C['sunken']}; border-bottom: 1px solid {C['border']}; }}
#brandTitle {{ color: {C['accent']}; font-size: 16px; font-weight: 700; }}
#brandTag {{ color: {C['muted']}; font-size: 12px; }}

QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
  background: {C['panel']}; border: 1px solid {C['border']};
  border-radius: 6px; padding: 5px 8px; color: {C['ink']};
  selection-background-color: {C['accent']}; selection-color: {C['bg']};
  min-height: 22px;
}}
QLineEdit, QPlainTextEdit {{ placeholder-text-color: {C['faint']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {C['accent']}; }}
/* 禁用态必须肉眼可辨，否则用户会反复点一个点不动的输入框 */
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
  background: {C['bg']}; color: {C['faint']}; border-color: {C['track']};
}}
QLineEdit[readOnly="true"] {{ color: {C['ink_dim']}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{
  image: none;
  width: 0; height: 0;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-top: 6px solid {C['muted']};
  margin-right: 10px;
}}
QComboBox:hover::down-arrow {{ border-top-color: {C['ink_dim']}; }}
QComboBox:focus::down-arrow {{ border-top-color: {C['accent']}; }}
QComboBox QAbstractItemView {{
  background: {C['panel']}; border: 1px solid {C['border']};
  border-radius: 6px; padding: 4px;
  selection-background-color: {C['raised']}; selection-color: {C['accent']};
  outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 18px; background: transparent; border: none; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {C['hover']}; }}
QSpinBox::up-arrow {{
  image: none; width: 0; height: 0;
  border-left: 4px solid transparent; border-right: 4px solid transparent;
  border-bottom: 5px solid {C['muted']};
}}
QSpinBox::down-arrow {{
  image: none; width: 0; height: 0;
  border-left: 4px solid transparent; border-right: 4px solid transparent;
  border-top: 5px solid {C['muted']};
}}
QSpinBox::up-arrow:disabled {{ border-bottom-color: {C['faint']}; }}
QSpinBox::down-arrow:disabled {{ border-top-color: {C['faint']}; }}

QPushButton {{
  background: {C['raised']}; border: 1px solid {C['border']};
  border-radius: 6px; padding: 6px 14px; color: {C['ink']};
}}
QPushButton:hover {{ background: {C['hover']}; border-color: {C['border_hi']}; }}
QPushButton:pressed {{ background: {C['press']}; }}
QPushButton:disabled {{ color: {C['faint']}; background: {C['panel']}; border-color: {C['track']}; }}
QPushButton[role="primary"] {{
  background: {C['accent']}; border-color: {C['accent']}; color: {C['bg']}; font-weight: 600;
}}
QPushButton[role="primary"]:hover {{ background: {C['accent_hover']}; }}
QPushButton[role="primary"]:disabled {{ background: {C['accent_mute']}; color: {C['accent_mute_text']}; border-color: {C['accent_mute']}; }}
QPushButton[role="danger"] {{ color: {C['err']}; }}

QListWidget {{
  background: {C['panel']}; border: 1px solid {C['border']};
  border-radius: 8px; padding: 4px;
}}
QListWidget::item {{ border-radius: 6px; }}
QListWidget::item:selected {{ background: {C['sel']}; }}
QListWidget::item:hover {{ background: {C['raised']}; }}
QListWidget::item:selected:hover {{ background: {C['sel']}; }}

QTabWidget::pane {{
  border: 1px solid {C['border']}; border-radius: 8px;
  background: {C['panel']}; top: -1px;
}}
QTabBar::tab {{
  background: transparent; color: {C['muted']}; padding: 8px 18px;
  border: none; border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {C['accent']}; border-bottom: 2px solid {C['accent']}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ color: {C['ink']}; }}

QTextBrowser {{
  background: transparent; border: none; color: {C['ink_dim']};
  font-size: 14px; selection-background-color: {C['accent']}; selection-color: {C['bg']};
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {C['hover']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {C['border_hi']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {C['hover']}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QGroupBox {{
  border: 1px solid {C['border']}; border-radius: 10px;
  margin-top: 18px; padding: 12px 12px 12px 12px; background: {C['sunken']};
  font-weight: 600;
}}
QGroupBox::title {{
  subcontrol-origin: margin;
  subcontrol-position: top left;
  left: 10px;
  top: 4px;
  padding: 2px 10px;
  /* 粉底必须配深色字：ink 压在 accent 上只有约 1.9:1，几乎不可读 */
  color: {C['bg']};
  background: {C['accent']};
  border: none;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
}}

QStatusBar {{ background: {C['bg']}; color: {C['muted']}; border-top: 1px solid {C['border']}; font-size: 12px; }}
QStatusBar::item {{ border: none; }}

QSplitter::handle {{ background: {C['bg']}; }}
QSplitter::handle:horizontal {{ width: 4px; }}

QMessageBox {{ background: {C['panel']}; }}
QMessageBox QLabel {{ color: {C['ink']}; font-size: 13px; }}
QToolTip {{ background: {C['raised']}; color: {C['ink']}; border: 1px solid {C['border']}; padding: 4px; }}

QCheckBox {{ color: {C['ink_dim']}; spacing: 6px; }}
QCheckBox:disabled {{ color: {C['faint']}; }}
QCheckBox::indicator {{
  width: 16px; height: 16px; border: 1px solid {C['border']};
  border-radius: 4px; background: {C['panel']};
}}
QCheckBox::indicator:hover {{ border-color: {C['border_hi']}; }}
/* 勾选态除填色外还有勾形，色觉障碍用户也能分辨 */
QCheckBox::indicator:checked, QCheckBox::indicator:indeterminate {{
  background: {C['accent']}; border-color: {C['accent']};
  image: url("@CHECK_ICON@");
}}
QCheckBox::indicator:disabled {{ background: {C['bg']}; border-color: {C['track']}; }}
QCheckBox::indicator:checked:disabled {{ background: {C['accent_mute']}; border-color: {C['accent_mute']}; }}

QProgressBar {{
  background: {C['track']}; border: none; border-radius: 3px;
  height: 6px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {C['accent']}; border-radius: 3px; }}
"""


def _check_icon() -> str:
    """生成勾选图标（QSS 只能引用文件路径，不能内联 svg）。失败则退回纯色块。"""
    try:
        from ..config import data_dir
        d = data_dir() / "theme"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "check.svg"
        if not f.exists():
            f.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                'viewBox="0 0 16 16">'
                f'<path d="M3.6 8.4 L6.6 11.4 L12.4 4.8" fill="none" stroke="{C["bg"]}" '
                'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
                encoding="utf-8",
            )
        return f.as_posix()
    except Exception:
        return ""


def apply_theme(app):
    app.setStyle("Fusion")
    app.setStyleSheet(QSS.replace("@CHECK_ICON@", _check_icon()))
