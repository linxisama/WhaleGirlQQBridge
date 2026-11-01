param(
    [Parameter(Mandatory=$true)][string]$CurrentInstaller,
    [string]$OldInstaller = '',
    [Parameter(Mandatory=$true)][string]$ResultDir
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$runId = Get-Date -Format 'yyyyMMdd-HHmmss'
$result = [ordered]@{
    run_id = $runId
    started_at = (Get-Date).ToString('o')
    computer = $env:COMPUTERNAME
    user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    is_admin = (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    os = (Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber | ForEach-Object { "$($_.Caption) $($_.Version) build $($_.BuildNumber)" })
    python_present = [bool](Get-Command python.exe -ErrorAction SilentlyContinue)
    node_present = [bool](Get-Command node.exe -ErrorAction SilentlyContinue)
    steps = @()
    ok = $false
    error = ''
}
New-Item -ItemType Directory -Force -Path $ResultDir | Out-Null
function Add-Step([string]$name, [bool]$passed, [string]$detail = '') {
    $script:result.steps += [ordered]@{ name = $name; passed = $passed; detail = $detail; at = (Get-Date).ToString('o') }
    Write-Host ("[{0}] {1} {2}" -f $(if ($passed) {'PASS'} else {'FAIL'}), $name, $detail)
}
function Stop-AppTree([int]$pidValue) {
    if ($pidValue) { & taskkill.exe /PID $pidValue /T /F 2>$null | Out-Null }
}
function Wait-Console([string]$exe, [int]$seconds = 90, [int]$appPid = 0) {
    $deadline = (Get-Date).AddSeconds($seconds)
    $endpoint = Join-Path $env:LOCALAPPDATA 'WhaleGirlQQ\data\qq-only\console_endpoint.json'
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $endpoint) {
            try {
                $info = Get-Content -LiteralPath $endpoint -Raw -Encoding UTF8 | ConvertFrom-Json
                $url = "http://127.0.0.1:$($info.port)/api/status"
                $status = Invoke-RestMethod -Uri $url -TimeoutSec 3
                if ($null -ne $status) { return @{ ok = $true; port = [int]$info.port; status = $status; endpoint = $endpoint } }
            } catch { Start-Sleep -Milliseconds 500 }
        }
        if ($appPid -and -not (Get-Process -Id $appPid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 700
    }
    return @{ ok = $false; port = 0; status = $null; endpoint = $endpoint }
}
function Install-Setup([string]$installer, [string]$label) {
    $p = Start-Process -FilePath $installer -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER') -Wait -PassThru
    Add-Step "$label setup exit" ($p.ExitCode -eq 0) ("exit=" + $p.ExitCode)
    return $p.ExitCode -eq 0
}
function Get-AppExe {
    return (Join-Path $env:LOCALAPPDATA 'Programs\WhaleGirlQQBridge\WhaleGirlQQBridge.exe')
}
function Get-Uninstaller {
    return (Join-Path $env:LOCALAPPDATA 'Programs\WhaleGirlQQBridge\unins000.exe')
}
try {
    $currentExe = Get-AppExe
    # Phase 1: clean fresh install, first launch, uninstall, reinstall.
    if (Test-Path -LiteralPath $currentExe) { throw "测试机不是干净环境：已存在 $currentExe" }
    if (-not (Install-Setup $CurrentInstaller 'fresh')) { throw '全新安装失败' }
    Add-Step 'fresh exe exists' (Test-Path -LiteralPath $currentExe) $currentExe
    $version = (Get-Item -LiteralPath $currentExe).VersionInfo.FileVersion
    Add-Step 'fresh version' ($version -like '5.2.102*') $version
    $mcp = & $currentExe --mcp check 2>&1 | Out-String
    Add-Step 'mcp check' ($LASTEXITCODE -eq 0 -and $mcp -match 'ok') ($mcp.Trim() -replace '\s+',' ')
    $proc = Start-Process -FilePath $currentExe -PassThru
    $console = Wait-Console $currentExe 90 $proc.Id
    Add-Step 'first launch api' $console.ok ("port=" + $console.port)
    if ($console.ok) {
        Add-Step 'first launch version' ($console.status.version -eq '5.2.102' -or $console.status.app_version -eq '5.2.102') (($console.status | ConvertTo-Json -Compress))
    }
    Stop-AppTree $proc.Id
    Start-Sleep -Seconds 2
    $uninst = Get-Uninstaller
    $up = Start-Process -FilePath $uninst -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -Wait -PassThru
    Add-Step 'uninstall exit' ($up.ExitCode -eq 0) ("exit=" + $up.ExitCode)
    Start-Sleep -Seconds 2
    Add-Step 'uninstall removed exe' (-not (Test-Path -LiteralPath $currentExe)) $currentExe
    if (-not (Install-Setup $CurrentInstaller 'reinstall')) { throw '重装失败' }
    Add-Step 'reinstall version' ((Get-Item -LiteralPath $currentExe).VersionInfo.FileVersion -like '5.2.102*') ((Get-Item -LiteralPath $currentExe).VersionInfo.FileVersion)

    # Phase 2: upgrade from OldInstaller, preserving a user marker file.
    if ($OldInstaller -and (Test-Path -LiteralPath $OldInstaller)) {
        $uninst2 = Get-Uninstaller
        if (Test-Path -LiteralPath $uninst2) {
            Start-Process -FilePath $uninst2 -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -Wait | Out-Null
            Start-Sleep -Seconds 2
        }
        if (-not (Install-Setup $OldInstaller 'old')) { throw '旧版安装失败' }
        $oldVersion = (Get-Item -LiteralPath $currentExe).VersionInfo.FileVersion
        Add-Step 'old version installed' ($oldVersion -notlike '5.2.102*') $oldVersion
        $markerDir = Join-Path $env:LOCALAPPDATA 'WhaleGirlQQ'
        New-Item -ItemType Directory -Force -Path $markerDir | Out-Null
        $marker = Join-Path $markerDir 'upgrade-preserve-marker.txt'
        Set-Content -LiteralPath $marker -Value 'keep-me' -Encoding UTF8
        if (-not (Install-Setup $CurrentInstaller 'upgrade')) { throw '覆盖升级失败' }
        Add-Step 'upgrade version' ((Get-Item -LiteralPath $currentExe).VersionInfo.FileVersion -like '5.2.102*') ((Get-Item -LiteralPath $currentExe).VersionInfo.FileVersion)
        Add-Step 'upgrade kept user data' ((Test-Path -LiteralPath $marker) -and ((Get-Content -LiteralPath $marker -Raw).Trim() -eq 'keep-me')) $marker
        $proc2 = Start-Process -FilePath $currentExe -PassThru
        $console2 = Wait-Console $currentExe 90 $proc2.Id
        Add-Step 'upgrade first launch api' $console2.ok ("port=" + $console2.port)
        Stop-AppTree $proc2.Id
    }
    $result.ok = $true
}
catch {
    $result.error = $_.Exception.Message
    Add-Step 'fatal' $false $result.error
}
finally {
    $result.ended_at = (Get-Date).ToString('o')
    $out = Join-Path $ResultDir ("clean-install-" + $runId + ".json")
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $out -Encoding UTF8
    Write-Host ("RESULT=" + $out)
}