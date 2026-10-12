# =====================================================================
# crash_analyze.ps1 v1.0 - LGI native-crash postmortem driver (X17)
# ---------------------------------------------------------------------
# Companion to LGI.md SS11 WATCH (flaky native crash class: ntdll
# 0xc0000374 heap corruption / ucrtbase 0xc0000409 fail-fast, silent to
# Python) and to the second-instance GPU-race dump pythonw.exe.30564.dmp.
# Prepares and drives WinDbg !analyze -v postmortems BEFORE the next
# event strikes, and audits crash-readiness on demand.
#
# Modes (combine freely; default with no switches = -Posture):
#   -Posture         readiness audit: live session pair/marker, watchdog
#                    task, WER LocalDumps capture config, dump inventory,
#                    debugger presence, report/symbol dirs. Read-only.
#   -EnsureDumps     pin HKCU WER LocalDumps for pythonw.exe: full dumps
#                    (DumpType=2), keep 15, %LOCALAPPDATA%\CrashDumps -
#                    future captures never rely on Windows defaults.
#   -Install         stage a debugger: winget Microsoft.WinDbg first,
#                    then the Windows SDK Debuggers-only fallback
#                    (OptionId.WindowsDesktopDebuggers). Never fires a
#                    UAC prompt itself - prints the elevated one-liner.
#   -Latest [-Wait]  analyze the newest pythonw dump
#   -Dump <name>     analyze one dump (file name in CrashDumps or full
#                    path)
#   -All [-Keep n]   analyze the newest n dumps (default 3; skips dumps
#                    with an existing newer .analysis.log verdict)
#   -Wait            block until cdb exits and print the verdict extract
#                    (default = async launch + poll recipe; first runs
#                    may download Microsoft symbols, allow minutes)
#
# Side effects: writes only under D:\LGI\crash_reports\ and
# D:\LGI\symcache\ (plus HKCU WER keys with -EnsureDumps). Never touches
# the live session, the watchdog, the session marker, or LGI code.
#
# Exit codes: 0 ok; 2 debugger missing (run -Install); 3 dump not found;
#             4 dump locked/still growing; 1 unexpected.
# Strict ASCII / CRLF - matches watchdog.ps1 v2.1 conventions.
# =====================================================================

[CmdletBinding()]
param(
    [switch]$Posture,
    [switch]$EnsureDumps,
    [switch]$Install,
    [switch]$Latest,
    [string]$Dump,
    [switch]$All,
    [int]$Keep = 3,
    [switch]$Wait
)

$ErrorActionPreference = 'Continue'
$LgiRoot     = 'D:\LGI'
$ReportsDir  = Join-Path $LgiRoot 'crash_reports'
$SymCacheDir = Join-Path $LgiRoot 'symcache'
$DumpDir     = Join-Path $env:LOCALAPPDATA 'CrashDumps'
$SdkFwLinks  = @(
    'https://go.microsoft.com/fwlink/?linkid=2289980',
    'https://go.microsoft.com/fwlink/?linkid=2196859'
)

# ---------------------------------------------------------------- helpers

function Find-Debugger {
    foreach ($p in @(
        'C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe',
        'C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\kd.exe',
        'C:\Program Files\Windows Kits\10\Debuggers\x64\cdb.exe',
        'C:\Program Files\Windows Kits\11\Debuggers\x64\cdb.exe'
    )) {
        if (Test-Path $p) { return $p }
    }
    foreach ($name in @('cdb.exe', 'kd.exe')) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    $pkg = Get-AppxPackage Microsoft.WinDbg -ErrorAction SilentlyContinue
    if ($pkg) {
        foreach ($rel in @('amd64\cdb.exe', 'x64\cdb.exe', 'cdb.exe', 'amd64\kd.exe')) {
            $p = Join-Path $pkg.InstallLocation $rel
            if (Test-Path $p) { return $p }
        }
    }
    return $null
}

function Test-DumpReadable {
    param([string]$Path)
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'None')
        if ($fs) { $fs.Close() }
        return $true
    } catch {
        return $false
    }
}

function Get-DumpFiles {
    param([int]$Top = 3)
    if (-not (Test-Path $DumpDir)) { return @() }
    @(Get-ChildItem $DumpDir -Filter 'pythonw*.dmp' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First $Top)
}

# ------------------------------------------------------------- capture pin

function Enable-DumpConfig {
    $key = 'HKCU:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\pythonw.exe'
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    Set-ItemProperty -Path $key -Name 'DumpFolder'      -Value '%LOCALAPPDATA%\CrashDumps' -Type ExpandString
    Set-ItemProperty -Path $key -Name 'DumpType'        -Value 2  -Type DWord
    Set-ItemProperty -Path $key -Name 'DumpCount'       -Value 15 -Type DWord
    Set-ItemProperty -Path $key -Name 'CustomDumpFlags' -Value 0  -Type DWord
    Write-Output '[ENSURE] HKCU LocalDumps\pythonw.exe pinned: DumpFolder=%LOCALAPPDATA%\CrashDumps, DumpType=2 (full), DumpCount=15, CustomDumpFlags=0'
}

# ------------------------------------------------------------- installer

function Invoke-DebuggerInstall {
    $dbg = Find-Debugger
    if ($dbg) {
        Write-Output ('[INSTALL] debugger already present: ' + $dbg)
        exit 0
    }
    Write-Output '[INSTALL] no debugger found - trying winget Microsoft.WinDbg (per-user MSIX, normally no UAC)...'
    try {
        $wingetOut = winget install -e --id Microsoft.WinDbg --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-String
        Write-Output ($wingetOut.TrimEnd())
    } catch {
        Write-Output ('[INSTALL] winget call failed: ' + $_.Exception.Message)
    }
    Start-Sleep -Seconds 3
    $dbg = Find-Debugger
    if ($dbg) {
        Write-Output ('[INSTALL] console debugger usable after winget install: ' + $dbg)
        exit 0
    }
    Write-Output '[INSTALL] the WinDbg MSIX exposes no console cdb.exe (UI-only payload).'
    Write-Output '[INSTALL] falling back to the Windows SDK Debuggers-only install (one UAC click required).'
    $boot = Join-Path $LgiRoot 'winsdksetup.exe'
    $ok = $false
    foreach ($url in $SdkFwLinks) {
        try {
            Write-Output ('[INSTALL] downloading SDK bootstrapper: ' + $url)
            Invoke-WebRequest -Uri $url -OutFile $boot -UseBasicParsing -TimeoutSec 120
            if ((Get-Item $boot).Length -gt 512KB) { $ok = $true; break }
        } catch {
            Write-Output ('[INSTALL] download failed: ' + $_.Exception.Message)
        }
    }
    if ($ok) {
        Write-Output ('[INSTALL] bootstrapper staged: ' + $boot)
        Write-Output '[INSTALL] ELEVATED step (one UAC click at the X17 - pick ONE):'
        Write-Output ('  A) elevated cmd:  ' + $boot + ' /q /norestart /features OptionId.WindowsDesktopDebuggers')
        Write-Output '  B) Explorer:      right-click winsdksetup.exe -> Run as administrator -> select ONLY "Debugging Tools for Windows"'
        Write-Output '[INSTALL] afterwards cdb.exe lands at C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe - rerun -Posture to confirm.'
    } else {
        Write-Output '[INSTALL] bootstrapper staging failed - manual route: https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/debugger-download-tools (Windows SDK, Debugging Tools feature only).'
    }
    exit 2
}

# ------------------------------------------------------------- analysis

function Invoke-DumpAnalysis {
    param([string]$Debugger, [string]$DumpFile, [switch]$Block)
    if (-not (Test-Path $ReportsDir)) {
        New-Item -ItemType Directory -Path $ReportsDir -Force | Out-Null
    }
    if (-not (Test-Path $SymCacheDir)) {
        New-Item -ItemType Directory -Path $SymCacheDir -Force | Out-Null
    }
    $base   = [System.IO.Path]::GetFileNameWithoutExtension($DumpFile)
    $report = Join-Path $ReportsDir ($base + '.analysis.log')
    if ((Test-Path $report) -and ((Get-Item $report).LastWriteTime -gt (Get-Item $DumpFile).LastWriteTime)) {
        Write-Output ('[ANALYZE] verdict newer than dump - skipping ' + $base + ' (delete the .analysis.log to force)')
        return
    }
    if (-not (Test-DumpReadable $DumpFile)) {
        Write-Output ('[ANALYZE] dump locked - WER may still be writing ' + $base + '; wait ~1 min and rerun')
        exit 4
    }
    $symPath = 'srv*' + $SymCacheDir + '*https://msdl.microsoft.com/download/symbols'
    $cdbCmds = '.symfix+ ' + $SymCacheDir + '; .reload; !analyze -v; .logclose; q'
    $cdbArgs = @('-y', $symPath, '-z', $DumpFile, '-logo', $report, '-c', ('"' + $cdbCmds + '"'))
    if (Test-Path $report) { Remove-Item $report -Force -ErrorAction SilentlyContinue }
    Write-Output ('[ANALYZE] cdb ' + $Debugger)
    Write-Output ('[ANALYZE] dump   ' + $DumpFile)
    Write-Output ('[ANALYZE] report ' + $report)
    if ($Block) {
        Write-Output '[ANALYZE] blocking mode - first run may download Microsoft symbols (allow minutes)...'
        $proc = Start-Process -FilePath $Debugger -ArgumentList $cdbArgs -WindowStyle Hidden -Wait -PassThru
        Write-Output ('[ANALYZE] cdb exit code: ' + $proc.ExitCode)
        Show-Verdict -Report $report
    } else {
        $proc = Start-Process -FilePath $Debugger -ArgumentList $cdbArgs -WindowStyle Hidden -PassThru
        Write-Output ('[ANALYZE] launched async - cdb pid ' + $proc.Id)
        Write-Output ('[ANALYZE] poll:  Get-Process -Id ' + $proc.Id + '  ;  Get-Content -Tail 30 -Path ' + $report)
    }
}

# ------------------------------------------------------------- verdict

function Show-Verdict {
    param([string]$Report)
    if (-not (Test-Path $Report)) {
        Write-Output ('[VERDICT] report missing: ' + $Report)
        return
    }
    Write-Output ('===== VERDICT EXTRACT - ' + (Split-Path $Report -Leaf) + ' =====')
    Select-String -Path $Report -Pattern 'EXCEPTION_CODE|DEFAULT_BUCKET_ID|FAILURE_BUCKET_ID|SYMBOL_NAME|MODULE_NAME|IMAGE_NAME|PROCESS_NAME|FAULTING_THREAD|PRIMARY_PROBLEM_CLASS' |
        ForEach-Object { Write-Output ($_.Line.Trim()) }
    $stackLine = Select-String -Path $Report -Pattern 'STACK_TEXT:' | Select-Object -First 1
    if ($stackLine) {
        Write-Output '--- STACK_TEXT (top frames) ---'
        Get-Content $Report | Select-Object -Skip $stackLine.LineNumber -First 13 | ForEach-Object { Write-Output $_ }
    }
    Write-Output ('===== full verbose log: ' + $Report + ' =====')
}

# ------------------------------------------------------------- posture

function Show-Posture {
    Write-Output ('[POSTURE] LGI native-crash readiness @ ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    $procs = @(Get-Process pythonw -ErrorAction SilentlyContinue)
    $marker = Get-Content (Join-Path $LgiRoot '.session_active') -ErrorAction SilentlyContinue
    if ($marker) { $marker = ($marker | Select-Object -First 1) }
    if ($procs.Count -eq 2 -and $marker) {
        Write-Output ('[ 1] live session : pythonw x2, marker ' + $marker + '  -> OK (v1.5.1 session live)')
    } elseif ($procs.Count -eq 0 -and -not $marker) {
        Write-Output '[ 1] live session : down with no marker  -> OK (graceful/manual-off; guarded watchdog stays down)'
    } else {
        Write-Output ('[ 1] live session : pythonw x' + $procs.Count + ', marker ' + $marker + '  -> CHECK (unexpected pair/marker combination)')
    }
    $task = Get-ScheduledTask -TaskName 'LGI Watchdog' -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName 'LGI Watchdog'
        Write-Output ('[ 2] watchdog     : ' + $task.State + ', last ' + $info.LastRunTime + ', next ' + $info.NextRunTime)
    } else {
        Write-Output '[ 2] watchdog     : MISSING  -> CHECK (LGI.md SS10 item 9 contract)'
    }
    $capLines = @()
    foreach ($spec in @(
        @('HKCU:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\pythonw.exe', 'HKCU pythonw.exe'),
        @('HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\pythonw.exe', 'HKLM pythonw.exe'),
        @('HKCU:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps', 'HKCU default'),
        @('HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps', 'HKLM default')
    )) {
        $pv = Get-ItemProperty $spec[0] -ErrorAction SilentlyContinue
        $names = @(); if ($pv) { $names = @($pv.PSObject.Properties.Name) }
        if (($names -contains 'DumpType') -or ($names -contains 'DumpFolder')) {
            $capLines += ($spec[1] + ': DumpFolder=' + $pv.DumpFolder + ' DumpType=' + $pv.DumpType + ' DumpCount=' + $pv.DumpCount)
        }
    }
    if ($capLines.Count -gt 0) {
        foreach ($line in $capLines) { Write-Output ('[ 3] dump capture : ' + $line) }
    } else {
        Write-Output '[ 3] dump capture : no explicit LocalDumps config - current captures ride Windows default WER behavior; -EnsureDumps pins it (HKCU pythonw.exe, full dumps, keep 15)'
    }
    $dumps = @(Get-DumpFiles 100)
    if ($dumps.Count -gt 0) {
        Write-Output ('[ 4] dump inventory: ' + $dumps.Count + ' pythonw dumps in ' + $DumpDir)
        foreach ($d in ($dumps | Select-Object -First 5)) {
            Write-Output ('      - ' + $d.Name + '  ' + [int]($d.Length / 1MB) + ' MB  @ ' + $d.LastWriteTime.ToString('yyyy-MM-dd HH:mm'))
        }
    } else {
        Write-Output ('[ 4] dump inventory: no pythonw dumps in ' + $DumpDir)
    }
    $dbg = Find-Debugger
    if ($dbg) {
        Write-Output ('[ 5] debugger     : ' + $dbg + '  -> OK')
    } else {
        Write-Output '[ 5] debugger     : NOT FOUND  -> run -Install (winget first, Windows-SDK Debuggers fallback; one UAC click for that fallback path)'
    }
    Write-Output ('[ 6] reports dir  : ' + $ReportsDir + '  (exists: ' + (Test-Path $ReportsDir) + ')')
    Write-Output ('[ 7] symbol cache : ' + $SymCacheDir + '  (exists: ' + (Test-Path $SymCacheDir) + '; first analysis may download MS symbols)')
    if ($dbg) {
        Write-Output '[VERDICT] READY - analyze now: -Latest (newest dump), -Dump <name>, or -All -Keep 3'
    } else {
        Write-Output '[VERDICT] NEEDS DEBUGGER - run: powershell -ExecutionPolicy Bypass -File D:\LGI\crash_analyze.ps1 -Install'
    }
}

# ------------------------------------------------------------- dispatch

if ($EnsureDumps) { Enable-DumpConfig }
if ($Install)     { Invoke-DebuggerInstall }   # always exits

$requested = @()
if ($Dump) {
    $candidate = $Dump
    if (-not (Test-Path $candidate)) { $candidate = Join-Path $DumpDir $Dump }
    if (-not (Test-Path $candidate)) {
        Write-Output ('[ANALYZE] dump not found: ' + $Dump)
        exit 3
    }
    $requested = @(Get-Item $candidate)
} elseif ($Latest) {
    $requested = @(Get-DumpFiles 1)
} elseif ($All) {
    $requested = @(Get-DumpFiles $Keep)
}

if ($Posture -or ($requested.Count -eq 0)) { Show-Posture }

if ($requested.Count -eq 0) { exit 0 }

$dbg = Find-Debugger
if (-not $dbg) {
    Write-Output '[ANALYZE] no debugger found - run: powershell -ExecutionPolicy Bypass -File D:\LGI\crash_analyze.ps1 -Install'
    exit 2
}

foreach ($d in $requested) {
    Invoke-DumpAnalysis -Debugger $dbg -DumpFile $d.FullName -Block:$Wait
}

exit 0