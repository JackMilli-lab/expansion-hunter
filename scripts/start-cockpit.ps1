$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (!(Test-Path ".app-venv\Scripts\python.exe")) {
    python -m venv .app-venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.11 or newer is required." }
}
& .app-venv\Scripts\python.exe -m pip install .
if ($LASTEXITCODE -ne 0) { throw "Application dependency installation failed." }
Write-Host "Expansion Hunter: http://127.0.0.1:8000/cockpit"
Write-Host "Public market data only; no exchange orders."
& .app-venv\Scripts\python.exe -m expansion_hunter.cli app --host 127.0.0.1 --port 8000
