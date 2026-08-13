[CmdletBinding()]
param(
    [switch]$SkipDocker,

    [string]$EnvFile = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendRoot = Join-Path $RepoRoot "backend"
$FrontendRoot = Join-Path $RepoRoot "frontend"
$Python = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$LocalScript = Join-Path $PSScriptRoot "local.ps1"

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "[predeploy] $Label"
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Missing backend virtualenv: $Python"
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw "npm.cmd was not found on PATH."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git was not found on PATH."
}

Invoke-CheckedNative `
    -Label "backend unittest suite" `
    -WorkingDirectory $BackendRoot `
    -FilePath $Python `
    -Arguments @("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py")

Invoke-CheckedNative `
    -Label "frontend production build" `
    -WorkingDirectory $FrontendRoot `
    -FilePath "npm.cmd" `
    -Arguments @("run", "build")

Invoke-CheckedNative `
    -Label "Python compileall" `
    -WorkingDirectory $BackendRoot `
    -FilePath $Python `
    -Arguments @("-m", "compileall", "-q", "app", "tools")

Invoke-CheckedNative `
    -Label "git diff check" `
    -WorkingDirectory $RepoRoot `
    -FilePath "git" `
    -Arguments @("diff", "--check")

if (-not $SkipDocker) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker was not found on PATH. Re-run with -SkipDocker for host-only checks."
    }

    $ResolvedEnvFile = if ($EnvFile) {
        $Candidate = if ([System.IO.Path]::IsPathRooted($EnvFile)) {
            $EnvFile
        }
        else {
            Join-Path $RepoRoot $EnvFile
        }
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            throw "Env file not found: $Candidate"
        }
        (Resolve-Path -LiteralPath $Candidate).Path
    }
    else {
        Join-Path $RepoRoot ".env.local.example"
    }

    Invoke-CheckedNative `
        -Label "Docker Compose configuration" `
        -WorkingDirectory $RepoRoot `
        -FilePath "docker" `
        -Arguments @("compose", "--env-file", $ResolvedEnvFile, "config", "--quiet")

    Write-Host "[predeploy] local Docker integration startup"
    if ($EnvFile) {
        & $LocalScript -Action up -EnvFile $ResolvedEnvFile
    }
    else {
        & $LocalScript -Action up
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Local Docker integration startup failed with exit code $LASTEXITCODE"
    }

    $ApiBase = "http://127.0.0.1:18000"
    Write-Host "[predeploy] local API health and authentication smoke"
    $HealthResponse = Invoke-WebRequest `
        -UseBasicParsing `
        -Uri "$ApiBase/api/health" `
        -TimeoutSec 15
    $Health = $HealthResponse.Content | ConvertFrom-Json
    $ModeHeader = [string]$HealthResponse.Headers["X-Cloudmiddle-DB-Mode"]
    if (
        $HealthResponse.StatusCode -ne 200 `
        -or $Health.status -ne "ok" `
        -or $Health.db_mode -ne "local" `
        -or $ModeHeader -ne "local"
    ) {
        throw "Local health must report status=ok and db_mode/header=local."
    }

    $LoginBody = @{
        email = "test@test.com"
        password = "test1234"
    } | ConvertTo-Json
    $Login = Invoke-RestMethod `
        -Method Post `
        -Uri "$ApiBase/api/auth/login" `
        -ContentType "application/json" `
        -Body $LoginBody `
        -TimeoutSec 15
    $Token = [string]$Login.access_token
    if (-not $Token) {
        throw "Local test login did not return an access token."
    }
    $AuthHeaders = @{ Authorization = "Bearer $Token" }
    $Me = Invoke-RestMethod `
        -Method Get `
        -Uri "$ApiBase/api/auth/me" `
        -Headers $AuthHeaders `
        -TimeoutSec 15
    if ($Me.email -ne "test@test.com" -or $Me.is_admin -ne $true) {
        throw "Local /api/auth/me did not return the expected admin test account."
    }
    $Cities = @(
        Invoke-RestMethod `
            -Method Get `
            -Uri "$ApiBase/api/cities" `
            -Headers $AuthHeaders `
            -TimeoutSec 15
    )
    if ($Cities.Count -lt 1) {
        throw "Local /api/cities returned no cities."
    }

    Write-Host "[predeploy] Docker smoke passed; local containers remain available at $ApiBase"
}
else {
    Write-Host "[predeploy] Docker checks skipped by -SkipDocker."
}

Write-Host "[predeploy] PASS"
