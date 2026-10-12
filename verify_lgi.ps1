# verify_lgi.ps1 — version-agnostic post-resurrection verification helper.
Start-Sleep -Seconds 60
Write-Host ==PROCS==
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*lgi.py*" } |
    Select-Object ProcessId, CommandLine | Format-List
Write-Host ==MARKER==
(Get-Item "D:\LGI\.session_active" -ErrorAction SilentlyContinue).LastWriteTime
Write-Host ==TAIL==
Get-Content "$env:TEMP\lgi_console.log" -Tail 26