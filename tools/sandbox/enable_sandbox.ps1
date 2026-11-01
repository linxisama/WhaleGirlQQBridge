<#
.SYNOPSIS
    提权启用 Containers-DisposableClientVM（Windows Sandbox）。绝不重启系统。

.DESCRIPTION
    输出单行 JSON，退出码沿用 DISM / Enable-WindowsOptionalFeature 的语义：
    0 成功（通常仍需重启才生效）、3010 需要重启、740 未提权、1223 UAC 被取消。
    DISM 走 Start-Process 拿真实退出码；PowerShell 分支把 HResult 当退出码。

.PARAMETER Feature
    可选功能名，默认 Containers-DisposableClientVM。

.PARAMETER Mode
    DISM（默认，兼容性最好）或 PowerShell（Enable-WindowsOptionalFeature）。

.PARAMETER NoRestart
    保留参数：本脚本无论如何都不会重启系统，加了只是为了让命令自解释。

.PARAMETER ResultPath
    把 JSON 结果另存一份（提权窗口里 stdout 有时拿不到，落文件更稳）。
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
    $Data['schema']      = 1
    $Data['action']      = 'enable'
    $Data['feature']     = $Feature
    $Data['no_restart']  = $true
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
        detail = '未以管理员身份运行：无法启用可选功能，请让调用方走 UAC 提权。' }
    exit 740
}

$current = 'Unknown'
try { $current = [string](Get-WindowsOptionalFeature -Online -FeatureName $Feature -ErrorAction Stop).State } catch { }
if ($current -eq 'Enabled') {
    Write-Result @{ ok = $true; status = 'already_enabled'; reason = ''; exit_code = 0
        needs_reboot = $false; feature_state_after = 'Enabled'
        detail = '可选功能已是 Enabled，无需重复启用。' }
    exit 0
}
if ($current -eq 'Absent') {
    Write-Result @{ ok = $false; status = 'unsupported'; reason = 'feature_absent'
        exit_code = 2147943568; needs_reboot = $false; feature_state_after = 'Absent'
        detail = "本机没有 $Feature 可选功能（家庭版 SKU 不提供），无法启用。" }
    exit 1
}

$code   = 1
$detail = ''
if ($Mode -eq 'PowerShell') {
    try {
        Enable-WindowsOptionalFeature -Online -FeatureName $Feature -All -NoRestart -ErrorAction Stop | Out-Null
        $code = 0
    } catch {
        $detail = [string]$_.Exception.Message
        try { if ($_.Exception.HResult) { $code = [int]$_.Exception.HResult } } catch { }
    }
} else {
    try {
        $dismArgs = @('/online', '/enable-feature', "/featurename:$Feature", '/all', '/norestart')
        $process  = Start-Process -FilePath 'dism.exe' -ArgumentList $dismArgs -Wait -PassThru -NoNewWindow
        $code     = [int]$process.ExitCode
    } catch {
        $detail = [string]$_.Exception.Message
        $code   = 1
    }
}

$after = 'Unknown'
try { $after = [string](Get-WindowsOptionalFeature -Online -FeatureName $Feature -ErrorAction Stop).State } catch { }
$succeeded  = ($code -eq 0 -or $code -eq 3010)
$needsReboot = $false
if ($succeeded) { $needsReboot = ($after -ne 'Enabled') }

$status = 'failed'
$reason = "exit_$code"
if ($succeeded) {
    $status = 'enabled_pending_reboot'
    $reason = ''
} elseif ($code -eq 3010) {
    $status = 'pending_reboot'
    $reason = ''
}

Write-Result @{
    ok                  = $succeeded
    status              = $status
    reason              = $reason
    exit_code           = $code
    needs_reboot        = $needsReboot
    feature_state_before = $current
    feature_state_after = $after
    detail              = ($detail + ' 本脚本不会重启系统。').Trim()
}
if ($succeeded) { exit 0 } else { exit 1 }
