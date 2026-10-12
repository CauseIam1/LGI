# LGI Watchdog - GUARDED relauncher (Oct 4, 2026 / v2.1 manual-ignition mode).
# Manual-ignition contract (LGI.md section 10 item 9): LGI never self-starts at
# boot; the operator ignites it (desktop shortcut / schtasks /run "LGI Manual").
# This watcher respects that. It relaunches ONLY when LGI died while a live
# session marker (D:\LGI\.session_active) exists - that marker is written by
# lgi.py right after a successful start and removed on graceful exit:
#   pythonw pair alive      -> healthy, do nothing; a session that has run for
#                              10+ minutes expires the crash-loop streak file.
#   down + marker present   -> crash/force-kill of an ignited session ->
#                              guarded relaunch via the no-trigger "LGI Manual"
#                              Scheduled Task (interactive-safe, survives
#                              disconnects - never a bare background spawn).
#                              v2.1: the STALE marker is cleared right before
#                              the relaunch fires, so a relaunched session that
#                              dies BEFORE writing its own marker leaves no
#                              marker at all and the chain self-arrests; plus a
#                              crash-loop brake permits at most 3 guarded
#                              relaunches inside 10 minutes, then the watcher
#                              stands down to a cooldown slow-retry while the
#                              marker persists.
#   down + marker absent    -> operator intent (graceful quit, reboot, or LGI
#                              never started) -> stay down. This is what keeps
#                              boot-time silent in manual-ignition mode.
# Logs once per state transition to %TEMP%\lgi_watchdog.log - no 60 s spam.
# -TestGuard dry-runs the guard logic WITHOUT relaunching or touching state
# (own state + streak files):
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\LGI\watchdog.ps1 -TestGuard
param([switch]$TestGuard)
$ErrorActionPreference = 'SilentlyContinue'
$py = @(Get-Process pythonw -ErrorAction SilentlyContinue)
$alive = ($py.Count -ge 2)
if ($TestGuard) { $alive = $false }   # dry-run: pretend LGI is down
$marker = 'D:\LGI\.session_active'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$logFile = Join-Path 'D:\LGI\logs' 'lgi_watchdog.log'
$stateFile = if ($TestGuard) { Join-Path $env:TEMP 'lgi_watchdog_state_test.txt' }
             else { Join-Path $env:TEMP 'lgi_watchdog_state.txt' }
$streakFile = if ($TestGuard) { Join-Path $env:TEMP 'lgi_watchdog_streak_test.txt' }
              else { Join-Path $env:TEMP 'lgi_watchdog_streak.txt' }
$brakeMax = 3          # max guarded relaunches inside the brake window
$brakeWindowMin = 10   # brake window = cooldown between stood-down attempts
$healthyMin = 10       # an alive-for-this-long session expires the streak
function _Log([string]$msg) { Add-Content -Path $logFile -Value $msg }
function _Changed([string]$key) {
    $prev = Get-Content -Path $stateFile -ErrorAction SilentlyContinue
    Set-Content -Path $stateFile -Value $key
    return ($prev -ne $key)
}
function _StreakEntries {
    # Timestamped relaunch attempts younger than the brake window; older ones
    # expire on their own, which is what turns the brake into a slow-retry.
    $now = Get-Date
    $kept = @()
    if (Test-Path $streakFile) {
        foreach ($line in @(Get-Content $streakFile -ErrorAction SilentlyContinue)) {
            $t = [datetime]::MinValue
            if ([datetime]::TryParseExact($line, 'yyyy-MM-dd HH:mm:ss', $null,
                    [System.Globalization.DateTimeStyles]::None, [ref]$t) -and
                    ($now - $t).TotalMinutes -lt $brakeWindowMin) { $kept += $t }
        }
    }
    return @($kept)
}

if ($alive) {
    $oldest = ($py | Sort-Object StartTime | Select-Object -First 1).StartTime
    if (-not $TestGuard -and $oldest -and (Test-Path $streakFile) -and
            ((Get-Date) - $oldest).TotalMinutes -ge $healthyMin) {
        Remove-Item $streakFile -Force   # healthy 10m+ session: streak expired
        _Log "$stamp LGI healthy 10m+ (pythonw x$($py.Count)) - crash-loop streak expired"
    } elseif (_Changed 'up') {
        _Log "$stamp LGI healthy (pythonw x$($py.Count))"
    }
    exit 0
}
if (-not (Test-Path $marker)) {
    if (_Changed 'down-no-marker') {
        _Log "$stamp LGI down (pythonw x$($py.Count)), no session marker - staying down (manual-ignition guard)"
    }
    exit 0
}
# Down WITH a session marker: a previously ignited session died non-gracefully.
$streak = _StreakEntries
$braked = ($streak.Count -ge $brakeMax) -and -not $TestGuard
if (_Changed 'down-guarded') {
    $note = ''
    if ($TestGuard) { $note = ' [TESTGUARD dry-run - not relaunched]' }
    elseif ($braked) {
        $note = " - STOOD DOWN, crash-loop brake ($($streak.Count)/$brakeMax guarded relaunches inside ${brakeWindowMin}m); cooldown slow-retry re-arms while the marker persists"
    } else {
        $note = " (streak $($streak.Count)/$brakeMax)"
    }
    _Log "$stamp LGI down (pythonw x$($py.Count)) with session marker - guarded relaunch via 'LGI Manual'$note"
}
if ($TestGuard) { exit 0 }
if ($braked) { exit 0 }
$streak | ForEach-Object { $_.ToString('yyyy-MM-dd HH:mm:ss') } | Set-Content $streakFile
Add-Content -Path $streakFile -Value $stamp
Remove-Item $marker -Force   # v2.1: stale marker of the dead session - the
                             # fresh session writes its own once boot succeeds
_Log "$stamp stale session marker cleared - guarded relaunch fired (streak $(@(_StreakEntries).Count)/$brakeMax)"
schtasks /run /tn "LGI Manual" | Out-Null
