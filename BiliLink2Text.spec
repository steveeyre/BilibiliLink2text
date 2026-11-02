# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: BiliLink2Text (onedir, windowed)
import os

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

datas, binaries, hiddenimports = [], [], []

for pkg in ("yt_dlp", "sherpa_onnx", "onnxruntime"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# 窗口图标：运行时由 config.resource_path("assets/icon.png") 读取
ICON_PNG = os.path.join(SPECPATH, "assets", "icon.png")
if os.path.exists(ICON_PNG):
    datas.append((ICON_PNG, "assets"))
# exe 图标：多尺寸 .ico，缺失时保留默认图标而不是构建失败
ICON_ICO = os.path.join(SPECPATH, "assets", "icon.ico")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "PIL", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliLink2Text",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON_ICO if os.path.exists(ICON_ICO) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BiliLink2Text",
)
