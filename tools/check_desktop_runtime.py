"""打包前拒绝漂移的 Qt 运行库，避免重新引入未验收的桌面底层。"""
import importlib.metadata
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.desktop_runtime import QT_PACKAGES


def main():
    for package, expected in QT_PACKAGES.items():
        actual = importlib.metadata.version(package)
        if actual != expected:
            raise RuntimeError(f'{package}={actual}，当前桌面构建要求 {expected}，请按 requirements.gui.txt 安装')
    from PyQt6.QtCore import qVersion, QLibraryInfo
    if qVersion() != QT_PACKAGES['PyQt6-Qt6']:
        raise RuntimeError('实际加载的 Qt DLL 不是固定版本')
    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    if sys.platform == 'win32' and not (plugins / 'platforms/qwindows.dll').is_file():
        raise RuntimeError('缺少 Windows 桌面平台插件')
    print('Desktop runtime pins verified:', qVersion())


if __name__ == '__main__':
    main()
