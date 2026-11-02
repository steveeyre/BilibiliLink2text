"""BiliLink2Text 入口。"""
import logging
import sys
import tempfile
from pathlib import Path

from PySide6.QtWidgets import QApplication

from bili import __version__
from bili.config import load_settings, log_file, save_settings
from bili.gui.local_model_dialog import LocalModelDialog
from bili.gui.main_window import MainWindow
from bili.gui.theme import apply_theme
from bili.local_models import apply_local_model, pick_default, scan_local_models
from bili.models import AppState


def _setup_logging() -> None:
    """窗口化构建没有控制台，异常必须落到 data/logs/app.log 才可排查。"""
    try:
        f = log_file()
        handler = logging.FileHandler(f, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except Exception:
        return

    def _hook(exc_type, exc, tb):
        logging.getLogger("bili").critical("未捕获异常", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


def _install_qt_log_handler() -> None:
    """把 Qt 自身的警告/错误也写进同一份日志。"""
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    log = logging.getLogger("qt")

    def _handler(mode, _ctx, msg):
        log.log(levels.get(mode, logging.INFO), msg)

    qInstallMessageHandler(_handler)


def _selftest() -> int:
    """无头自检：验证 ASR 引擎能加载并转写一段短音频，结果写入临时文件。"""
    import struct
    import wave
    from bili.asr_sherpa import SherpaASR

    out = Path(tempfile.gettempdir()) / "bililink_selftest.txt"
    try:
        wav = Path(tempfile.gettempdir()) / "bililink_selftest.wav"
        with wave.open(str(wav), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(struct.pack("<8000h", *([0] * 8000)))
        eng = SherpaASR()
        eng.ensure_ready()
        segs = eng.transcribe(wav)
        eng.release()
        out.write_text(f"SELFTEST OK v{__version__} engine={eng.engine_label} "
                       f"segments={len(segs)}", encoding="utf-8")
        return 0
    except Exception as e:  # noqa: BLE001
        out.write_text(f"SELFTEST FAIL: {e!r}", encoding="utf-8")
        return 1


def _maybe_pick_local_model(app: QApplication, settings) -> None:
    """初始化时扫描 .ollama/models，有模型则让用户选一款（可记住，不再询问）。"""
    if settings.local_model_picked:
        return
    models = scan_local_models()
    if not models:
        return
    dlg = LocalModelDialog(models, preferred=pick_default(models))
    if dlg.exec() and dlg.chosen:
        apply_local_model(settings, dlg.chosen)
        if dlg.should_remember:
            settings.local_model_picked = True
        save_settings(settings)


def main():
    _setup_logging()
    app = QApplication(sys.argv)
    _install_qt_log_handler()
    app.setApplicationName("BiliLink2Text")
    app.setApplicationVersion(__version__)
    apply_theme(app)
    settings = load_settings()
    _maybe_pick_local_model(app, settings)
    state = AppState()
    win = MainWindow(state, settings)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    main()
