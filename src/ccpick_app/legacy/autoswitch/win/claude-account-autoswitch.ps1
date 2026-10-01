
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$ForceCheck,
    [switch]$NoLock
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Continue"

$Root     = Join-Path $env:LOCALAPPDATA "ccpick-autoswitch"
$LOG      = Join-Path $Root "autoswitch.log"
$NOTIFIED = Join-Path $Root ".notified"
$STATUS   = Join-Path $Root "status.json"
$LOCKDIR  = Join-Path $Root ".lock"
$HELPER_ERR = Join-Path $Root "child.stderr.log"

$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Shared = Split-Path -Parent $Here
$DECIDE = Join-Path $Shared "claude-autoswitch-decide.py"
$HELPER = Join-Path $Shared "claude-autoswitch-helper.py"

if (-not (Test-Path -LiteralPath $Root)) {
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
}

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -LiteralPath $LOG -Value $line -Encoding utf8
}

function Resolve-Python {
    $cands = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:SystemRoot "py.exe")
    )
    foreach ($c in $cands) { if (Test-Path -LiteralPath $c) { return $c } }
    $scan = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe") `
            -ErrorAction SilentlyContinue | Where-Object { $_.Directory.Name -match '^Python3\d+$' } |
            Sort-Object { [int]($_.Directory.Name.Substring(7)) } -Descending | Select-Object -First 1
    if ($scan) { return $scan.FullName }
    $w = (Get-Command python.exe -ErrorAction SilentlyContinue)
    if ($w) { return $w.Source }
    return $null
}

function ConvertTo-Argv([string[]]$argv) {
    $BS = [char]92; $Q = [char]34
    $sb = New-Object System.Text.StringBuilder
    foreach ($a in $argv) {
        if ($sb.Length -gt 0) { [void]$sb.Append(' ') }
        if ($a -ne '' -and $a.IndexOfAny([char[]]@(' ', "`t", $Q, $BS)) -lt 0) {
            [void]$sb.Append($a); continue
        }
        [void]$sb.Append($Q)
        $n = 0
        foreach ($ch in $a.ToCharArray()) {
            if ($ch -eq $BS) { $n++; continue }
            if ($ch -eq $Q) {
                [void]$sb.Append([string]$BS * ($n * 2 + 1)); [void]$sb.Append($Q)
            } else {
                [void]$sb.Append([string]$BS * $n); [void]$sb.Append($ch)
            }
            $n = 0
        }
        [void]$sb.Append([string]$BS * ($n * 2))
        [void]$sb.Append($Q)
    }
    return $sb.ToString()
}

function Invoke-Capture([string]$exe, [string[]]$argv, [int]$timeoutMs = 180000, [string]$inputText = "") {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = ConvertTo-Argv $argv
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = ($inputText.Length -gt 0)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    $p = [System.Diagnostics.Process]::Start($psi)
    $so = $p.StandardOutput.ReadToEndAsync()
    $se = $p.StandardError.ReadToEndAsync()
    if ($psi.RedirectStandardInput) {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($inputText)
        $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $p.StandardInput.Close()
    }
    if (-not $p.WaitForExit($timeoutMs)) {
        try { $p.Kill() } catch {}
        return @{ rc = 124; out = ""; err = "timeout after ${timeoutMs}ms" }
    }
    return @{ rc = $p.ExitCode; out = $so.Result; err = $se.Result }
}

if (Test-Path -LiteralPath $LOG) {
    $len = (Get-Item -LiteralPath $LOG).Length
    if ($len -gt 2000000) {
        $keep = Get-Content -LiteralPath $LOG -Tail 4000
        Set-Content -LiteralPath $LOG -Value $keep -Encoding utf8
    }
}

$PY = Resolve-Python
if (-not $PY) { Write-Log "FAIL 找不到 Python 解释器"; exit 0 }
if (-not (Test-Path -LiteralPath $DECIDE)) { Write-Log "FAIL 找不到决策脚本 $DECIDE"; exit 0 }

. (Join-Path $Here "lock.ps1")
$gotLock = $false
if (-not $NoLock) {
    $lock = Enter-AutoswitchLock $LOCKDIR
    if ($null -eq $lock -or -not $lock.got) {
        if ($null -ne $lock) { foreach ($m in $lock.log) { Write-Log $m } }
        else { Write-Log "SKIP 锁状态读不出来, 本轮让路" }
        exit 0
    }
    foreach ($m in $lock.log) { Write-Log $m }
    $gotLock = $true
}

function Remove-Lock {
    if ($script:gotLock) {
        Remove-Item -LiteralPath $script:LOCKDIR -Recurse -Force -ErrorAction SilentlyContinue
        $script:gotLock = $false
    }
}

function Format-LocalWhen([string]$iso) {
    if (-not $iso) { return "" }
    try {
        $lt = ([datetimeoffset]::Parse($iso)).LocalDateTime
        $days = ($lt.Date - (Get-Date).Date).Days
        if ($days -eq 0) { return $lt.ToString("今天 HH:mm") }
        if ($days -eq 1) { return $lt.ToString("明天 HH:mm") }
        return $lt.ToString("MM-dd HH:mm")
    } catch { return "" }
}

function Write-Status([string]$state, [string]$msg, [string]$extra,
                      [string]$thr, [string]$wins, [string]$sched) {
    $r = Invoke-Capture $PY @($HELPER, "write", $STATUS, $state, $msg, $extra, $thr, $wins, $sched) 30000
    if ($r.rc -ne 0 -and $r.err) {
        Add-Content -LiteralPath $HELPER_ERR -Value $r.err -Encoding utf8
    }
}

function Update-Wait([string]$operation, [string]$decisionJson) {
    $r = Invoke-Capture $PY @($HELPER, $operation, $STATUS) 30000 $decisionJson
    if ($r.err) { Add-Content -LiteralPath $HELPER_ERR -Value $r.err -Encoding utf8 }
}

function Test-TrayAlive {
    try {
        $m = [System.Threading.Mutex]::OpenExisting("Global\ccpick-autoswitch-tray")
        $m.Dispose()
        return $true
    } catch { return $false }
}

function Show-Balloon([string]$title, [string]$text) {
    if (Test-TrayAlive) {
        Write-Log "通知交给托盘弹（本轮薄壳不弹，免得弹两次）"
        return
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        Add-Type -AssemblyName System.Drawing -ErrorAction Stop
        $ni = New-Object System.Windows.Forms.NotifyIcon
        $ni.Icon = [System.Drawing.SystemIcons]::Information
        $ni.Visible = $true
        $ni.BalloonTipTitle = $title
        $ni.BalloonTipText = $text
        $ni.ShowBalloonTip(8000)
        Start-Sleep -Milliseconds 6000
        $ni.Visible = $false
        $ni.Dispose()
    } catch {
        Write-Log "WARN 通知弹不出来: $($_.Exception.Message)"
    }
}

try {
    if (-not $ForceCheck) {
        $wait = Invoke-Capture $PY @($HELPER, "wait", $STATUS) 30000
        if ($wait.err) { Add-Content -LiteralPath $HELPER_ERR -Value $wait.err -Encoding utf8 }
        if ($wait.rc -eq 0) { exit 0 }
    }
    $accountPointer = Join-Path (Split-Path -Parent $Shared) 'account-manager.json'
    $managedAccounts = Test-Path -LiteralPath $accountPointer
    $decisionBudgetMs = 180000
    if ($managedAccounts) {
        try {
            $managerRoot = (Get-Content -LiteralPath $accountPointer -Raw -Encoding UTF8 | ConvertFrom-Json).root
            $profileCount = @(Get-ChildItem -LiteralPath (Join-Path $managerRoot 'data') -Directory -ErrorAction Stop |
                Where-Object { $_.Name -match '^[a-z][a-z0-9_-]{0,47}$' }).Count
            $decisionBudgetMs = [int][Math]::Min(2147480000, 300000.0 * ($profileCount + 1))
        } catch {
            Write-Status 'error' '统一账号配置暂不可用' '保留当前账号，稍后重试' '' '' ''
            exit 0
        }
    }
    $online = $false
    if ($managedAccounts) { $online = $true } else {
    try {
        $req = [System.Net.HttpWebRequest]::Create("https://claude.ai/")
        $req.Method = "HEAD"
        $req.Timeout = 8000
        $req.AllowAutoRedirect = $true
        $resp = $req.GetResponse()
        $resp.Close()
        $online = $true
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { $online = $true }
    } catch {}
    }

    if (-not $online) {
        Write-Log "SKIP 连不上 claude.ai (断网/睡醒瞬间)"
        $t = $env:CCSWITCH_THRESHOLD
        if (-not $t) { $t = "90" }
        Write-Status "offline" "连不上 claude.ai" "下一轮自己重试" $t "" ""
        exit 0
    }

    $argv = @($DECIDE)
    if ($DryRun) { $argv += "--dry-run" }
    $res = Invoke-Capture $PY $argv $decisionBudgetMs
    $rc = $res.rc
    $out = ($res.out + $res.err).Trim()

    $o = $null
    try { $o = $out | ConvertFrom-Json } catch {}

    function Field($name) {
        if ($null -eq $o) { return $null }
        if ($o.PSObject.Properties.Name -contains $name) { return $o.$name }
        return $null
    }
    function NumOrDash($v) {
        if ($null -eq $v) { return "-" }
        try { return [string][int][math]::Round([double]$v) } catch { return "-" }
    }
    $decisionAction = [string](Field "action")
    if ($rc -eq 0 -and $decisionAction -ne 'switched') {
        if ($decisionAction -in @('stay', 'would-switch')) { $rc = 2 } else { $rc = 1 }
    }

    $w = Field "windows"
    $used = Field "used"
    if ($null -eq $used) { $used = Field "toUsed" }
    $w5 = "-"; $w7 = "-"; $wm = "-"; $modelKey = $null
    if ($w) {
        $names = $w.PSObject.Properties.Name
        if ($names -contains "5h") { $w5 = NumOrDash $w."5h" }
        if ($names -contains "7d") { $w7 = NumOrDash $w."7d" }
        $modelKey = $names | Where-Object { $_ -ne "5h" -and $_ -ne "7d" } | Select-Object -First 1
        if ($modelKey) { $wm = NumOrDash $w.$modelKey }
    }
    $bind = Field "binding"
    if ($null -eq $bind) { $bind = "" }
    $mc = ""
    if ($modelKey -and $null -ne $o -and ($o.PSObject.Properties.Name -contains "countedWindows") `
            -and $null -ne $o.countedWindows) {
        if (@($o.countedWindows) -contains [string]$modelKey) { $mc = "1" } else { $mc = "0" }
    }
    $mname = ""
    if ($modelKey) { $mname = [string]$modelKey }
    $wins = "{0}|{1}|{2}|{3}|0|{4}|{5}|{6}" -f (NumOrDash $used), $w5, $w7, $wm,
            ([string]$bind -replace '\|', ''), ($mname -replace '\|', ''), $mc

    function Fmt1($v) {
        if ($null -eq $v) { return "" }
        try { return "{0:F1}" -f [double]$v } catch { return "" }
    }
    $ae = Field "activeEmail"
    if ($null -eq $ae) { $ae = "" }
    $sched = "{0}|{1}|{2}|{3}|{4}" -f (Fmt1 (Field "nextCheckS")), (Fmt1 (Field "etaS")),
                                  (Fmt1 (Field "burnRate")), ($ae -replace '\|', ''), ($decisionAction -replace '\|', '')

    $thr = ""
    $consider = Field "threshold"
    if ($null -eq $consider) { $consider = Field "consider" }
    if ($null -ne $consider) { try { $thr = "{0:F0}" -f [double]$consider } catch {} }
    if (-not $thr) {
        $thr = $env:CCSWITCH_THRESHOLD
        if (-not $thr) { $thr = "90" }
    }

    $rate = Field "burnRate"; $eta = Field "etaS"
    $extra = [string](Field "why")
    if (-not $extra -and $null -ne $rate -and $null -ne $eta) {
        try {
            $mins = [math]::Max(1, [math]::Round([double]$eta / 60))
            $extra = "烧 {0:F0} 点/分, 约 {1} 分钟到顶" -f [double]$rate, $mins
        } catch {}
    }
    if (-not $extra) { $extra = "到 $thr% 或快见底就切" }

    switch ($rc) {
        0 {
            $to = Field "to"
            if (-not $to) { $to = $ae }
            if (-not $to) { $to = "见日志" }
            $deniedFrom = [string](Field "deniedFrom")
            $notice = [string](Field "notice")          # 通知正文由 decide 给 (短, 结论在前)
            if (-not $notice) { $notice = "$deniedFrom 被拒 (403), 疑似被封; 现在: $to" }
            if ($deniedFrom) {
                Write-Log "★SWITCHED 原号疑似被停用 (用量接口 403) $out"
                Write-Status "switched" "刚切到 $to" $notice $thr $wins $sched
                Set-Content -LiteralPath "$NOTIFIED.denied-switched" -Value ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) -Encoding ascii
            } else {
                Write-Log "SWITCHED $out"
                Write-Status "switched" "刚切到 $to" "刚落地, 先紧盯几轮" $thr $wins $sched
            }
            Remove-Item -LiteralPath $NOTIFIED -Force -ErrorAction SilentlyContinue
            Remove-Lock            # 先放锁再弹气泡：气泡要等 6 秒，不该占着锁
            if ($deniedFrom) {
                Show-Balloon "Claude 账号疑似被封, 已自动换号" $notice
            } else {
                Show-Balloon "Claude 账号已自动切换" "现在：$to"
            }
        }
        2 {
            if ((Field "switchFailed") -eq $true) {
                $why = [string](Field "why")
                if (-not $why) { $why = "切换没成功" }
                if ($why.Length -gt 120) { $why = $why.Substring(0, 120) }
                Write-Log "WARN 切换失败, 留在原号 $out"
                Write-Status "ok" "切换没成功, 先用着当前号" $why $thr $wins $sched
                $sfFile = "$NOTIFIED.switchfail"
                $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                $last = 0
                if (Test-Path -LiteralPath $sfFile) {
                    try { $last = [long](Get-Content -LiteralPath $sfFile -Raw).Trim() } catch {}
                }
                if (($now - $last) -ge 3600) {
                    Set-Content -LiteralPath $sfFile -Value $now -Encoding ascii
                    Remove-Lock
                    Show-Balloon "Claude 自动切号没切成" "当前号还能用; 看日志 $LOG"
                }
            } else {
                $wo = ((Field "watchOnly") -eq $true)
                $wto = [string](Field "wouldSwitchTo")
                $wnote = ""
                if ((Field "currentDenied") -eq $true) {
                    $wnote = [string](Field "notice")
                    if ($wnote.Length -gt 120) { $wnote = $wnote.Substring(0, 120) }
                }
                if ($wo -and $wto) {
                    if ($wnote) {
                        Write-Log "WATCH 当前号疑似被封, 只看不切, 建议 $wto $out"
                        Write-Status "advise" "该换号了: 建议 $wto" $wnote $thr $wins $sched
                        Update-Wait "schedule" $res.out
                        $wfFile = "$NOTIFIED.denied"; $wTitle = "Claude 账号疑似被封, 请手动换号"; $wText = $wnote
                    } else {
                        Write-Log "WATCH 该换号了但只看不切, 建议 $wto $out"
                        Write-Status "advise" "该换号了: 建议 $wto" "自动切换已关; 在托盘菜单里点账号名即可手动切" $thr $wins $sched
                        Update-Wait "schedule" $res.out
                        $wfFile = "$NOTIFIED.watch"; $wTitle = "Claude 该换号了"; $wText = "自动切换已关; 建议切到 $wto"
                    }
                    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                    $last = 0
                    if (Test-Path -LiteralPath $wfFile) {
                        try { $last = [long](Get-Content -LiteralPath $wfFile -Raw).Trim() } catch {}
                    }
                    if (($now - $last) -ge 3600) {
                        Set-Content -LiteralPath $wfFile -Value $now -Encoding ascii
                        Remove-Lock
                        Show-Balloon $wTitle $wText
                    }
                } elseif ($wo) {
                    Write-Log "ok 只看不切, 无需换号 $out"
                    $wextra = "$extra · 自动切换已关"
                    if ($extra.StartsWith("到")) { $wextra = "自动切换已关; 该换号时会提醒" }
                    Write-Status "ok" "只看不切" $wextra $thr $wins $sched
                    Update-Wait "schedule" $res.out
                } else {
                    Write-Log "ok 无需切换 $out"
                    Write-Status "ok" "在盯着" $extra $thr $wins $sched
                }
                Remove-Item -LiteralPath "$NOTIFIED.switchfail" -Force -ErrorAction SilentlyContinue
                if (-not $wnote) {
                    Remove-Item -LiteralPath "$NOTIFIED.denied" -Force -ErrorAction SilentlyContinue
                }
            }
            Remove-Item -LiteralPath $NOTIFIED -Force -ErrorAction SilentlyContinue
        }
        3 {
          if (((Field "activeDenied") -eq $true) -or [string](Field "deniedFrom")) {
            $notice = [string](Field "notice")          # 通知正文由 decide 给, 壳不自己拼
            if (-not $notice) { $notice = [string](Field "why") }
            if (-not $notice) { $notice = "当前号被拒 (403), 疑似被封" }
            if ($notice.Length -gt 120) { $notice = $notice.Substring(0, 120) }
            $msg = "当前号疑似被封, 没得换"
            if ((Field "denyUnsure") -eq $true) { $msg = "当前号疑似被封, 还在确认" }
            elseif ((Field "activeDenied") -ne $true) { $msg = "原号疑似被封, 换到的也满了" }
            elseif ((Field "switchFailed") -eq $true) { $msg = "当前号疑似被封, 切换没成功" }  # 有号能去, 是切不过去
            Write-Log "★BLOCKED $msg $out"
            Write-Status "blocked" $msg $notice $thr $wins $sched
            if ((Field "quiet") -ne $true) {
                $dnFile = "$NOTIFIED.denied"
                $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                $last = 0
                if (Test-Path -LiteralPath $dnFile) {
                    try { $last = [long](Get-Content -LiteralPath $dnFile -Raw).Trim() } catch {}
                }
                if (($now - $last) -ge 3600) {
                    Set-Content -LiteralPath $dnFile -Value $now -Encoding ascii
                    Remove-Lock
                    Show-Balloon "Claude 账号疑似被封" $notice
                }
            }
          } elseif ((Field "switchFailed") -eq $true) {
            $why = [string](Field "why")
            if (-not $why) { $why = "切换没成功" }
            if ($why.Length -gt 120) { $why = $why.Substring(0, 120) }
            Write-Log "★BLOCKED 切换失败, 当前号也快满了 $out"
            Write-Status "blocked" "切换没成功, 当前号也快满了" $why $thr $wins $sched
            $sfFile = "$NOTIFIED.switchfail"
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            $last = 0
            if (Test-Path -LiteralPath $sfFile) {
                try { $last = [long](Get-Content -LiteralPath $sfFile -Raw).Trim() } catch {}
            }
            if (($now - $last) -ge 3600) {
                Set-Content -LiteralPath $sfFile -Value $now -Encoding ascii
                Remove-Lock
                Show-Balloon "Claude 自动切号没切成" "当前号也快满了; 看日志 $LOG"
            }
          } else {
            $soonest = Field "soonest"
            $soonAt = Field "soonestAt"
            $soon = "等最早那个恢复"
            $soonText = Format-LocalWhen $soonAt
            if ($soonest -and $soonText) {
                $soon = "$soonest 最早恢复 $soonText"
            }
            Write-Log "★BLOCKED 没有可切的账号。$soon | $out"
            Write-Status "blocked" "全部账号见底" $soon $thr $wins $sched
            Update-Wait "defer" $res.out
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            if (-not (Test-Path -LiteralPath $NOTIFIED)) {
                Set-Content -LiteralPath $NOTIFIED -Value $now -Encoding ascii
                Remove-Lock
                Show-Balloon "Claude 全部账号额度用尽" $soon
            }
          }
        }
        default {
            Write-Log "ERROR rc=$rc $out"
            $lines = $out -split "`n"
            $errmsg = ""
            if ($lines.Count -gt 0) { $errmsg = $lines[-1].Trim() }
            if ($errmsg.Length -gt 120) { $errmsg = $errmsg.Substring(0, 120) }
            $t = $env:CCSWITCH_THRESHOLD
            if (-not $t) { $t = "90" }
            Write-Status "error" "决策出错" $errmsg $t "" ""
        }
    }
} finally {
    Remove-Lock
}

exit 0
