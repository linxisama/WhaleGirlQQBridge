# 鲸鱼娘 QQ 控制台 · 一键诊断收集
#
# 用途：另一台电脑上"打不开/闪退/报错"时，双击同目录的「收集诊断.cmd」跑这个脚本，
#       它会把**不用启动控制台也能拿到**的证据打包成一个 zip（默认放到桌面），
#       用户只要把这一个文件发回来即可。
#
# 设计约束：
# - 不结束现有用户进程、不改配置、不写安装目录；自检超时只结束本脚本创建的自检进程树。
# - 不依赖 Python：目标电脑可能没装 Python，也可能控制台根本起不来。
# - 每步都单独兜异常：某一项取不到不能影响整份报告。

$ErrorActionPreference = 'Continue'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$work = Join-Path $env:TEMP "whalegirl-diag-$stamp"
$desktop = [Environment]::GetFolderPath('Desktop')
$zip = Join-Path $desktop "鲸鱼娘诊断-$stamp.zip"
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Write-Report([string]$name, [scriptblock]$body) {
    $path = Join-Path $work $name
    try {
        & $body | Out-String -Width 200 | Set-Content -Path $path -Encoding UTF8
        Write-Host "  已收集 $name"
    } catch {
        "收集失败：$($_.Exception.Message)" | Set-Content -Path $path -Encoding UTF8
        Write-Host "  收集 $name 失败：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Write-Host "鲸鱼娘诊断收集中…（不关闭现有程序，不修改配置；只清理超时的本次自检）" -ForegroundColor Cyan

# ── 1. 系统与运行环境 ────────────────────────────────────────────────────────
Write-Report '系统信息.txt' {
    "=== 系统 ==="
    $os = Get-CimInstance Win32_OperatingSystem
    "系统版本      : $($os.Caption) $($os.Version)（Build $($os.BuildNumber)）"
    "系统架构      : $($os.OSArchitecture)"
    "内存(MB)      : $([math]::Round($os.TotalVisibleMemorySize/1KB))"
    "计算机名      : $env:COMPUTERNAME"
    "用户名        : $env:USERNAME"
    "当前时间      : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "PowerShell    : $($PSVersionTable.PSVersion)"
    ""
    "=== 显示与缩放 ==="
    try {
        Add-Type -AssemblyName System.Windows.Forms
        foreach ($screen in [System.Windows.Forms.Screen]::AllScreens) {
            "显示器        : $($screen.DeviceName)  $($screen.Bounds.Width)x$($screen.Bounds.Height)  主屏=$($screen.Primary)"
        }
    } catch { "显示器信息取不到：$($_.Exception.Message)" }
    ""
    "=== Python（本程序自带，不需要系统 Python；这里只作参考） ==="
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    "系统 Python   : $(if ($py) { $py.Source } else { '未安装（正常，本程序不依赖它）' })"
}

# ── 2. 安装情况 ──────────────────────────────────────────────────────────────
Write-Report '安装情况.txt' {
    "=== 已安装版本（注册表） ==="
    $keys = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{4B9B8779-8646-4D84-9082-BC25B9EC688B}_is1',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{4B9B8779-8646-4D84-9082-BC25B9EC688B}_is1',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{4B9B8779-8646-4D84-9082-BC25B9EC688B}_is1'
    )
    $found = $false
    foreach ($key in $keys) {
        $item = Get-ItemProperty $key -ErrorAction SilentlyContinue
        if ($item) {
            $found = $true
            "注册表项      : $key"
            "  显示版本    : $($item.DisplayVersion)"
            "  安装位置    : $($item.InstallLocation)"
        }
    }
    if (-not $found) { "注册表里没有找到本程序的安装记录（可能没装成功，或装的是便携版）" }
    ""
    "=== 安装目录 ==="
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\WhaleGirlQQBridge", "$env:ProgramFiles\WhaleGirlQQBridge")) {
        if (Test-Path $dir) {
            "目录：$dir"
            Get-ChildItem $dir -File -ErrorAction SilentlyContinue |
                Select-Object -First 20 |
                ForEach-Object { "  {0,-32} {1,10:N0} 字节  修改于 {2}" -f $_.Name, $_.Length, $_.LastWriteTime }
            $appFile = Join-Path $dir 'WhaleGirlQQBridge.exe'
            if (Test-Path $appFile) {
                "主程序SHA256 : $((Get-FileHash -LiteralPath $appFile -Algorithm SHA256).Hash)"
            }
        } else {
            "目录不存在：$dir"
        }
    }
    ""
    "=== 快捷方式 ==="
    foreach ($lnk in @((Join-Path $desktop '鲸鱼娘 QQ 控制台.lnk'),
                       (Join-Path ([Environment]::GetFolderPath('Programs')) '鲸鱼娘 QQ 控制台.lnk'))) {
        if (Test-Path $lnk) {
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($lnk)
            "快捷方式      : $lnk"
            "  目标        : $($shortcut.TargetPath)"
            "  图标        : $($shortcut.IconLocation)"
            "  目标存在    : $(Test-Path $shortcut.TargetPath)"
        } else {
            "没有快捷方式：$lnk"
        }
    }
}

# ── 3. 8095 端口与进程 ───────────────────────────────────────────────────────
Write-Report '端口与进程.txt' {
    "=== 8095 端口（控制台后台） ==="
    $conns = Get-NetTCPConnection -LocalPort 8095 -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($conn in $conns) {
            "状态 $($conn.State)  本地 $($conn.LocalAddress):$($conn.LocalPort)  PID $($conn.OwningProcess)"
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc) { "    进程：$($proc.ProcessName)  启动于 $($proc.StartTime)" }
            $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction SilentlyContinue
            if ($cim) { "    路径：$($cim.ExecutablePath)" }
        }
    } else {
        "8095 当前空闲（说明后台没在跑）"
    }
    ""
    "=== 相关进程 ==="
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -match 'WhaleGirlQQBridge' } |
        Select-Object Id, ProcessName, Responding, MainWindowTitle, StartTime |
        Format-Table -AutoSize | Out-String -Width 200
}

# ── 4. 日志（程序自己写的） ──────────────────────────────────────────────────
# 5.x 起日志写在 %LOCALAPPDATA%\WhaleGirlQQ\logs（见 src/startup_supervisor.py:13），
# 更早的版本用过 %LOCALAPPDATA%\WhaleGirlQQBridge\logs；两个候选按顺序取第一个存在的。
$logCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'WhaleGirlQQ\logs'),
    (Join-Path $env:LOCALAPPDATA 'WhaleGirlQQBridge\logs')
)
$logDir = $null
foreach ($candidate in $logCandidates) {
    if (Test-Path $candidate) { $logDir = $candidate; break }
}
Write-Report '日志清单.txt' {
    if ($logDir) {
        "实际日志目录：$logDir"
        ""
        Get-ChildItem $logDir -File | Sort-Object LastWriteTime -Descending |
            ForEach-Object { "{0,-34} {1,10:N0} 字节  {2}" -f $_.Name, $_.Length, $_.LastWriteTime }
    } else {
        "实际日志目录：（无）"
        "两个候选目录都不存在："
        foreach ($candidate in $logCandidates) { "  $candidate" }
        "这只能说明这两个位置没有日志，不能据此断定程序没启动过（也可能日志被清理或写到了别处）。"
    }
    ""
    "=== 崩溃转储（dumps） ==="
    $dumpsDir = $null
    if ($logDir) { $dumpsDir = Join-Path $logDir 'dumps' }
    $dumpFiles = @()
    if ($dumpsDir -and (Test-Path $dumpsDir)) {
        $dumpFiles = @(Get-ChildItem $dumpsDir -File -Filter '*.dmp' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending)
    }
    if ($dumpFiles.Count -gt 0) {
        "dumps 目录：$dumpsDir"
        foreach ($dump in $dumpFiles) { "{0,-34} {1,10:N0} 字节  {2}" -f $dump.Name, $dump.Length, $dump.LastWriteTime }
        "（dmp 本体没有放进 zip，这里只列清单）"
        "如果有 dmp 文件，请单独把它发过来。"
    } elseif ($dumpsDir -and (Test-Path $dumpsDir)) {
        "dumps 目录存在，里面没有 .dmp 文件：$dumpsDir"
    } else {
        "没有 dumps 目录"
    }
}
if ($logDir) {
    # 只收最新的几个 hang 转储：开发机上可能有几百个（测试留下的），
    # 全塞进 zip 会让用户发来的包又大又难翻。
    $latest = Get-ChildItem $logDir -File -Filter 'hang-*.txt' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 5
    foreach ($file in $latest) {
        try { Copy-Item $file.FullName (Join-Path $work "日志-$($file.Name)") -Force } catch {}
    }
    foreach ($name in 'startup.log', 'ui-errors.log', 'native-crash.log', 'startup-stages.jsonl', 'launching.json', 'gui-child.log', 'qt-messages.log', 'desktop-crash.json') {
        $src = Join-Path $logDir $name
        if (Test-Path $src) {
            try { Copy-Item $src (Join-Path $work "日志-$name") -Force } catch {}
        }
    }
    # 只看最后 200 行，避免 zip 太大
    foreach ($name in 'startup.log', 'ui-errors.log', 'native-crash.log', 'startup-stages.jsonl', 'gui-child.log', 'qt-messages.log', 'desktop-crash.json') {
        $src = Join-Path $logDir $name
        if (Test-Path $src) {
            Get-Content $src -Tail 200 -Encoding UTF8 |
                Set-Content (Join-Path $work "尾部-$name") -Encoding UTF8
        }
    }
}

# ── 5. Windows 事件日志里的崩溃/挂起 ────────────────────────────────────────
Write-Report 'Windows事件日志.txt' {
    "=== 最近 3 天与本程序相关的事件 ==="
    try {
        Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddDays(-3)} -ErrorAction Stop |
            Where-Object { $_.Message -match 'WhaleGirlQQBridge' } |
            Select-Object -First 20 |
            ForEach-Object {
                "---- $($_.TimeCreated)  [$($_.LevelDisplayName)]  ID=$($_.Id) ----"
                ($_.Message -split "`n" | Select-Object -First 12) -join "`n"
            }
    } catch {
        "读取事件日志失败：$($_.Exception.Message)"
    }
}

# ── 6. 让程序自己跑一次自检（不需要界面） ──────────────────────────────────
$exe = Join-Path $env:LOCALAPPDATA 'Programs\WhaleGirlQQBridge\WhaleGirlQQBridge.exe'
if (-not (Test-Path $exe)) { $exe = Join-Path $env:ProgramFiles 'WhaleGirlQQBridge\WhaleGirlQQBridge.exe' }
if (Test-Path $exe) {
    $checkPlan = @(
        @{Name='离屏自检'; Args=@('--self-test')},
        @{Name='桌面原生自检'; Args=@('--native-self-test')},
        @{Name='桌面兼容自检'; Args=@('--safe-mode','--native-self-test')}
    )
    foreach ($checkItem in $checkPlan) {
        $selfTest = Join-Path $work ($checkItem.Name + '.json')
        $resultFile = Join-Path $work ($checkItem.Name + '.txt')
        Write-Host "  正在运行 $($checkItem.Name)（单项最多 45 秒）…"
        try {
            $checkArgs = @($checkItem.Args) + @("`"$selfTest`"")
            $proc = Start-Process -FilePath $exe -ArgumentList $checkArgs -PassThru -WindowStyle Hidden
            if (-not $proc.WaitForExit(45000)) {
                # 仅终止上面亲自创建的自检 PID，绝不按进程名批量结束用户程序。
                & "$env:SystemRoot\System32\taskkill.exe" /PID $proc.Id /T /F | Out-Null
                "自检超过45秒，已结束本次自检进程树" | Set-Content $resultFile -Encoding UTF8
            } else {
                $proc.Refresh()
                "自检退出码：$($proc.ExitCode)" | Set-Content $resultFile -Encoding UTF8
            }
            if (Test-Path $selfTest) {
                Get-Content $selfTest -Raw -Encoding UTF8 | Add-Content $resultFile -Encoding UTF8
            }
        } catch {
            "自检启动失败：$($_.Exception.Message)" | Set-Content $resultFile -Encoding UTF8
        }
    }
    if ($logDir) {
        foreach ($name in 'startup.log','native-crash.log','startup-stages.jsonl') {
            $latestLog = Join-Path $logDir $name
            if (Test-Path $latestLog) {
                Get-Content $latestLog -Tail 200 -Encoding UTF8 |
                    Set-Content (Join-Path $work "尾部-$name") -Encoding UTF8
            }
        }
    }
} else {
    "找不到主程序 exe，跳过自检" | Set-Content (Join-Path $work '自检结果.txt') -Encoding UTF8
}

# ── 打包 ─────────────────────────────────────────────────────────────────────
try {
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $work '*') -DestinationPath $zip -Force
    $resolvedWork = [IO.Path]::GetFullPath($work)
    $tempPrefix = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    if ($resolvedWork.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($resolvedWork).StartsWith('whalegirl-diag-')) {
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "收集完成：$zip" -ForegroundColor Green
    Write-Host "请把这个 zip 文件发给作者（QQ 2019117802）即可。" -ForegroundColor Green
    Start-Process explorer.exe "/select,`"$zip`"" -WindowStyle Hidden
} catch {
    Write-Host "打包失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "原始文件留在这里，可以直接发这个文件夹：$work" -ForegroundColor Yellow
}
