param([string]$ScriptPath = 'D:\LGI\lgi.py')
# LGI runner v1.1 (Oct 12 2026) - invoked by lgi_run_hidden.vbs (action of
# "LGI AutoStart" / "LGI Manual"). Starts venv pythonw.exe with REDIRECTED
# stdout/stderr so print()/tracebacks leave a persistent paper trail.
#   stdout -> D:\LGI\logs\lgi_runtime.log   (current session)
#             + .prev copy of the previous session is made at start
#   stderr -> D:\LGI\logs\lgi_stderr.log    (+ .prev copy)
# -ScriptPath exists for safe smoke tests against a dummy python script.
# Process image stays pythonw.exe: the watchdog counts pythonw (>=2 alive).
$ErrorActionPreference = 'Continue'
$logDir = 'D:\LGI\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$out = Join-Path $logDir 'lgi_runtime.log'
$err = Join-Path $logDir 'lgi_stderr.log'
foreach ($f in @($out, $err)) {
  if (Test-Path $f) { Copy-Item $f ($f + '.prev') -Force }
}
$py = 'D:\LGI\venv312\Scripts\pythonw.exe'
if (-not (Test-Path $py)) { Write-Output "pythonw missing: $py"; exit 1 }
if (-not (Test-Path $ScriptPath)) { Write-Output "script missing: $ScriptPath"; exit 1 }
Start-Process -FilePath $py -ArgumentList @('-u', $ScriptPath) -WorkingDirectory 'D:\LGI' -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
exit 0