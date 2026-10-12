'LGI Watchdog hidden launcher (v1.3.1a, Sep 30 2026).
'wscript.exe is a GUI-subsystem host: no console window ever appears, and
'Run window style 0 hides the child powershell too. This is the action of
'the "LGI Watchdog" Scheduled Task (every 60 s): wscript.exe D:\LGI\watchdog_hidden.vbs
'Errors are appended to %TEMP%\lgi_watchdog_launcher.log instead of popping
'an error dialog (a WSH runtime error would otherwise flash a dialog box
'every minute, recreating the very problem this launcher fixes).
On Error Resume Next
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\LGI\watchdog.ps1""", 0, False
If Err.Number <> 0 Then
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set log = fso.OpenTextFile(fso.BuildPath(fso.GetSpecialFolder(2), "lgi_watchdog_launcher.log"), 8, True)
    log.WriteLine Year(Now) & "-" & Month(Now) & "-" & Day(Now) & " " & Time & " launcher error 0x" & Hex(Err.Number) & ": " & Err.Description
    log.Close
End If