# 构建说明

## 环境

- Windows 10/11 x64
- Python 3.12（构建 venv 需要 PyInstaller、PyQt6 6.11.0、PyQt6-Qt6 6.11.2、PyQt6-sip 13.13.0）
- Node.js 20+（仅用于准备 runtime/node 兜底；不装也能构建，但干净机器上的插件宿主需要它）

## 版本号

同时改三处，保持完全一致：

1. version.txt（UTF-8 无 BOM，建议保留原换行风格）
2. src/runtime_identity.py 的 APP_VERSION
3. tests/test_build_metadata.py 的 VERSION / FULL_VERSION

## 源码运行

```powershell
python -m venv .venv-qq
.venv-qq\Scripts\python.exe -m pip install -r requirements.qq.txt -r requirements.gui.txt
.venv-qq\Scripts\python.exe qq_main.py
```

## 构建安装版

build_installer.ps1 调用 PyInstaller 生成 dist/<version>/WhaleGirlQQBridge/，再调用 Inno Setup 生成 installer/WhaleGirlQQBridge-Setup-<version>.exe。

```powershell
$env:WHALE_BUILD_PYTHON = 'D:\path\to\.venv-build\Scripts\python.exe'
powershell -ExecutionPolicy Bypass -File .\build_installer.ps1
```

注意：

- --add-data 必须包含 runtime/qqagent-host、runtime/node、plugin_packages、tools/sandbox、styles、src/web/static 和 Qt 翻译。漏掉 runtime/qqagent-host 会导致安装版插件能力桥永远未就绪。
- 构建脚本使用 installer.iss 的 AppVersion，安装包文件名和注册表 DisplayVersion 都取自它。
- 构建前先跑完整测试。

## 生成更新包

```powershell
.venv-qq\Scripts\python.exe tools\build_update.py --old dist\5.2.101\WhaleGirlQQBridge --new dist\5.2.102\WhaleGirlQQBridge --version 5.2.102 --base 5.2.101 --out installer\WhaleGirlQQBridge-Update-5.2.101-to-5.2.102.zip
```

更新包只包含变化的文件和删除清单；安装版自动更新会下载 Setup .exe，更新包用于手动升级。

## 发布 Release

1. 创建 tag v<version>。
2. 上传 WhaleGirlQQBridge-Setup-<version>.exe、WhaleGirlQQBridge-Update-<old>-to-<version>.zip。
3. 为每个资产生成 .sha256 文件并上传。
4. Release 说明写清版本、主要改动、校验值和回滚方法。
5. 确认控制台「关于作者 → 自动更新」能从该 Release 检查到新版本。

## 校验

```powershell
Get-FileHash .\installer\WhaleGirlQQBridge-Setup-5.2.102.exe -Algorithm SHA256
```

安装器使用 Inno Setup 6；卸载程序为 unins000.exe，支持 /VERYSILENT /SUPPRESSMSGBOXES /NORESTART。
