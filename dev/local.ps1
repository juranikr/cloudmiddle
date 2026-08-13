[CmdletBinding()]
param(
    [ValidateSet("up", "db", "status", "logs", "down", "reset")]
    [string]$Action = "up",

    [string]$EnvFile = "",

    [string]$ConfirmReset = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeArgs = @("compose")

if ($EnvFile) {
    $Candidate = if ([System.IO.Path]::IsPathRooted($EnvFile)) {
        $EnvFile
    } else {
        Join-Path $RepoRoot $EnvFile
    }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        throw "Env file not found: $Candidate"
    }
    $ComposeArgs += @("--env-file", (Resolve-Path -LiteralPath $Candidate).Path)
}
else {
    # Bypass Docker Compose's implicit .env loading. Local integration without
    # an explicit file must never inherit unrelated deployment secrets.
    $ComposeArgs += @("--env-file", (Join-Path $RepoRoot ".env.local.example"))
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & docker @ComposeArgs @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

Push-Location $RepoRoot
try {
    & docker info --format "{{.ServerVersion}}" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop is not running. Start it and retry."
    }
    Invoke-Compose config --quiet

    switch ($Action) {
        "up" {
            Invoke-Compose up --build --detach --wait
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:18000/api/health" -TimeoutSec 10
            if ($Health.status -ne "ok" -or $Health.db_mode -ne "local") {
                throw "Unexpected app health response; expected status=ok and db_mode=local."
            }
            Write-Host "Cloudmiddle local integration is ready: http://127.0.0.1:18000"
            Write-Host "Database: 127.0.0.1:55432 / cloudmiddle_local"
        }
        "db" {
            Invoke-Compose up --detach --wait db
            Write-Host "Local PostgreSQL is ready on 127.0.0.1:55432 (cloudmiddle_local)."
        }
        "status" {
            Invoke-Compose ps
        }
        "logs" {
            Invoke-Compose logs --tail 200 --follow app db
        }
        "down" {
            Invoke-Compose down --remove-orphans
            Write-Host "Containers stopped. The cloudmiddle_local_pgdata volume was preserved."
        }
        "reset" {
            if ($ConfirmReset -ne "RESET-cloudmiddle_local") {
                throw (
                    "Reset deletes the local PostgreSQL volume. Re-run with " +
                    "-ConfirmReset RESET-cloudmiddle_local"
                )
            }
            Invoke-Compose down --volumes --remove-orphans
            Write-Host "Deleted only the Compose-managed cloudmiddle_local PostgreSQL volume."
        }
    }
}
finally {
    Pop-Location
}
