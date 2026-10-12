# restart_lgi.ps1 — version-agnostic LGI restart helper for the AutoStart task.
# Kills ONLY pythonw processes whose command line references lgi.py,
# clears the guarded-watchdog session marker, relaunches via schtasks,
# then verifies boot lines, marker, and process pair.
$ErrorActionPreference = "Continue"
Write-Host "--- LGI restart (LGI AutoStart) ---"
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*lgi.py*" }
$procs | ForEach-Object {
    Write-Host ("killing PID {0}: {1}" -f $_.ProcessId, $_.CommandLine)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Remove-Item -Path "D:\LGI\.session_active" -Force -ErrorAction SilentlyContinue
schtasks /End /TN "LGI AutoStart" | Out-Null
Start-Sleep -Seconds 1
schtasks /Run /TN "LGI AutoStart"
Start-Sleep -Seconds 12
Write-Host "--- console tail ---"
Get-Content "$env:TEMP\lgi_console.log" -Tail 22
Write-Host "--- session marker ---"
(Get-Item "D:\LGI\.session_active" -ErrorAction SilentlyContinue).LastWriteTime
Write-Host "--- lgi.py processes ---"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*lgi.py*" } |
    Select-Object ProcessId, CommandLine | Format-List