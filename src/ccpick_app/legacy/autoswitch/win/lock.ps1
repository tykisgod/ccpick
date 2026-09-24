
function Enter-AutoswitchLock([string]$LockDir, [scriptblock]$BeforeReclaim = $null) {
    $log = @()
    try {
        New-Item -ItemType Directory -Path $LockDir -ErrorAction Stop | Out-Null
        Set-Content -LiteralPath (Join-Path $LockDir "pid") -Value $PID -Encoding ascii
        return @{ got = $true; log = $log }
    } catch {
        $holder = ""
        $pidFile = Join-Path $LockDir "pid"
        if (Test-Path -LiteralPath $pidFile) {
            $holder = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue)
        }
        $seen = Read-LockRaw $LockDir
        $alive = $false
        if ($holder) {
            $holder = $holder.Trim()
            $n = 0
            if ([int]::TryParse($holder, [ref]$n) -and $n -gt 0) {
                if (Get-Process -Id $n -ErrorAction SilentlyContinue) { $alive = $true }
            } else {
                $holder = ""
            }
        }
        if ($alive) {
            return @{ got = $false; log = @("SKIP 另一轮正在跑 (pid=$holder), 本轮让路") }
        }
        if (-not $holder) {
            $lockAge = 999.0
            try {
                $lockAge = ((Get-Date) - (Get-Item -LiteralPath $LockDir -Force).CreationTime).TotalSeconds
            } catch {}
            if ($lockAge -lt 10) {
                return @{ got = $false; log = @("SKIP 锁刚建 {0:F1}s 且还没写 pid, 可能对方正在写, 本轮让路" -f $lockAge) }
            }
        }
        $who = $holder
        if (-not $who) { $who = "未知" }
        $log += "回收陈旧锁 (pid=$who 已不在)"
        if ($BeforeReclaim) { & $BeforeReclaim }
        $grave = "{0}.reclaim.{1}.{2}" -f $LockDir, $PID, [DateTime]::Now.Ticks
        try {
            Rename-Item -LiteralPath $LockDir -NewName (Split-Path -Leaf $grave) -ErrorAction Stop
        } catch {
            return @{ got = $false; log = ($log + "SKIP 别的进程先回收了, 本轮让路") }
        }
        if ((Read-LockRaw $grave) -ne $seen) {
            try { Rename-Item -LiteralPath $grave -NewName (Split-Path -Leaf $LockDir) -ErrorAction Stop } catch {}
            return @{ got = $false; log = ($log + "SKIP 锁刚被别的进程接手, 本轮让路") }
        }
        Remove-Item -LiteralPath $grave -Recurse -Force -ErrorAction SilentlyContinue
        try {
            New-Item -ItemType Directory -Path $LockDir -ErrorAction Stop | Out-Null
            Set-Content -LiteralPath (Join-Path $LockDir "pid") -Value $PID -Encoding ascii
            return @{ got = $true; log = $log }
        } catch {
            return @{ got = $false; log = ($log + "SKIP 抢不到锁") }
        }
    }
}

function Read-LockRaw([string]$dir) {
    $f = Join-Path $dir "pid"
    if (-not (Test-Path -LiteralPath $f)) { return "" }
    $t = Get-Content -LiteralPath $f -Raw -ErrorAction SilentlyContinue
    if ($null -eq $t) { return "" }
    return ([string]$t).Trim()
}
