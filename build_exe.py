#!/usr/bin/env python3
"""打包 qq_gui.py 为单文件 exe（Windows）。
运行方式：python build_exe.py
需要：pip install pyinstaller
"""
import subprocess, sys, shutil
from pathlib import Path

ROOT = Path(__file__).parent

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",          # 单文件
    "--windowed",         # 无控制台窗口
    "--name", "鲸鱼娘QQ控制台",
    "--add-data", f"{ROOT / 'styles'};styles",          # 人格文件
    "--add-data", f"{ROOT / 'src/web/static'};src/web/static",  # Web 静态文件（后端用）
    "--icon", str(ROOT / "icon.ico") if (ROOT / "icon.ico").exists() else "NONE",
    "--hidden-import", "uvicorn",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.protocols",
    "--hidden-import", "uvicorn.protocols.http",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "fastapi",
    # 外观/配置模块：虽然 PyInstaller 能顺着 import 收集，显式声明避免哪天改成
    # 动态导入后打包出缺模块的 exe。
    "--hidden-import", "src.gui.config",
    "--hidden-import", "src.gui.appearance",
    str(ROOT / "qq_gui.py"),
]

print("Running PyInstaller...")
result = subprocess.run(cmd, cwd=str(ROOT))
if result.returncode == 0:
    print(f"\n✅ 打包成功！exe 位于: {ROOT / 'dist' / '鲸鱼娘QQ控制台.exe'}")
else:
    print("\n❌ 打包失败，请查看上面的错误信息")
