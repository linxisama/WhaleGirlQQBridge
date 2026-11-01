# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/聊天机器人/qq机器人/qq_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('D:/聊天机器人/qq机器人/styles', 'styles'), ('D:/聊天机器人/qq机器人/src/web/static', 'src/web/static')],
    hiddenimports=['uvicorn', 'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'fastapi', 'src.gui.config', 'src.gui.appearance'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='鲸鱼娘QQ控制台',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='NONE',
)
