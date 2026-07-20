$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\backend")

if (-not (Test-Path ".\.venv\Scripts\uvicorn.exe")) {
    Write-Host "Creating Python 3.11 venv..."
    py -3.11 -m venv .venv
    .\.venv\Scripts\pip.exe install -r requirements.txt
}

Write-Host "Starting API on http://127.0.0.1:8000 (SQLite)"
.\.venv\Scripts\uvicorn.exe app.main:app --reload --host 0.0.0.0 --port 8000
