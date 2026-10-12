# crash_query.ps1 - last pythonw Application Errors + WER entries with
# faulting module details. Quoting-free run: powershell -File.
Write-Output "=== APPLICATION ERRORS (last 6) ==="
Get-WinEvent -FilterHashtable @{LogName = 'Application'; ProviderName = 'Application Error'} -MaxEvents 6 -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match 'pythonw' } |
    ForEach-Object {
        Write-Output ("--- {0} [{1}]" -f $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'), $_.Id)
        $lines = $_.Message -split "`n" | Select-Object -First 12
        $lines | ForEach-Object { Write-Output ("    " + $_.Trim()) }
    }
Write-Output "=== WER CRASHES (last 4) ==="
Get-WinEvent -FilterHashtable @{LogName = 'Application'; ProviderName = 'Windows Error Reporting'} -MaxEvents 20 -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match 'pythonw' } |
    Select-Object -First 4 |
    ForEach-Object {
        Write-Output ("--- {0} [{1}]" -f $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'), $_.Id)
        $lines = $_.Message -split "`n" | Select-Object -First 14
        $lines | ForEach-Object { Write-Output ("    " + $_.Trim()) }
    }