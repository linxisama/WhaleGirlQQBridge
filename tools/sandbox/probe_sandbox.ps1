<#
.SYNOPSIS
    只读探测 Windows Sandbox 支持情况，输出单行 JSON。不需要管理员。

.DESCRIPTION
    state 只可能是 Enabled / Disabled / Absent / Unknown 之一。
    非管理员时 Get-WindowsOptionalFeature 会因缺少提权而失败，此时退到 CBS 注册表
    包键 + WindowsSandbox.exe 的存在性推断，并把 state_source 一起报出来，让调用方
    自己判断可信度——宁可报 Unknown，也不要把"没查到"当成"没启用"。

.PARAMETER Feature
    可选功能名，默认 Containers-DisposableClientVM（Windows Sandbox）。

.PARAMETER SandboxExe
    WindowsSandbox.exe 路径，默认 %SystemRoot%\System32\WindowsSandbox.exe。
#>
[CmdletBinding()]
param(
    [string]$Feature = 'Containers-DisposableClientVM',
    [string]$SandboxExe = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$state    = 'Unknown'
$source   = 'unavailable'
$detail   = ''
$elevated = $false

try {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $elevated  = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch {
    $detail = "身份探测失败：$($_.Exception.Message)"
}

if (-not $SandboxExe) { $SandboxExe = Join-Path $env:SystemRoot 'System32\WindowsSandbox.exe' }
$exePresent = $false
try { $exePresent = Test-Path -LiteralPath $SandboxExe -PathType Leaf } catch { }

if ($elevated) {
    try {
        $featureInfo = Get-WindowsOptionalFeature -Online -FeatureName $Feature -ErrorAction Stop
        $state  = [string]$featureInfo.State
        $source = 'optional-feature'
    } catch {
        $detail = "Get-WindowsOptionalFeature 失败：$($_.Exception.Message)"
    }
} else {
    $detail = '非管理员：Get-WindowsOptionalFeature 需要提权，改用 CBS 注册表包与 exe 探测。'
}

if ($source -eq 'unavailable') {
    try {
        $cbsKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\Packages'
        $packages = @(Get-ChildItem -LiteralPath $cbsKey -ErrorAction Stop |
            Where-Object { $_.PSChildName -like "*$Feature*" })
        if ($packages.Count -gt 0) {
            if ($exePresent) { $state = 'Enabled' } else { $state = 'Disabled' }
            $source = 'cbs-package'
        } else {
            $state  = 'Absent'
            $source = 'cbs-package'
        }
    } catch {
        $state  = 'Unknown'
        $source = 'unavailable'
        $detail = ($detail + ' ' + "CBS 注册表探测失败：$($_.Exception.Message)").Trim()
    }
}

$os = [ordered]@{ product_name = ''; edition_id = ''; display_version = ''; build = ''; release = '' }
try {
    $currentVersion = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop
    $os.product_name    = [string]$currentVersion.ProductName
    $os.edition_id      = [string]$currentVersion.EditionID
    $os.display_version = [string]$currentVersion.DisplayVersion
    $os.build           = [string]$currentVersion.CurrentBuildNumber
    $os.release         = [string]$currentVersion.ReleaseId
} catch {
    $detail = ($detail + ' ' + "读取系统版本失败：$($_.Exception.Message)").Trim()
}

$payload = [ordered]@{
    schema   = 1
    feature  = [ordered]@{ name = $Feature; state = $state; state_source = $source; detail = $detail }
    os       = $os
    elevated = $elevated
    exe      = [ordered]@{ present = $exePresent; path = $SandboxExe }
}

$payload | ConvertTo-Json -Depth 5 -Compress
exit 0
