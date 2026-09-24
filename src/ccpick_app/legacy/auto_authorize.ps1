param(
  [Parameter(Mandatory=$true)][string]$Profile,
  [Parameter(Mandatory=$true)][string]$Email,
  [int]$TimeoutSec = 240,
  [switch]$NoAdd,
  [string]$ConfigDir,
  [int]$MaxAttemptsPerStage = 6,
  [Parameter(Mandatory=$true)][string]$CcpickLauncher
)

$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FgWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
}
"@

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$Cond = [System.Windows.Automation.PropertyCondition]
$CT = [System.Windows.Automation.ControlType]

function Get-ChromePages {
  $root = $AE::RootElement
  $c = New-Object $Cond ($AE::ClassNameProperty, "Chrome_WidgetWin_1")
  $out = @()
  foreach ($w in $root.FindAll($TS::Children, $c)) {
    $n = $w.Current.Name
    if ($n -and $n -like "*- Google Chrome") { $out += $w }
  }
  return $out
}

function Find-Element($win, $text, $exact = $true) {
  foreach ($tn in @("Button", "Hyperlink")) {
    $ct = [System.Windows.Automation.ControlType]::$tn
    $cc = New-Object $Cond ($AE::ControlTypeProperty, $ct)
    foreach ($e in $win.FindAll($TS::Descendants, $cc)) {
      $n = $e.Current.Name
      if (-not $n) { continue }
      $t = $n.Trim()
      $hit = if ($exact) { $t -eq $text } else { $t -like ("*" + $text + "*") }
      if ($hit) {
        $inv = $e.GetSupportedPatterns() | Where-Object { $_.ProgrammaticName -like "*Invoke*" }
        if ($inv) { return $e }
      }
    }
  }
  return $null
}

function Bring-Front($win) {
  try {
    $h = [IntPtr]$win.Current.NativeWindowHandle
    [void][FgWin]::ShowWindow($h, 9)      # SW_RESTORE
    [void][FgWin]::SetForegroundWindow($h)
  } catch {}
}

function Try-Click($win, $text, $exact = $true, $label = "") {
  $el = Find-Element $win $text $exact
  if (-not $el) { return $false }

  if (-not $el.Current.IsEnabled -or $el.Current.IsOffscreen) {
    Bring-Front $win
    Start-Sleep -Milliseconds 1200
    $el = Find-Element $win $text $exact
    if (-not $el) { return $false }
  }
  if ($el.Current.IsOffscreen) {
    try {
      $el.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView()
      Start-Sleep -Milliseconds 700
      $el = Find-Element $win $text $exact
      if (-not $el) { return $false }
    } catch {}
  }
  if (-not $el.Current.IsEnabled) {
    Write-Output ("   [{0}] 目标仍 disabled，跳过本轮" -f $label)
    return $false
  }
  try {
    $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    Write-Output ("   [{0}] 已点「{1}」" -f $label, $text)
    return $true
  } catch {
    Write-Output ("   [{0}] Invoke 失败: {1}" -f $label, $_.Exception.Message)
    return $false
  }
}

$cc = $CcpickLauncher
if (-not [System.IO.Path]::IsPathRooted($cc) -or -not (Test-Path -LiteralPath $cc -PathType Leaf)) {
  throw 'The installed ccpick launcher must be an absolute executable path.'
}
$eargs = @("enroll", "--profile", $Profile, "--email", $Email, "--timeout", "$TimeoutSec")
if (-not $NoAdd) { $eargs += "--add" }
if ($ConfigDir) { $eargs += @("--config-dir", $ConfigDir) }

$stalePat = @("Sign in successful | Claude Platform*", "localhost:*callback*",
              "Sign in - Claude*", "登录 - Google 账号*", "Sign in - Google Accounts*")
$closed = 0
foreach ($w in (Get-ChromePages)) {
  $t = $w.Current.Name
  foreach ($p in $stalePat) {
    if ($t -like ($p + "*")) {
      try {
        $wp = $w.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern)
        $wp.Close(); $closed++
      } catch {}
      break
    }
  }
}
if ($closed) { Write-Output ("[auto] 清理了 {0} 个上一轮遗留的 OAuth 窗口" -f $closed) }

Write-Output ("[auto] 起 enroll: profile={0} email={1}" -f $Profile, $Email)
$job = Start-Job -ScriptBlock {
  param($exe, $a)
  $OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
  & $exe @a 2>&1
  $childCode = $LASTEXITCODE
  if ($null -eq $childCode) { $childCode = 1 }
  [pscustomobject]@{ CcpickExitCode = [int]$childCode }
} -ArgumentList $cc, $eargs

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$done = @{ signin = $false; google = $false; authorize = $false }
$tries = @{ signin = 0; google = 0; consent = 0; authorize = 0 }

while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
  if ($job.State -ne "Running") { Write-Output "[auto] enroll 进程已结束"; break }

  foreach ($w in (Get-ChromePages)) {
    $title = $w.Current.Name


    if (-not $done.signin -and $title -like "*Sign in*Claude*" -and $title -notlike "*successful*") {
      if ($tries.signin -ge $MaxAttemptsPerStage) { continue }
      if (Find-Element $w "Continue with Google" $true) {
        $tries.signin++
        if (Try-Click $w "Continue with Google" $true "阶段1") {
          $done.signin = $true; Start-Sleep -Seconds 3
        }
      }
      continue
    }

    $isGoogleAuth = ($title -like "登录 - Google*") -or
                    ($title -like "Sign in - Google Accounts*") -or
                    ($title -like "accounts.google.com*") -or
                    ($title -like "*Choose an account*")
    if ($isGoogleAuth) {
      if (-not $done.google -and $tries.google -lt $MaxAttemptsPerStage) {
        if (Find-Element $w $Email $false) {
          $tries.google++
          if (Try-Click $w $Email $false "阶段2-选号") {
            $done.google = $true; Start-Sleep -Seconds 3
          }
          continue
        }
      }
      foreach ($cont in @("继续", "Continue", "确认", "Confirm")) {
        if (Find-Element $w $cont $true) {
          if ($tries.consent -lt $MaxAttemptsPerStage) {
            $tries.consent++
            if (Try-Click $w $cont $true "阶段2b-确认") {
              $done.google = $true; Start-Sleep -Seconds 4
            }
          }
          break
        }
      }
      continue
    }

    if (-not $done.authorize -and $title -eq "Claude - Google Chrome") {
      if ($tries.authorize -ge $MaxAttemptsPerStage) { continue }
      if (Find-Element $w "Authorize" $true) {
        $tries.authorize++
        if (Try-Click $w "Authorize" $true "阶段3") {
          $done.authorize = $true; Start-Sleep -Seconds 2
        }
      }
      continue
    }
  }

  foreach ($w in (Get-ChromePages)) {
    $t = $w.Current.Name
    if ($t -like "localhost:*callback*error=*") {
      $err = if ($t -match "error_description=([^&]+)") { $matches[1] } else { "?" }
      $code = if ($t -match "[?&]error=([^&]+)") { $matches[1] } else { "?" }
      Write-Output ("[auto] ★授权被拒★ error={0} description={1}" -f $code, $err)
      if ($err -eq "account_on_hold") {
        Write-Output "[auto] 这个 Claude 账号被暂停了（claude.ai/restricted），不是工具问题，无法入库。"
      }
      $accountBlocked = $err
      break
    }
  }
  if ($accountBlocked) { break }

  if ($done.authorize) {
    Write-Output "[auto] 已点 Authorize，等 localhost 回调完成..."
    $waited = 0
    while ($job.State -eq "Running" -and $waited -lt 90) {
      Start-Sleep -Seconds 2; $waited += 2
    }
    Write-Output ("[auto] 回调等待结束，job 状态: " + $job.State)
    break
  }
  Start-Sleep -Milliseconds 1000
}

Write-Output ""
Write-Output "[auto] 收尾..."
$null = Wait-Job $job -Timeout 120
$jobCompleted = $job.State -eq 'Completed'
$results = @(Receive-Job $job)
$exitResult = $results | Where-Object { $_.PSObject.Properties['CcpickExitCode'] } | Select-Object -Last 1
$childExit = if ($jobCompleted -and $exitResult) { [int]$exitResult.CcpickExitCode } else { 1 }
$out = @($results | Where-Object { -not $_.PSObject.Properties['CcpickExitCode'] })
if ($job.State -eq "Running") { Write-Output "[auto] 警告：job 仍在运行，强制结束" }
Remove-Job $job -Force -ErrorAction SilentlyContinue

Write-Output "--- enroll 输出 ---"
$out | ForEach-Object { Write-Output ("  " + $_) }
Write-Output ""
Write-Output ("[auto] 阶段: signin={0}({1}次) google={2}({3}次) consent={6}次 authorize={4}({5}次)" -f
  $done.signin, $tries.signin, $done.google, $tries.google, $done.authorize, $tries.authorize, $tries.consent)

if ($accountBlocked) { exit 1 }
if ($childExit -ne 0) { exit $childExit }
& $cc usage --snapshot --reason ("enroll " + $Email) 2>&1 | Out-Null
exit 0
