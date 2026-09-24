
[CmdletBinding()]
param(
    [switch]$Foreground
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Continue"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$mutexName = "Global\ccpick-autoswitch-tray"
$script:Mutex = New-Object System.Threading.Mutex($false, $mutexName)
if (-not $script:Mutex.WaitOne(0, $false)) {
    if ($Foreground) {
        [System.Windows.Forms.MessageBox]::Show(
            "自动切账号的托盘已经在运行了。", "ccpick autoswitch",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    }
    exit 0
}

$Here    = $PSScriptRoot
$Shell   = Join-Path $Here "claude-account-autoswitch.ps1"
$Shared  = Split-Path -Parent $Here
$HELPER  = Join-Path $Shared "claude-autoswitch-helper.py"
$Root    = Join-Path $env:LOCALAPPDATA "ccpick-autoswitch"
$STATUS  = Join-Path $Root "status.json"
$LOG     = Join-Path $Root "autoswitch.log"
$ACCTS   = Join-Path $Root "accounts.json"      # 账号列表缓存（面板用，全离线）
$TRAYLOG = Join-Path $Root "tray.log"

if (-not (Test-Path -LiteralPath $Root)) {
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
}

function Write-TrayLog([string]$m) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Add-Content -LiteralPath $TRAYLOG -Value $line -Encoding utf8
    if ($Foreground) { Write-Host $line }
}

if (-not $Foreground) {
    Add-Type -Namespace CcpickWin -Name Native -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
[DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr h);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
'@
    $h = [CcpickWin.Native]::GetConsoleWindow()
    if ($h -ne [IntPtr]::Zero) { [CcpickWin.Native]::ShowWindow($h, 0) | Out-Null }
} else {
    Add-Type -Namespace CcpickWin -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr h);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
'@
}

function Resolve-Python {
    $cands = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\pythonw.exe"),
        (Join-Path $env:SystemRoot "pyw.exe")
    )
    foreach ($c in $cands) { if (Test-Path -LiteralPath $c) { return $c } }
    $scan = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\pythonw.exe") `
            -ErrorAction SilentlyContinue | Where-Object { $_.Directory.Name -match '^Python3\d+$' } |
            Sort-Object { [int]($_.Directory.Name.Substring(7)) } -Descending | Select-Object -First 1
    if ($scan) { return $scan.FullName }
    $w = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($w) { return $w.Source }
    $w2 = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($w2) { return $w2.Source }
    return $null
}
$script:PYW = Resolve-Python

function Read-Status {
    $d = [ordered]@{
        state = "unknown"; message = "还没有数据"; extra = ""
        usedPct = $null; win5h = $null; win7d = $null; winModel = $null
        binding = $null; modelName = $null; modelCounted = $null
        cooling = $false; nextCheckS = $null; etaS = $null; burnRate = $null
        activeEmail = ""; threshold = $null; age = [double]::PositiveInfinity
    }
    if (-not (Test-Path -LiteralPath $STATUS)) { return $d }
    try {
        $o = Get-Content -LiteralPath $STATUS -Raw -Encoding utf8 | ConvertFrom-Json
    } catch { return $d }
    foreach ($k in @("state","message","extra","usedPct","win5h","win7d","winModel",
                     "binding","modelName","modelCounted",
                     "cooling","nextCheckS","etaS","burnRate","activeEmail","threshold")) {
        if ($o.PSObject.Properties.Name -contains $k) { $d[$k] = $o.$k }
    }
    if ($o.PSObject.Properties.Name -contains "ts") {
        $now = [System.DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        $d["age"] = $now - [double]$o.ts
    }
    if ($d["age"] -gt 600) { $d["state"] = "stalled" }
    elseif ($d["cooling"] -and $d["state"] -eq "ok") { $d["state"] = "cooling" }
    return $d
}

function Get-IconSpec([string]$state) {
    switch ($state) {
        "ok"       { return @{ bg = [System.Drawing.Color]::FromArgb(52,199,89);  glyph = [char]0x2713 } }
        "switched" { return @{ bg = [System.Drawing.Color]::FromArgb(0,122,255);  glyph = [char]0x21BB } }
        "blocked"  { return @{ bg = [System.Drawing.Color]::FromArgb(255,204,0);  glyph = [char]0x0021 } }
        "error"    { return @{ bg = [System.Drawing.Color]::FromArgb(255,59,48);  glyph = [char]0x00D7 } }
        "offline"  { return @{ bg = [System.Drawing.Color]::FromArgb(255,149,0);  glyph = [char]0x2205 } }
        "cooling"  { return @{ bg = [System.Drawing.Color]::FromArgb(48,176,199); glyph = [char]0x231B } }
        "stalled"  { return @{ bg = [System.Drawing.Color]::FromArgb(175,82,222); glyph = [char]0x003F } }
        default    { return @{ bg = [System.Drawing.Color]::FromArgb(142,142,147);glyph = [char]0x003F } }
    }
}

$script:IconHandle = [IntPtr]::Zero
function New-StateIcon([string]$state) {
    $spec = Get-IconSpec $state
    $size = 32
    $bmp = New-Object System.Drawing.Bitmap($size, $size)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias
    $brush = New-Object System.Drawing.SolidBrush($spec.bg)
    $g.FillEllipse($brush, 1, 1, $size - 2, $size - 2)
    $fg = [System.Drawing.Color]::White
    if ($state -eq "blocked") { $fg = [System.Drawing.Color]::FromArgb(200,30,30) }  # 黄底上白字看不清
    $font = New-Object System.Drawing.Font("Segoe UI Symbol", 17, [System.Drawing.FontStyle]::Bold,
                                           [System.Drawing.GraphicsUnit]::Pixel)
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = [System.Drawing.StringAlignment]::Center
    $fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
    $tb = New-Object System.Drawing.SolidBrush($fg)
    $rect = New-Object System.Drawing.RectangleF(0, 0, $size, $size)
    $g.DrawString([string]$spec.glyph, $font, $tb, $rect, $fmt)
    $g.Dispose(); $brush.Dispose(); $tb.Dispose(); $font.Dispose(); $fmt.Dispose()

    $hicon = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($hicon)
    $bmp.Dispose()
    if ($script:IconHandle -ne [IntPtr]::Zero) {
        [CcpickWin.Native]::DestroyIcon($script:IconHandle) | Out-Null
    }
    $script:IconHandle = $hicon
    return $icon
}

function Get-Headline([string]$state) {
    switch ($state) {
        "ok"       { return "自动切账号：在盯着" }
        "switched" { return "自动切账号：刚切过" }
        "blocked"  { return "全部账号额度用尽" }
        "error"    { return "出错了" }
        "offline"  { return "连不上 claude.ai" }
        "cooling"  { return "刚切过，冷却中" }
        "stalled"  { return "监控停摆（不是额度问题）" }
        default    { return "状态未知" }
    }
}
function Get-AgeText([double]$age) {
    if ([double]::IsInfinity($age)) { return "还没有数据" }
    if ($age -lt 90) { return ("数据 {0:F0} 秒前" -f $age) }
    if ($age -lt 5400) { return ("数据 {0:F0} 分钟前" -f ($age / 60)) }
    return ("数据 {0:F1} 小时前" -f ($age / 3600))
}

function Get-CheckInterval($snap) {
    if ($snap["state"] -eq "switched") { return 45 }
    $n = $snap["nextCheckS"]
    if ($null -ne $n) {
        try {
            $v = [double]$n
            if ($v -gt 0) { return [int][math]::Min([math]::Max($v, 20), 300) }
        } catch {}
    }
    $used = $snap["usedPct"]
    if ($null -eq $used) { return 60 }        # 读不到就按较紧的来
    try {
        $assumedRate = 20.0                    # 百分点/分钟
        $eta = [math]::Max(0, 100 - [double]$used) / $assumedRate * 60
        return [int][math]::Min([math]::Max($eta / 4, 20), 300)
    } catch { return 60 }
}

function Start-Detached([string]$exe, [string]$argLine) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $argLine
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    try { [System.Diagnostics.Process]::Start($psi) | Out-Null }
    catch { Write-TrayLog "WARN 拉不起 $exe $argLine : $($_.Exception.Message)" }
}

function Invoke-Check() {
    $ps = (Get-Process -Id $PID).Path        # 用拉起自己的那个 powershell.exe
    Start-Detached $ps ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Shell)
    $script:LastCheckAt = Get-Date
}

function Update-Accounts() {
    if (-not $script:PYW) { return }
    if (-not (Test-Path -LiteralPath $HELPER)) { return }
    $script:AccountsInFlight = $true
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PYW
    $psi.Arguments = ('"{0}" accounts' -f $HELPER)
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        $t = $p.StandardOutput.ReadToEndAsync()
        if ($p.WaitForExit(20000)) {
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($ACCTS, $t.Result, $utf8NoBom)
        }
    } catch { Write-TrayLog "WARN 读账号列表失败: $($_.Exception.Message)" }
    $script:AccountsInFlight = $false
}

function Read-Accounts {
    if (-not (Test-Path -LiteralPath $ACCTS)) { return $null }
    try { return (Get-Content -LiteralPath $ACCTS -Raw -Encoding utf8 | ConvertFrom-Json) }
    catch { return $null }
}

function Switch-To([string]$email) {
    $cswap = Join-Path $env:USERPROFILE ".local\bin\cswap.exe"
    if (-not (Test-Path -LiteralPath $cswap)) {
        Write-TrayLog "WARN 找不到 cswap"; return
    }
    Write-TrayLog "手动切到 $email"
    Start-Detached $cswap ('switch "{0}"' -f $email)
    Start-Sleep -Milliseconds 1500
    Invoke-Check
}

$script:Notify = New-Object System.Windows.Forms.NotifyIcon
$script:CurState = ""
$script:LastCheckAt = [DateTime]::MinValue
$script:NextIntervalS = 60
$script:AccountsInFlight = $false

$script:Menu = New-Object System.Windows.Forms.ContextMenuStrip
$script:Notify.ContextMenuStrip = $script:Menu
$script:Notify.Visible = $true

$script:ShowMenuMethod = [System.Windows.Forms.NotifyIcon].GetMethod(
    "ShowContextMenu",
    [System.Reflection.BindingFlags]::Instance -bor [System.Reflection.BindingFlags]::NonPublic)

$script:Notify.Add_MouseUp({
    param($s, $e)
    if ($e.Button -ne [System.Windows.Forms.MouseButtons]::Left) { return }
    try {
        if ($null -ne $script:ShowMenuMethod) {
            $script:ShowMenuMethod.Invoke($script:Notify, $null)
        } else {
            $script:Menu.Show([System.Windows.Forms.Control]::MousePosition)
            [CcpickWin.Native]::SetForegroundWindow($script:Menu.Handle) | Out-Null
        }
    } catch {
        Write-TrayLog ("左键弹面板出错: " + $_.Exception.Message)
    }
})

function Add-Info($text) {
    $mi = New-Object System.Windows.Forms.ToolStripMenuItem($text)
    $mi.Enabled = $false
    [void]$script:Menu.Items.Add($mi)
}

function Build-Menu {
    $script:Menu.Items.Clear()
    $snap = Read-Status

    Add-Info (Get-Headline $snap["state"])
    if ($snap["message"]) { Add-Info ("   " + $snap["message"]) }
    if ($snap["extra"])   { Add-Info ("   " + $snap["extra"]) }

    $wins = @()
    if ($null -ne $snap["win5h"])   { $wins += @{ k = "5h"; n = "5 小时"; v = [double]$snap["win5h"] } }
    if ($null -ne $snap["win7d"])   { $wins += @{ k = "7d"; n = "7 天  "; v = [double]$snap["win7d"] } }
    if ($null -ne $snap["winModel"]){
        $mn = "模型周"
        if ($snap["modelName"]) { $mn = "{0} 周" -f $snap["modelName"] }
        if ($snap["modelCounted"] -eq $false) { $mn = $mn + "（不计入）" }
        $wins += @{ k = "model"; n = $mn; v = [double]$snap["winModel"] }
    }
    if ($wins.Count -gt 0) {
        $starKey = $null
        $b = [string]$snap["binding"]
        if ($b) {
            if ($b -eq "5h" -or $b -eq "7d") { $starKey = $b } else { $starKey = "model" }
        } else {
            $cands = @($wins | Where-Object { -not ($_.k -eq "model" -and $snap["modelCounted"] -eq $false) })
            if ($cands.Count -gt 0) {
                $worst = ($cands | ForEach-Object { $_.v } | Measure-Object -Maximum).Maximum
                $starKey = @($cands | Where-Object { $_.v -eq $worst })[0].k
            }
        }
        [void]$script:Menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
        foreach ($w in $wins) {
            $mark = "  "
            if ($w.k -eq $starKey) { $mark = " ★" }
            Add-Info ("{0} {1}  已用 {2:F0}%" -f $mark, $w.n, $w.v)
        }
        Add-Info "   （★＝最紧的一道）"
    }
    Add-Info ("   " + (Get-AgeText $snap["age"]))
    if ($snap["state"] -eq "stalled") {
        Add-Info "   → 用下面「立刻检查一次」试着唤醒"
    }

    [void]$script:Menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
    $acc = Read-Accounts
    if ($null -eq $acc -or -not ($acc.PSObject.Properties.Name -contains "accounts")) {
        Add-Info "  正在读取账号余量…（下次打开就是现成的）"
    } else {
        Add-Info "账号（点账号名即切换）"
        $activeEmail = $snap["activeEmail"]
        foreach ($a in $acc.accounts) {
            $mark = "     "
            if ($a.email -eq $activeEmail -or $a.active) { $mark = "  ●  " }
            $label = $mark + $a.email

            $blocked = $false
            if ($a.PSObject.Properties.Name -contains "blocked") { $blocked = [bool]$a.blocked }
            if ($blocked) {
                $reason = ""
                if ($a.PSObject.Properties.Name -contains "blockedWhy") { $reason = [string]$a.blockedWhy }
                if ($reason.Length -gt 46) { $reason = $reason.Substring(0, 46) }
                Add-Info ("     " + $a.email + "    " + $reason)
                continue
            }

            $mi = New-Object System.Windows.Forms.ToolStripMenuItem($label)
            $mi.Tag = $a.email
            $mi.Add_Click({
                $target = $this.Tag
                try { Switch-To $target }
                catch { Write-TrayLog ("切到 " + $target + " 出错: " + $_.Exception.Message) }
            })
            if ($a.email -eq $activeEmail) { $mi.Enabled = $false }
            [void]$script:Menu.Items.Add($mi)

            if ($a.PSObject.Properties.Name -contains "windows") {
                foreach ($w in $a.windows) {
                    $wname = [string]$w.name
                    if ($wname -eq "5h") { $wlabel = "5 小时" }
                    elseif ($wname -eq "7d") { $wlabel = "7 天  " }
                    else { $wlabel = $wname }
                    $at = ""
                    if ($w.PSObject.Properties.Name -contains "at") { $at = [string]$w.at }
                    if (-not $at -or $at -eq "—") { $at = "—" } else { $at = "$at 恢复" }
                    $flag = ""
                    $counted = $true
                    if ($w.PSObject.Properties.Name -contains "counted") { $counted = [bool]$w.counted }
                    try {
                        $u = [double]$w.used
                        if (-not $counted) { $flag = "  （不计入）" }
                        elseif ($u -ge 95) { $flag = "  用尽" } elseif ($u -ge 80) { $flag = "  快满" }
                    } catch { $u = 0 }
                    Add-Info ("          {0}  已用 {1,3:F0}%   {2}{3}" -f $wlabel, $u, $at, $flag)
                }
            }
        }
    }

    Add-Actions
}

function Add-Actions {
    [void]$script:Menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

    $now = New-Object System.Windows.Forms.ToolStripMenuItem("立刻检查一次")
    $now.Add_Click({
        try { Invoke-Check; Update-Accounts }
        catch { Write-TrayLog ("立刻检查出错: " + $_.Exception.Message) }
    })
    [void]$script:Menu.Items.Add($now)

    $lg = New-Object System.Windows.Forms.ToolStripMenuItem("打开日志")
    $lg.Add_Click({
        try { if (Test-Path -LiteralPath $script:LOGPATH) { Start-Process notepad.exe $script:LOGPATH } }
        catch { Write-TrayLog ("打开日志出错: " + $_.Exception.Message) }
    })
    [void]$script:Menu.Items.Add($lg)

    $q = New-Object System.Windows.Forms.ToolStripMenuItem("退出")
    $q.Add_Click({
        $script:Notify.Visible = $false
        [System.Windows.Forms.Application]::Exit()
    })
    [void]$script:Menu.Items.Add($q)
}

$script:Menu.add_Opening({
    try {
        Build-Menu
    } catch {
        $msg = $_.Exception.Message
        Write-TrayLog ("面板构建失败: " + $msg)
        Write-TrayLog ("  " + $_.ScriptStackTrace)
        try {
            $script:Menu.Items.Clear()
            Add-Info "面板出错了（托盘还在跑）"
            $short = $msg
            if ($short.Length -gt 60) { $short = $short.Substring(0, 60) }
            Add-Info ("   " + $short)
            Add-Info "   详情见「打开日志」旁边的 tray.log"
            Add-Actions
        } catch {
        }
    }
})
$script:LOGPATH = $LOG

$iconTimer = New-Object System.Windows.Forms.Timer
$iconTimer.Interval = 5000
$iconTimer.Add_Tick({
    try { Update-Icon } catch { Write-TrayLog ("图标轮次出错: " + $_.Exception.Message) }
})
$script:LastKnownEmail = $null
$script:NotifiedFile = Join-Path $Root ".notified"

function Show-TrayBalloon([string]$title, [string]$text) {
    try {
        $script:Notify.BalloonTipTitle = $title
        $script:Notify.BalloonTipText = $text
        $script:Notify.ShowBalloonTip(10000)
        Write-TrayLog "通知: $title / $text"
    } catch {
        Write-TrayLog "WARN 通知弹不出来: $($_.Exception.Message)"
    }
}

function Notify-OnChange($snap) {
    $ae = [string]$snap["activeEmail"]
    if (-not $ae) { return }
    if ($null -eq $script:LastKnownEmail) { $script:LastKnownEmail = $ae; return }
    if ($ae -ne $script:LastKnownEmail) {
        $script:LastKnownEmail = $ae
        Show-TrayBalloon "Claude 账号已自动切换" "现在：$ae"
    }
}

$script:SeenAlert = @{}
function Notify-Alerts($snap) {
    $specs = @(
        @{ f = $script:NotifiedFile;                  title = "Claude 全部账号额度用尽" },
        @{ f = ($script:NotifiedFile + ".switchfail"); title = "Claude 自动切号没切成" }
    )
    foreach ($sp in $specs) {
        $ts = [long]0
        if (Test-Path -LiteralPath $sp.f) {
            try { $ts = [long](Get-Content -LiteralPath $sp.f -Raw).Trim() } catch { $ts = [long]0 }
        }
        if (-not $script:SeenAlert.ContainsKey($sp.f)) { $script:SeenAlert[$sp.f] = $ts; continue }
        if ($ts -gt $script:SeenAlert[$sp.f]) {
            $script:SeenAlert[$sp.f] = $ts
            $text = [string]$snap["extra"]
            if (-not $text) { $text = [string]$snap["message"] }
            Show-TrayBalloon $sp.title $text
        } elseif ($ts -lt $script:SeenAlert[$sp.f]) {
            $script:SeenAlert[$sp.f] = $ts          # 文件被删 (恢复正常) ⇒ 下一次失败要能再弹
        }
    }
}

function Update-Icon {
    $snap = Read-Status
    if ($snap["state"] -ne $script:CurState) {
        $script:CurState = $snap["state"]
        $script:Notify.Icon = New-StateIcon $script:CurState
    }
    Notify-OnChange $snap
    Notify-Alerts $snap

    $tip = "{0}`n{1}" -f (Get-Headline $snap["state"]), (Get-AgeText $snap["age"])
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 63) }   # NotifyIcon.Text 硬上限 64
    $script:Notify.Text = $tip

    $script:NextIntervalS = Get-CheckInterval $snap
    $due = $script:LastCheckAt.AddSeconds($script:NextIntervalS)
    if ((Get-Date) -ge $due) {
        Invoke-Check
    }
}
$iconTimer.Start()

$acctTimer = New-Object System.Windows.Forms.Timer
$acctTimer.Interval = 240000
$acctTimer.Add_Tick({
    try { Update-Accounts } catch { Write-TrayLog ("账号刷新出错: " + $_.Exception.Message) }
})
$acctTimer.Start()

[Microsoft.Win32.SystemEvents]::add_PowerModeChanged({
    param($s, $e)
    if ($e.Mode -eq [Microsoft.Win32.PowerModes]::Resume) {
        Write-TrayLog "睡醒，补一轮"
        $script:LastCheckAt = [DateTime]::MinValue
    }
})

[System.Windows.Forms.Application]::add_ThreadException({
    param($s, $e)
    Write-TrayLog ("UI 线程未捕获异常: " + $e.Exception.ToString())
})
[System.AppDomain]::CurrentDomain.add_UnhandledException({
    param($s, $e)
    Write-TrayLog ("进程级未捕获异常: " + $e.ExceptionObject.ToString())
})

Write-TrayLog "托盘启动 (pid=$PID)"
$script:CurState = (Read-Status)["state"]
$script:Notify.Icon = New-StateIcon $script:CurState
try { Notify-Alerts (Read-Status) } catch { Write-TrayLog ("提醒基线出错: " + $_.Exception.Message) }
Invoke-Check
Update-Accounts

try {
    [System.Windows.Forms.Application]::Run()
} finally {
    $iconTimer.Stop(); $acctTimer.Stop()
    $script:Notify.Visible = $false
    $script:Notify.Dispose()
    if ($script:IconHandle -ne [IntPtr]::Zero) {
        [CcpickWin.Native]::DestroyIcon($script:IconHandle) | Out-Null
    }
    $script:Mutex.ReleaseMutex()
    Write-TrayLog "托盘退出"
}
