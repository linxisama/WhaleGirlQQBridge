# Installer smoke check: silent install -> self-test the installed app -> uninstall.
#
# Keep this file ASCII-only on purpose. Windows PowerShell 5.1 reads a .ps1 that has no
# UTF-8 BOM as ANSI, and this machine's ANSI codepage is GBK, which turns any Chinese
# text in the script into a parse error. English output here avoids that whole class of
# problem for a developer-only tool.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

$identity = Get-Content (Join-Path $root 'src/runtime_identity.py') -Raw -Encoding UTF8
if ($identity -notmatch "APP_VERSION\s*=\s*'([^']+)'") { throw 'Cannot read APP_VERSION from src/runtime_identity.py' }
$version = $Matches[1]
$setup = Join-Path $root "installer\WhaleGirlQQBridge-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "Installer not found: $setup (run build_installer.ps1 first)" }
Write-Host "Verifying installer: $setup"

$sandbox = Join-Path $root '.tmp-install-test'
$target = Join-Path $sandbox 'WhaleGirlQQBridge'
$smoke = Join-Path $sandbox 'installed-self-test.json'
if (Test-Path $sandbox) { Remove-Item $sandbox -Recurse -Force }
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

Write-Host "== Silent install to $target"
$install = Start-Process -FilePath $setup -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/DIR=$target"
) -Wait -PassThru
if ($install.ExitCode -ne 0) { throw "Install failed with exit code $($install.ExitCode)" }

$exe = Join-Path $target 'WhaleGirlQQBridge.exe'
$unins = Join-Path $target 'unins000.exe'
foreach ($path in @($exe, $unins)) {
    if (-not (Test-Path $path)) { throw "Missing installed file: $path" }
    Write-Host "  present: $(Split-Path $path -Leaf)"
}
$docs = (Get-ChildItem $target -Filter '*.md' -File | Measure-Object).Count
Write-Host "  installed docs: $docs"
if ($docs -lt 2) { throw 'Installed documentation is missing' }
$sizeMb = [math]::Round((Get-ChildItem $target -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "  installed size: $sizeMb MB"

Write-Host '== Self-test of the installed app (offline: no QQ, no model API)'
# The exe is a GUI-subsystem binary: PowerShell does not wait for it with `&` alone,
# so Start-Process -Wait is required or the JSON is read before it exists.
$proc = Start-Process -FilePath $exe -ArgumentList '--self-test', $smoke -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    $err = [System.IO.Path]::ChangeExtension($smoke, '.error.txt')
    if (Test-Path $err) { Get-Content $err -Tail 40 }
    throw "Self-test failed with exit code $($proc.ExitCode)"
}
if (-not (Test-Path $smoke)) { throw "Self-test produced no result file: $smoke" }
$data = Get-Content $smoke -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host ("  self-test ok: frozen={0}, plugins={1}, pc_review={2}, memory_tab={3}" -f `
    $data.frozen, $data.plugins, $data.native_pc_review, $data.native_memory)
Write-Host "  screenshot: $([System.IO.Path]::ChangeExtension($smoke, '.pc.png'))"

Write-Host '== Silent uninstall'
$uninstall = Start-Process -FilePath $unins -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
if ($uninstall.ExitCode -ne 0) { throw "Uninstall failed with exit code $($uninstall.ExitCode)" }
Start-Sleep -Seconds 2
if (Test-Path $exe) { throw 'exe still present after uninstall' }
Write-Host '  program directory removed'

Write-Host ''
Write-Host 'Installer smoke check passed' -ForegroundColor Green
