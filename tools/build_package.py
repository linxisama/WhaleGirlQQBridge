#!/usr/bin/env python3
"""构建用户可用的发布包。

与仓库里的 build_exe.py 完全同参数（同样的 --onefile/--windowed/--add-data/
--hidden-import），只多两件事：

1. 输出到**独立目录** ``build/dist-clean``，不覆盖正在运行的 ``dist``
   （正在跑的 exe 被 Windows 锁着，覆盖会失败，而且会打断用户的服务）；
2. 显式声明 ``websockets`` 及其子模块 —— 官方 WebSocket 接收靠它，
   PyInstaller 虽然通常能顺着 import 收集，但漏了就会"打包出来收不到 QQ 消息"。

用法：python tools/build_package.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIST = ROOT / "build" / "dist-clean"
OUT_WORK = ROOT / "build" / "work-clean"
NAME = "鲸鱼娘QQ控制台"

HIDDEN = [
    "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "fastapi",
    "src.gui.config", "src.gui.appearance",
    # 官方 WebSocket 接收（src/qq_transports.OfficialQQ.run_websocket）
    "websockets", "websockets.asyncio", "websockets.asyncio.client",
]


def src_submodules() -> list:
    """收集 ``src`` 下所有子模块。

    **这是必须的**：``PluginManager._load_plugins`` 用
    ``importlib.import_module(f".{name}", package="src.plugins")`` **动态**导入插件，
    PyInstaller 的静态分析看不到这些导入，只按 ``qq_gui.py`` 的 import 图收集，
    结果打包出来的 exe **一个插件都没有**（实测解包目录里连 ``src/plugins`` 都没有，
    ``/api/plugins`` 返回空数组）。旧的 ``qq_bot.spec`` 用过 ``collect_submodules('src')``，
    后来换成 ``build_exe.py`` 时漏掉了这一步。
    """
    sys.path.insert(0, str(ROOT))
    from PyInstaller.utils.hooks import collect_submodules

    return collect_submodules("src")


def main() -> int:
    icon = ROOT / "icon.ico"
    # 图标资源没准备好就先跑一次分配脚本（随机但可复现，见 tools/setup_icons.py）。
    # 这样"打包"始终是一条命令，不依赖先后顺序。
    if not icon.is_file() or not (ROOT / "assets/icon-map.json").is_file():
        print("图标资源缺失，先运行 tools/setup_icons.py …")
        result = subprocess.run([sys.executable, str(ROOT / "tools/setup_icons.py")],
                                cwd=str(ROOT))
        if result.returncode != 0:
            print("图标生成失败，继续打包（exe 将使用默认图标）")
    submodules = src_submodules()
    print(f"收集到 src 子模块 {len(submodules)} 个（含插件）")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--name", NAME,
        "--distpath", str(OUT_DIST),
        "--workpath", str(OUT_WORK),
        "--specpath", str(OUT_WORK),
        "--add-data", f"{ROOT / 'styles'};styles",
        "--add-data", f"{ROOT / 'src/web/static'};src/web/static",
        # 界面图标与分配表：冻结后从 BUNDLE 读取（见 qq_gui.slot_icon）
        "--add-data", f"{ROOT / 'assets/icons'};assets/icons",
        "--add-data", f"{ROOT / 'assets/icon-map.json'};assets/icon-map.json",
        "--icon", str(icon) if icon.exists() else "NONE",
    ]
    for item in list(HIDDEN) + sorted(submodules):
        cmd += ["--hidden-import", item]
    cmd.append(str(ROOT / "qq_gui.py"))

    print("构建命令:", " ".join(cmd[:8]), "…")
    result = subprocess.run(cmd, cwd=str(ROOT))
    target = OUT_DIST / f"{NAME}.exe"
    if result.returncode == 0 and target.is_file():
        print(f"\n✅ 构建成功：{target}")
        print(f"   大小：{target.stat().st_size / (1024 * 1024):.1f} MB")
        return 0
    print("\n❌ 构建失败，请查看上面的错误信息")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
