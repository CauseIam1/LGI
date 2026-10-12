'LGI hidden launcher v1.1 (Oct 12 2026) - action of "LGI AutoStart" / "LGI Manual".
'Mirrors the battle-tested watchdog_hidden.vbs v1.3.1a pattern: wscript.exe is a
'GUI-subsystem host, so no console window ever appears, and Run window style 0
'hides the child powershell too. powershell runs D:\LGI\lgi_run.ps1, which starts
'venv pythonw.exe with redirected stdout/stderr (paper trail in
'D:\LGI\logs\lgi_runtime.log / lgi_stderr.log). Image name stays pythonw.exe so
'the watchdog pythonw-count guard is unaffected.
'Launcher errors append to %TEMP%\lgi_run_launcher.log instead of error dialogs.
On Error Resume Next
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\LGI\lgi_run.ps1""", 0, False
If Err.Number <> 0 Then
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set log = fso.OpenTextFile(fso.BuildPath(fso.GetSpecialFolder(2), "lgi_run_launcher.log"), 8, True)
    log.WriteLine Year(Now) & "-" & Month(Now) & "-" & Day(Now) & " " & Time & " launcher error 0x" & Hex(Err.Number) & ": " & Err.Description
    log.Close
End If