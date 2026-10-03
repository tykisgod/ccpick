
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
$WATCH_ONLY = Join-Path $Root "claude-autoswitch.watch-only"

if (-not (Test-Path -LiteralPath $Root)) {
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
}

function Write-TrayLog([string]$m) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Add-Content -LiteralPath $TRAYLOG -Value $line -Encoding utf8
    if ($Foreground) { Write-Host $line }
}

$script:ParentConhost = $null
try {
    $ppid = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
    $pp = Get-Process -Id $ppid -ErrorAction Stop
    if ($pp.ProcessName -eq "conhost") { $script:ParentConhost = @{ Id = $pp.Id; Start = $pp.StartTime } }
} catch { }
$script:Exiting = $false

function Test-ParentGone {
    if (-not $script:ParentConhost) { return $false }
    try {
        $p = Get-Process -Id $script:ParentConhost.Id -ErrorAction Stop
        return ($p.StartTime -ne $script:ParentConhost.Start)
    } catch { return $true }
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
        activeEmail = ""; threshold = $null; decisionAction = $null; age = [double]::PositiveInfinity
    }
    if (-not (Test-Path -LiteralPath $STATUS)) { return $d }
    try {
        $o = Get-Content -LiteralPath $STATUS -Raw -Encoding utf8 | ConvertFrom-Json
    } catch { return $d }
    foreach ($k in @("state","message","extra","usedPct","win5h","win7d","winModel",
                     "binding","modelName","modelCounted",
                     "cooling","nextCheckS","etaS","burnRate","activeEmail","threshold","decisionAction")) {
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
        "advise"   { return @{ bg = [System.Drawing.Color]::FromArgb(255,149,0);  glyph = [char]0x21C4 } }
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

function Get-Headline([string]$state, [string]$message = "") {
    if ($state -eq "blocked" -and $message.StartsWith("切换没成功")) { return "自动切号没切成" }
    if ($state -eq "blocked" -and ($message.StartsWith("当前号疑似被封") -or $message.StartsWith("原号疑似被封"))) {
        return "账号疑似被封"
    }
    if ($state -eq "ok" -and $message.StartsWith("只看不切")) { return "只看不切：在盯着（自动切号已关）" }
    switch ($state) {
        "advise"   { return "该换号了（自动切号已关）" }
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
    if ($script:AccountsInFlight -or -not $script:PYW) { return }
    if (-not (Test-Path -LiteralPath $HELPER)) { return }
    $script:AccountsInFlight = $true
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PYW
    $psi.Arguments = ('"{0}" accounts' -f $HELPER)
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.RedirectStandardError = $true
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        $identity = $null
        try { $identity = @{ id = $p.Id; start = $p.StartTime.ToUniversalTime().Ticks } } catch { }
        $script:AccountsRefresh = @{ process = $p; output = $p.StandardOutput.ReadToEndAsync();
            error = $p.StandardError.ReadToEndAsync(); startedAt = [DateTime]::UtcNow; identity = $identity }
    } catch { $script:AccountsInFlight = $false; Write-TrayLog 'WARN 账号列表刷新未开始' }
}

function Stop-AccountsRefreshWorker($pending) {
    if (-not $pending.identity -or $pending.process.HasExited -or $pending.process.Id -ne $pending.identity.id) { return }
    $current = $null
    try {
        $current = Get-Process -Id $pending.identity.id -ErrorAction Stop
        if ($current.StartTime.ToUniversalTime().Ticks -eq $pending.identity.start) { $pending.process.Kill() }
    } catch { }
    finally { if ($current) { $current.Dispose() } }
}

function Complete-AccountsRefresh {
    if (-not $script:AccountsRefresh) { return }
    if (-not $script:AccountsRefresh.process.HasExited -or
        -not $script:AccountsRefresh.output.IsCompleted -or -not $script:AccountsRefresh.error.IsCompleted) {
        if (([DateTime]::UtcNow - $script:AccountsRefresh.startedAt).TotalSeconds -lt 20) { return }
        $expired = $script:AccountsRefresh
        $script:AccountsRefresh = $null; $script:AccountsInFlight = $false
        try { Stop-AccountsRefreshWorker $expired }
        finally { $expired.process.Dispose() }
        Write-TrayLog 'WARN 账号列表刷新超时，保留上次列表并允许重试'
        return
    }
    $pending = $script:AccountsRefresh
    $script:AccountsRefresh = $null
    try {
        if ($pending.process.ExitCode -ne 0) { throw 'account_list_failed' }
        $value = $pending.output.Result | ConvertFrom-Json -ErrorAction Stop
        if (-not ($value.PSObject.Properties.Name -contains 'accounts')) { throw 'account_list_invalid' }
        [System.IO.File]::WriteAllText($ACCTS, $pending.output.Result, (New-Object System.Text.UTF8Encoding($false)))
        if ($script:Menu.Visible) { Build-Menu }
    } catch { Write-TrayLog 'WARN 账号列表未能刷新，保留上次列表' }
    finally { $script:AccountsInFlight = $false; $pending.process.Dispose() }
}

function Read-Accounts {
    if (-not (Test-Path -LiteralPath $ACCTS)) { return $null }
    try { return (Get-Content -LiteralPath $ACCTS -Raw -Encoding utf8 | ConvertFrom-Json) }
    catch { return $null }
}

function Get-SelectedEmail($accounts, $snapshot) {
    $rows = @()
    if ($null -ne $accounts -and $accounts.PSObject.Properties.Name -contains 'accounts') {
        $rows = @($accounts.accounts)
    }
    $marked = @($rows | Where-Object { $_.PSObject.Properties.Name -contains 'active' })
    if ($marked.Count -gt 0) {
        $chosen = @($marked | Where-Object { $_.active -is [bool] -and $_.active })
        if ($chosen.Count -ne 1) { return '' }
        $matches = @($rows | Where-Object { $_.email -eq $chosen[0].email })
        if ($matches.Count -ne 1) { return '' }
        return [string]$chosen[0].email
    }
    return ''
}

function Switch-To([string]$email) {
    if ($script:SwitchAction) { Show-TrayBalloon '正在切换账号' '请等待当前切换完成。'; return }
    $entry = Join-Path (Split-Path -Parent $Shared) "ccpick.py"
    if (-not $script:PYW -or -not (Test-Path -LiteralPath $entry)) {
        Write-TrayLog "WARN 找不到受控切号入口"; return
    }
    if ($email -notmatch '^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$') {
        Write-TrayLog "WARN 无效的本机账号标识"; return
    }
    Write-TrayLog "手动受控切号"
    try {
        $script:SwitchAction = Start-SwitchProcess $entry $email
        Show-TrayBalloon '正在切换账号' '正在检查目标账号及家宽线路；完成后会通知。'
    } catch { Show-TrayBalloon '切换未开始' '无法启动账号管理器，请在终端重试。' }
}

function Start-SwitchProcess([string]$entry, [string]$email) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PYW
    $psi.Arguments = '"{0}" switch "{1}"' -f $entry, $email
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $p = [System.Diagnostics.Process]::Start($psi)
    return @{ process = $p; output = $p.StandardOutput.ReadToEndAsync();
        error = $p.StandardError.ReadToEndAsync(); email = $email }
}

function Get-SwitchFailureMessage([string]$text) {
    $messages = @{
        login_required = '目标账号尚未完成授权，请重新登录该账号。'
        wrong_account = '授权账号与所选条目不一致，请确认登录的邮箱。'
        auth_renewal_failed = '目标账号需要重新授权，请重新登录该账号。'
        auth_unverified = '目标账号尚未通过认证检查，请在终端查看结果。'
        profile_busy = '目标账号正在登录，请完成登录后重试。'
        readiness_busy = '目标账号正在验证，请稍后重试。'
        house_service_not_ready = '目标家宽线路尚未就绪，请稍后重试。'
        network_not_ready = '目标家宽线路检查未通过，请稍后重试。'
        local_runtime_unavailable = '账号服务检查未完成，请在终端重试。'
    }
    if ($text -match '\[([a-z_]{1,80})\]\s*$' -and $messages.ContainsKey($Matches[1])) { return $messages[$Matches[1]] }
    return '未能确认切号结果，请在终端查看具体结果。'
}

function Start-SwitchConfirmation([string]$email) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PYW
    $psi.Arguments = ('"{0}" accounts' -f $HELPER)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $process = [System.Diagnostics.Process]::Start($psi)
    $identity = $null
    try { $identity = @{ id = $process.Id; start = $process.StartTime.ToUniversalTime().Ticks } } catch { }
    return @{ process = $process; output = $process.StandardOutput.ReadToEndAsync();
        error = $process.StandardError.ReadToEndAsync(); email = $email; confirmation = $true;
        startedAt = [DateTime]::UtcNow; identity = $identity }
}

function Complete-SwitchAction {
    if (-not $script:SwitchAction) { return }
    $pending = $script:SwitchAction
    if (-not $pending.process.HasExited -or -not $pending.output.IsCompleted -or
        -not $pending.error.IsCompleted) {
        if ($pending.confirmation -and ([DateTime]::UtcNow - $pending.startedAt).TotalSeconds -ge 20) {
            $script:SwitchAction = $null
            try { Stop-AccountsRefreshWorker $pending }
            finally { $pending.process.Dispose() }
            Show-TrayBalloon '切号确认未完成' '账号列表读取超时，请在终端确认当前账号。'
            Update-Accounts
        }
        return
    }
    $script:SwitchAction = $null
    try {
        if (-not $pending.confirmation -and $pending.process.ExitCode -eq 0) {
            $script:SwitchAction = Start-SwitchConfirmation $pending.email
            return
        }
        $selected = ''
        if ($pending.confirmation -and $pending.process.ExitCode -eq 0) {
            $accounts = $pending.output.Result | ConvertFrom-Json -ErrorAction Stop
            $selected = Get-SelectedEmail $accounts $null
        }
        if ($pending.confirmation -and $selected -ceq $pending.email) {
            Write-TrayLog '手动切号已确认'
            $script:LastKnownEmail = $selected
            Show-TrayBalloon 'Claude 账号已切换' ('现在：' + $selected)
        } else {
            Write-TrayLog '手动切号未完成'
            Show-TrayBalloon '切号未完成' (Get-SwitchFailureMessage $pending.error.Result)
        }
        Update-Accounts
    } catch {
        Write-TrayLog '手动切号结果读取未完成'
        Show-TrayBalloon '切号确认未完成' '无法读取当前账号，请在终端确认切号结果。'
        Update-Accounts
    } finally { $pending.process.Dispose() }
}

$script:Notify = New-Object System.Windows.Forms.NotifyIcon
$script:CurState = ""
$script:LastCheckAt = [DateTime]::MinValue
$script:NextIntervalS = 60
$script:AccountsInFlight = $false
$script:AccountsRefresh = $null
$script:SwitchAction = $null

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

function Test-WatchOnlyOverride {
    return ($env:CCSWITCH_WATCH_ONLY -match '^(?i:1|true|yes|on)$')
}

function Get-WatchOnlyMode {
    return ((Test-Path -LiteralPath $WATCH_ONLY) -or (Test-WatchOnlyOverride))
}

function Set-WatchOnlyMode([bool]$enabled) {
    if (-not $enabled -and (Test-WatchOnlyOverride)) {
        throw "CCSWITCH_WATCH_ONLY 环境变量固定为只看不切"
    }
    if ($enabled) {
        [System.IO.File]::WriteAllText($WATCH_ONLY, "watch-only`n", [System.Text.Encoding]::ASCII)
    } elseif (Test-Path -LiteralPath $WATCH_ONLY) {
        Remove-Item -LiteralPath $WATCH_ONLY -ErrorAction Stop
    }
    $script:LastCheckAt = [DateTime]::MinValue
    $label = if ($enabled) { "只看不切" } else { "又看又切" }
    Write-TrayLog ("模式改为：" + $label)
    Show-TrayBalloon "Claude 切号模式" ("已选择「" + $label + "」，下一轮检查生效")
}

function Add-ModeItems {
    $watchOnly = Get-WatchOnlyMode
    foreach ($mode in @(
        @{ label = "只看不切"; watch = $true; tip = "监控额度并提醒，账号由你手动选择" },
        @{ label = "又看又切"; watch = $false; tip = "监控额度并自动选择账号，跟随选择的会话在安全节点切换" }
    )) {
        $mi = New-Object System.Windows.Forms.ToolStripMenuItem($mode.label)
        $mi.Tag = $mode.watch
        $mi.Checked = ($watchOnly -eq $mode.watch)
        $mi.ToolTipText = $mode.tip
        if (-not $mode.watch -and (Test-WatchOnlyOverride)) {
            $mi.Enabled = $false
            $mi.ToolTipText = "CCSWITCH_WATCH_ONLY 环境变量固定为只看不切"
        }
        $mi.Add_Click({
            try { Set-WatchOnlyMode ([bool]$this.Tag) }
            catch {
                Write-TrayLog ("模式修改失败：" + $_.Exception.Message)
                Show-TrayBalloon "模式未修改" "保存模式失败，请查看托盘日志"
            }
        })
        [void]$script:Menu.Items.Add($mi)
    }
    [void]$script:Menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
}

function Add-Info($text) {
    $mi = New-Object System.Windows.Forms.ToolStripMenuItem($text)
    $mi.Enabled = $false
    [void]$script:Menu.Items.Add($mi)
}

function Build-Menu {
    $script:Menu.Items.Clear()
    $snap = Read-Status

    Add-ModeItems
    Add-Info (Get-Headline $snap["state"] ([string]$snap["message"]))
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
        $activeEmail = Get-SelectedEmail $acc $snap
        foreach ($a in $acc.accounts) {
            $isSelected = $activeEmail -and $a.email -eq $activeEmail
            $manualOnly = ($a.PSObject.Properties.Name -contains "autoSwitchEnabled") -and
                ($a.autoSwitchEnabled -is [bool]) -and (-not $a.autoSwitchEnabled)
            $mark = "     "
            if ($isSelected) { $mark = "  ●  " }
            $label = $mark + $a.email
            $tags = @()
            if (($a.PSObject.Properties.Name -contains "plan") -and $a.plan -and ([string]$a.plan -ne "?")) { $tags += [string]$a.plan }
            if (($a.PSObject.Properties.Name -contains "cap") -and ($null -ne $a.cap)) {
                try { $tags += ("{0:F0} 点" -f [double]$a.cap) } catch { }
            }
            if ($manualOnly) { $tags += "仅手动" }
            if (($a.PSObject.Properties.Name -contains 'household') -and $a.household -cmatch '^[ABCD]$') { $tags += '家宽 ' + $a.household }
            if ($tags.Count -gt 0) { $label = $label + "    " + ($tags -join " · ") }

            $blocked = $false
            if ($a.PSObject.Properties.Name -contains "blocked") { $blocked = [bool]$a.blocked }
            if ($blocked -and -not $manualOnly) {
                $reason = ""
                if ($a.PSObject.Properties.Name -contains "blockedWhy") { $reason = [string]$a.blockedWhy }
                if ($reason.Length -gt 46) { $reason = $reason.Substring(0, 46) }
                Add-Info ("     " + $a.email + "    " + $reason)
                continue
            }

            $mi = New-Object System.Windows.Forms.ToolStripMenuItem($label)
            $mi.Tag = $a.email
            if ($manualOnly) {
                $mi.ForeColor = [System.Drawing.Color]::DarkViolet
                $mi.ToolTipText = "只在手动选择时使用，不参与自动切号。"
                if ($blocked -and ($a.PSObject.Properties.Name -contains "blockedWhy")) {
                    $mi.ToolTipText += " " + [string]$a.blockedWhy
                }
            }
            $mi.Add_Click({
                $target = $this.Tag
                try { Switch-To $target }
                catch { Write-TrayLog ("切到 " + $target + " 出错: " + $_.Exception.Message) }
            })
            if ($isSelected -or $script:SwitchAction) { $mi.Enabled = $false }
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
        Update-Accounts
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
    if ($script:Exiting) { return }
    if (Test-ParentGone) {
        $script:Exiting = $true
        Write-TrayLog ("父进程 conhost (pid={0}) 没了 —— 计划任务被结束，托盘跟着退出" -f $script:ParentConhost.Id)
        [System.Windows.Forms.Application]::Exit()
        return
    }
    try { Complete-AccountsRefresh; Complete-SwitchAction; Update-Icon } catch { Write-TrayLog ("图标轮次出错: " + $_.Exception.Message) }
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
        if ($snap['decisionAction'] -eq 'switched' -and $snap['state'] -eq 'switched') {
            Show-TrayBalloon "Claude 账号已自动切换" "现在：$ae"
        } else {
            Show-TrayBalloon "Claude 账号已切换" "现在：$ae"
        }
    }
}

$script:SeenAlert = @{}
function Notify-Alerts($snap) {
    $specs = @(
        @{ f = $script:NotifiedFile;                  title = "Claude 全部账号额度用尽" },
        @{ f = ($script:NotifiedFile + ".switchfail"); title = "Claude 自动切号没切成" },
        @{ f = ($script:NotifiedFile + ".denied");          title = "Claude 账号疑似被封" },
        @{ f = ($script:NotifiedFile + ".denied-switched"); title = "Claude 账号疑似被封, 已自动换号" },
        @{ f = ($script:NotifiedFile + ".watch"); title = "Claude 该换号了"; useMessage = $true }
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
            if ($sp.ContainsKey("useMessage") -or -not $text) { $text = [string]$snap["message"] }
            if ($sp.ContainsKey("useMessage")) { $text = "$text（自动切换已关）" }
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

    $tip = "{0}`n{1}" -f (Get-Headline $snap["state"] ([string]$snap["message"])), (Get-AgeText $snap["age"])
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
