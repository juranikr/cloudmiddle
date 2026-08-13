[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 18001
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendRoot = Join-Path $RepoRoot "backend"
$Python = Join-Path $BackendRoot ".venv\Scripts\python.exe"

if (-not $env:PROD_READONLY_DATABASE_URL) {
    throw (
        "Set PROD_READONLY_DATABASE_URL in this shell to a PostgreSQL URL for a DB role " +
        "that has SELECT-only grants. Do not reuse DATABASE_URL."
    )
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Missing backend virtualenv: $Python"
}

$HadMode = Test-Path Env:APP_DB_MODE
$OldMode = $env:APP_DB_MODE
$HadUrl = Test-Path Env:DATABASE_URL
$OldUrl = $env:DATABASE_URL
$HadResearch = Test-Path Env:AGENT_AUTONOMOUS_RESEARCH
$OldResearch = $env:AGENT_AUTONOMOUS_RESEARCH
$HadJwtSecret = Test-Path Env:JWT_SECRET
$OldJwtSecret = $env:JWT_SECRET

try {
    $env:APP_DB_MODE = "production_readonly"
    $env:DATABASE_URL = $env:PROD_READONLY_DATABASE_URL
    $env:AGENT_AUTONOMOUS_RESEARCH = "false"
    $TokenBytes = New-Object byte[] 48
    $Random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Random.GetBytes($TokenBytes) } finally { $Random.Dispose() }
    $env:JWT_SECRET = [Convert]::ToBase64String($TokenBytes)

    Push-Location $BackendRoot
    try {
        & $Python -c (
            "from app.config import settings; " +
            "assert settings.is_production_readonly; " +
            "print('production_readonly configuration accepted')"
        )
        if ($LASTEXITCODE -ne 0) {
            throw "The production_readonly configuration guard rejected this URL."
        }
        Write-Host "Starting SELECT-only diagnostic API on http://127.0.0.1:$Port"
        Write-Host "Startup migrations/seeding are disabled; only the login POST is permitted."
        & $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
        if ($LASTEXITCODE -ne 0) {
            throw "Diagnostic API exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($HadMode) { $env:APP_DB_MODE = $OldMode } else { Remove-Item Env:APP_DB_MODE -ErrorAction SilentlyContinue }
    if ($HadUrl) { $env:DATABASE_URL = $OldUrl } else { Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue }
    if ($HadResearch) { $env:AGENT_AUTONOMOUS_RESEARCH = $OldResearch } else { Remove-Item Env:AGENT_AUTONOMOUS_RESEARCH -ErrorAction SilentlyContinue }
    if ($HadJwtSecret) { $env:JWT_SECRET = $OldJwtSecret } else { Remove-Item Env:JWT_SECRET -ErrorAction SilentlyContinue }
}
