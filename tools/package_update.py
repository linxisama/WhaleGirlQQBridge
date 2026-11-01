#!/usr/bin/env python3
"""把新版单文件程序打成“简化更新包”。

只替换已安装目录里的 WhaleGirlQQ.exe 和 version.txt，保留配置、数据、插件和日志；
不再生成完整安装器。用法：
    python tools/package_update.py 5.2.41
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLY_PS1 = r"""$ErrorActionPreference = 'Stop'
$target = Join-Path $env:LOCALAPPDATA 'Programs\WhaleGirlQQ'
if (-not (Test-Path -LiteralPath (Join-Path $target 'WhaleGirlQQ.exe'))) {
    throw "没有找到已安装的 WhaleGirlQQ：$target"
}
for ($i = 0; $i -lt 20; $i++) {
    $running = @(Get-Process -Name WhaleGirlQQ -ErrorAction SilentlyContinue)
    if ($running.Count -eq 0) { break }
    $running | ForEach-Object {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $_.Id /T /F | Out-Null
    }
    Start-Sleep -Milliseconds 500
}
if (@(Get-Process -Name WhaleGirlQQ -ErrorAction SilentlyContinue).Count -gt 0) {
    throw "WhaleGirlQQ 仍在运行，请关闭控制台后再更新。"
}
$exe = Join-Path $target 'WhaleGirlQQ.exe'
$backup = Join-Path $target 'WhaleGirlQQ.previous.exe'
Copy-Item -LiteralPath $exe -Destination $backup -Force
$copied = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'WhaleGirlQQ.exe') -Destination $exe -Force -ErrorAction Stop
        $copied = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $copied) { throw "程序文件仍被占用，更新未完成；旧版本保留为 WhaleGirlQQ.previous.exe。" }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'version.txt') -Destination (Join-Path $target 'version.txt') -Force
Start-Process -FilePath $exe
Write-Host "更新完成，已启动 WhaleGirlQQ。旧版本保留为 WhaleGirlQQ.previous.exe。"
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else ''
    if not version:
        print('usage: package_update.py <version>')
        return 2
    exe = ROOT / 'dist' / version / 'WhaleGirlQQ.exe'
    if not exe.is_file():
        print(f'missing built exe: {exe}')
        return 1

    out_dir = ROOT / 'release' / f'WhaleGirlQQ-Update-{version}'
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    shutil.copy2(exe, out_dir / 'WhaleGirlQQ.exe')
    (out_dir / 'version.txt').write_text(version + '\n', encoding='ascii')
    (out_dir / '应用更新.ps1').write_text(APPLY_PS1, encoding='utf-8-sig')
    (out_dir / '应用更新.cmd').write_text(
        '@echo off\r\n'
        'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0应用更新.ps1"\r\n'
        'pause\r\n',
        encoding='gbk')
    (out_dir / '更新说明.txt').write_text(
        f'WhaleGirlQQ {version} 简化更新包\n'
        f'适用于已安装的 WhaleGirlQQ（保留原配置和数据）。\n'
        f'解压后双击“应用更新.cmd”（或右键“应用更新.ps1” → 使用 PowerShell 运行）；\n'
        f'只替换程序文件和版本号，保留配置、数据、插件、日志。\n'
        f'旧程序会备份为 WhaleGirlQQ.previous.exe，可手动回滚。\n',
        encoding='utf-8')
    archive = ROOT / 'release' / f'WhaleGirlQQ-Update-{version}.zip'
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for item in sorted(out_dir.iterdir()):
            zf.write(item, item.name)
    print(f'update: {archive}')
    print(f'size: {archive.stat().st_size}')
    print(f'sha256: {sha256(archive)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
