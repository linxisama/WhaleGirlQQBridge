<#
.SYNOPSIS
    提权禁用 Containers-DisposableClientVM（Windows Sandbox 回滚）。绝不重启系统。

.DESCRIPTION
    输出单行 JSON，退出码语义与 enable_sandbox.ps1 一致：0 成功、3010 需要重启、
    740 未提权、1223 UAC 被取消。禁用后 WindowsSandbox.exe 会在重启后再消失。

.PARAMETER Feature
    可选功能名，默认 Containers-DisposableClientVM。

.PARAMETER Mode
    DISM（默认）或 PowerShell（Disable-WindowsOptionalFeature）。

.PARAMETER NoRestart
    保留参数：本脚本无论如何都不会重启系统。

.PARAMETER ResultPath
    把 JSON 结果另存一份。
#>
[CmdletBinding()]
param(
    [string]$Feature = 'Containers-DisposableClientVM',
    [ValidateSet('DISM', 'PowerShell')][string]$Mode = 'DISM',
    [switch]$NoRestart,
    [string]$ResultPath = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Result {
    param([hashtable]$Data)
    $Data['schema']     = 1
    $Data['action']     = 'disable'
    $Data['feature']    = $Feature
    $Data['no_restart'] = $true
    $json = $Data | ConvertTo-Json -Depth 5 -Compress
    Write-Output $json
    if ($ResultPath) {
        try { Set-Content -LiteralPath $ResultPath -Value $json -Encoding UTF8 -ErrorAction Stop } catch { }
    }
}

$elevated = $false
try {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $elevated  = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { }
if (-not $elevated) {
    Write-Result @{ ok = $false; status = 'needs_elevation'; reason = 'elevation_required'
        exit_code = 740; needs_reboot = $false
        detail = '未以管理员身份运行：无法禁用可选功能，请让调用方走 UAC 提权。' }
    exit 740
}

$current = 'Unknown'
try { $current = [string](Get-WindowsOptionalFeature -Online -FeatureName $Feature -ErrorAction Stop).State } catch { }
if ($current -eq 'Disabled' -or $current -eq 'Absent') {
    Write-Result @{ ok = $true; status = 'already_disabled'; reason = ''; exit_code = 0
        needs_reboot = $false; feature_state_before = $current; feature_state_after = $current
        detail = "可选功能状态是 $current，不需要再禁用。" }
    exit 0
}

$code   = 1
$detail = ''
if ($Mode -eq 'PowerShell') {
    try {
        Disable-WindowsOptionalFeature -Online -FeatureName $Feature -NoRestart -ErrorAction Stop | Out-Null
        $code = 0
    } catch {
        $detail = [string]$_.Exception.Message
        try { if ($_.Exception.HResult) { $code = [int]$_.Exception.HResult } } catch { }
    }
} else {
    try {
        $dismArgs = @('/online', '/disable-feature', "/featurename:$Feature", '/norestart')
        $process  = Start-Process -FilePath 'dism.exe' -ArgumentList $dismArgs -Wait -PassThru -NoNewWindow
        $code     = [int]$process.ExitCode
    } catch {
        $detail = [string]$_.Exception.Message
        $code   = 1
    }
}

$after = 'Unknown'
try { $after = [string](Get-WindowsOptionalFeature -Online -FeatureName $Feature -ErrorAction Stop).State } catch { }
$succeeded   = ($code -eq 0 -or $code -eq 3010)
$needsReboot = $false
if ($succeeded) { $needsReboot = ($after -eq 'Enabled') }

Write-Result @{
    ok                   = $succeeded
    status               = $(if ($succeeded) { 'disabled_pending_reboot' } else { 'failed' })
    reason               = $(if ($succeeded) { '' } else { "exit_$code" })
    exit_code            = $code
    needs_reboot         = $needsReboot
    feature_state_before = $current
    feature_state_after  = $after
    detail               = ($detail + ' 本脚本不会重启系统。').Trim()
}
if ($succeeded) { exit 0 } else { exit 1 }
