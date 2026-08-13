[CmdletBinding()]
param(
    [string]$AwsSecretId = "",
    [string]$AwsRegion = "",
    [string]$SourceHost = "",
    [string]$SourceDatabase = "tourmiddle",
    [string]$SourceUser = "tourmiddle",
    [string]$Confirmation = "",
    [switch]$DryRun,
    [switch]$RetainPrivateContent,
    [switch]$StrictSourceRole
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendRoot = Join-Path $RepoRoot "backend"
$Python = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$OutputsPath = Join-Path $RepoRoot "infra\outputs.json"

function Get-TerraformOutputValue {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Test-Path -LiteralPath $OutputsPath -PathType Leaf)) {
        return ""
    }
    $Outputs = Get-Content -LiteralPath $OutputsPath -Raw | ConvertFrom-Json
    $Entry = $Outputs.PSObject.Properties[$Name]
    if ($null -eq $Entry -or $null -eq $Entry.Value.value) {
        return ""
    }
    return [string]$Entry.Value.value
}
if (-not $AwsSecretId) {
    $AwsSecretId = Get-TerraformOutputValue -Name "app_secret_arn"
}
if (-not $AwsRegion) {
    $AwsRegion = Get-TerraformOutputValue -Name "aws_region"
}
if (-not $AwsRegion) {
    $AwsRegion = "ap-northeast-2"
}
if (-not $SourceHost) {
    $SourceHost = Get-TerraformOutputValue -Name "rds_endpoint"
}

if (-not $AwsSecretId) {
    throw "AWS secret id is required (or generate infra/outputs.json)."
}
if (-not $SourceHost) {
    throw "Production RDS host is required (or generate infra/outputs.json)."
}
if ($SourceHost -notmatch '^[A-Za-z0-9.-]+$') {
    throw "SourceHost must be one DNS hostname without a port or URL scheme."
}
if ($AwsRegion -notmatch '^[a-z0-9-]+$') {
    throw "AwsRegion contains unsafe characters."
}
if ($AwsSecretId -notmatch '^[A-Za-z0-9_+=,.@:/-]+$') {
    throw "AwsSecretId contains unsafe characters."
}
foreach ($Identifier in @($SourceDatabase, $SourceUser)) {
    if ($Identifier -notmatch '^[A-Za-z_][A-Za-z0-9_$]{0,62}$') {
        throw "Source database/user must be a safe PostgreSQL identifier."
    }
}
if (-not $DryRun -and $Confirmation -ne "RESET-cloudmiddle_local") {
    throw (
        "This replaces only the allow-listed local database. Re-run with " +
        "-Confirmation RESET-cloudmiddle_local"
    )
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Backend virtualenv is missing: $Python"
}

if (-not $DryRun) {
    & docker info --format "{{.ServerVersion}}" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop is not running. Start it and retry."
    }
    & (Join-Path $PSScriptRoot "local.ps1") -Action db
}

$CloneEnvironment = [ordered]@{
    PROD_CLONE_AWS_SECRET_ID = $AwsSecretId
    PROD_CLONE_AWS_REGION = $AwsRegion
    PROD_CLONE_AWS_SECRET_KEY = "DATABASE_URL"
    PROD_CLONE_TARGET_URL = (
        "postgresql://cloudmiddle_local:cloudmiddle_local_only@" +
        "127.0.0.1:55432/cloudmiddle_local?sslmode=disable"
    )
    PROD_CLONE_ALLOWED_SOURCE_HOSTS = $SourceHost
    PROD_CLONE_ALLOWED_SOURCE_DBS = $SourceDatabase
    PROD_CLONE_ALLOWED_SOURCE_USERS = $SourceUser
    PROD_CLONE_ALLOWED_TARGET_DBS = "cloudmiddle_local"
    PROD_CLONE_ALLOWED_TARGET_USERS = "cloudmiddle_local"
    PROD_CLONE_ALLOWED_TARGET_PORTS = "55432"
    PROD_CLONE_LOCAL_ADMIN_EMAIL = "test@test.com"
    PROD_CLONE_LOCAL_ADMIN_PASSWORD = "test1234"
    PROD_CLONE_TOOL_MODE = "docker"
    PROD_CLONE_DOCKER_IMAGE = "postgres:16-alpine"
    PROD_CLONE_STRICT_SOURCE_ROLE = $(if ($StrictSourceRole) { "true" } else { "false" })
}
$PreviousEnvironment = @{}
foreach ($Name in $CloneEnvironment.Keys) {
    $PreviousEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    [Environment]::SetEnvironmentVariable(
        $Name,
        [string]$CloneEnvironment[$Name],
        "Process"
    )
}

$Arguments = @("-m", "tools.dev.prod_db_clone")
if ($DryRun) {
    $Arguments += "--dry-run"
}
else {
    $Arguments += @("--confirm", $Confirmation)
}
if ($RetainPrivateContent) {
    $Arguments += "--retain-private-content"
    Write-Warning (
        "RetainPrivateContent keeps production chats, notes, appeals, plans, and " +
        "agent traces locally. Account emails and all password hashes are still replaced."
    )
}

Push-Location $BackendRoot
try {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Production database clone failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
    foreach ($Name in $CloneEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable(
            $Name,
            $PreviousEnvironment[$Name],
            "Process"
        )
    }
}
