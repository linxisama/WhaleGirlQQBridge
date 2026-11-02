param([string]$Python = '')

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# 构建解释器：显式 -Python 参数 > 环境变量 WHALE_BUILD_PYTHON > 仓库内 .venv-build > 中文报错说明。
# 旧版把默认值写死成 <repo>/.venv-build/Scripts/python.exe，而仓库里并没有这个 venv，
# 于是只报“找不到文件”，不告诉人该怎么办。
if (-not $Python) {
    if ($env:WHALE_BUILD_PYTHON) { $Python = $env:WHALE_BUILD_PYTHON }
    else { $Python = Join-Path $PSScriptRoot '.venv-build/Scripts/python.exe' }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $hint = "找不到构建用的 Python：$Python`n" +
        "构建需要装有 PyInstaller + PyQt6 的 Python 解释器（Qt 固定 PyQt6 6.11.0 / PyQt6-Qt6 6.11.2 / PyQt6-sip 13.13.0）。二选一：`n" +
        "  1) 指向已有的构建 venv（推荐）：`n" +
        "       `$env:WHALE_BUILD_PYTHON = 'D:\聊天机器人\qq机器人\.venv-build\Scripts\python.exe'`n" +
        "       .\build_installer.ps1`n" +
        "     永久生效：setx WHALE_BUILD_PYTHON ""D:\聊天机器人\qq机器人\.venv-build\Scripts\python.exe""`n" +
        "  2) 在本仓库建一个构建专用 venv：`n" +
        "       python -m venv .venv-build`n" +
        "       .venv-build\Scripts\python.exe -m pip install -r requirements.gui.txt pyinstaller meme_generator`n" +
        "   然后直接 .\build_installer.ps1（默认就找 .venv-build）"
    throw $hint
}
$pyDir = [System.IO.Path]::GetDirectoryName($Python)
$venvRoot = [System.IO.Path]::GetDirectoryName($pyDir)
Write-Host "Build python: $Python"

# Do not collect unrelated DLLs from tools injected into the parent PATH.
$qtBin = Join-Path $venvRoot 'Lib/site-packages/PyQt6/Qt6/bin'
$pathParts = @($pyDir)
if (Test-Path -LiteralPath $qtBin) { $pathParts += $qtBin }
$pathParts += @((Join-Path $env:SystemRoot 'System32'), $env:SystemRoot)
$env:PATH = ($pathParts -join ';')

# Version comes from the code (single source of truth), not from a hard-coded string here.
# Before this the script was pinned to one version (dist/1.5.0, styles-1.5.0) while the code
# moved on, so a rebuild could quietly ship an installer built from a stale dist path.
$identity = Get-Content (Join-Path $PSScriptRoot 'src/runtime_identity.py') -Raw -Encoding UTF8
if ($identity -notmatch "APP_VERSION\s*=\s*'([^']+)'") { throw 'Cannot read APP_VERSION from src/runtime_identity.py' }
$version = $Matches[1]
Write-Host "Building WhaleGirl QQ $version"

& $Python tools/check_desktop_runtime.py
if ($LASTEXITCODE -ne 0) { throw 'Desktop runtime version check failed' }

$bundleStyles = Join-Path $PSScriptRoot ".build-tools/styles-$version"
New-Item -ItemType Directory -Path $bundleStyles -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'styles') -Filter '*.yaml' -File | Copy-Item -Destination $bundleStyles
$translationPath = (& $Python -c "from pathlib import Path; import json, PyQt6; print(json.dumps(str(Path(PyQt6.__file__).parent / 'Qt6/translations/qtbase_zh_CN.qm')))" ) | ConvertFrom-Json

# EXE 版本资源：PyInstaller 只有拿到 --version-file 才会写 RT_VERSION 段。之前命令行只有 --icon，
# 所以打出来的 exe 属性页里 文件版本/产品版本/产品名称/公司 全是空的。
# 版本用上面从 src/runtime_identity.py 读到的 $version（与安装包 AppVersion、dist 目录同源），
# 不传 --version 时生成器自己读 version.txt。产物落 .build-tools：构建中间产物，不进仓库根也不进 dist。
$versionInfoFile = Join-Path $PSScriptRoot ".build-tools/version_info_$version.txt"
& $Python (Join-Path $PSScriptRoot '.build-tools/make_version_file.py') --version $version --out $versionInfoFile
if ($LASTEXITCODE -ne 0) { throw 'Version resource generation failed' }
if (-not (Test-Path -LiteralPath $versionInfoFile -PathType Leaf)) { throw "版本资源脚本没有生成：$versionInfoFile" }
Write-Host "Version resource: $versionInfoFile"

# --onedir：程序目录展开成「WhaleGirlQQBridge.exe + _internal\」，代码改一行只换变化的那几个
# 文件（tools/build_update.py 靠它做轻量更新包）；--onefile 每次都要整包重下 70+MB，做不出增量。
& $Python -m PyInstaller --noconfirm --clean --onedir --windowed --name WhaleGirlQQBridge --distpath "dist/$version" --workpath build/installer-app --specpath .build-tools --icon "$PSScriptRoot/icon.ico" --version-file "$versionInfoFile" --add-data "$PSScriptRoot/icon.ico;." --add-data "$PSScriptRoot/assets;assets" --add-data "$PSScriptRoot/runtime/qqagent-host;runtime/qqagent-host" --add-data "$PSScriptRoot/runtime/node;runtime/node" --add-data "$PSScriptRoot/plugin_packages;plugin_packages" --add-data "$bundleStyles;styles" --add-data "$PSScriptRoot/src/web/static;src/web/static" --add-data "$translationPath;Qt6/translations" --collect-submodules src.plugins --collect-submodules plugin_core --collect-all meme_generator --hidden-import src.gui.smoke --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on --hidden-import src.dsh_mcp_entry --hidden-import tools.whale_memory_mcp --hidden-import tools.whale_bot_mcp --hidden-import tools.whale_tools_mcp gui_launcher.py
if ($LASTEXITCODE -ne 0) { throw 'Application build failed' }

$bundleDir = Join-Path $PSScriptRoot "dist/$version/WhaleGirlQQBridge"
$bundleExe = Join-Path $bundleDir 'WhaleGirlQQBridge.exe'
if (-not (Test-Path -LiteralPath $bundleExe -PathType Leaf)) { throw "onedir 产物缺失：$bundleExe" }
# 使用说明随安装包落到程序目录，用户不用回仓库找。
$docsTarget = Join-Path $bundleDir 'docs'
if (Test-Path -LiteralPath $docsTarget) { Remove-Item -LiteralPath $docsTarget -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'docs') -Destination $bundleDir -Recurse -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $bundleDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'BUILDING.md') -Destination $bundleDir -Force
$bundleFiles = @(Get-ChildItem -LiteralPath $bundleDir -Recurse -File)
Write-Host ("Bundle: dist/$version/WhaleGirlQQBridge  files=" + $bundleFiles.Count + "  exe=" + (Get-Item -LiteralPath $bundleExe).Length + " B")

& .build-tools/InnoSetup/ISCC.exe "/DAppVersion=$version" installer.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
Write-Host "Installer: installer/WhaleGirlQQBridge-Setup-$version.exe"
