# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（--onedir，GUI 无控制台）。

打包命令（在项目根目录、激活虚拟环境后执行）:
    pyinstaller build.spec

调试构建（需要看控制台报错时，把下面 EXE 的 console 改成 True 再打包）:
    pyinstaller build.spec
"""

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

for _pkg in ("matplotlib", "numpy"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ["app_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "wx",
        "IPython",
        "pandas",
        "scipy",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hapi_transmittance",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="hapi_transmittance",
)
