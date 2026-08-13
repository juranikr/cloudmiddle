[CmdletBinding()]
param(
    [ValidateSet("Register", "Unregister")]
    [string]$Action = "Register",
    [string]$TaskName = "Cloudmiddle Production DB Clone",
    [string]$DailyAt = "04:30",
    [string]$AwsSecretId = "",
    [string]$AwsRegion = "",
    [string]$SourceHost = "",
    [string]$SourceDatabase = "tourmiddle",
    [string]$SourceUser = "tourmiddle",
    [switch]$RetainPrivateContent,
    [switch]$StrictSourceRole
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$CloneScript = Join-Path $PSScriptRoot "clone-production-db.ps1"
$OutputsPath = Join-Path $RepoRoot "infra\outputs.json"

if ($Action -eq "Unregister") {
    $Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $Existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task: $TaskName"
    }
    else {
        Write-Host "Scheduled task does not exist: $TaskName"
    }
    return
}

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
if (-not $AwsSecretId -or -not $SourceHost) {
    throw "AwsSecretId and SourceHost are required (or generate infra/outputs.json)."
}
if ($DailyAt -notmatch '^(?:[01][0-9]|2[0-3]):[0-5][0-9]$') {
    throw "DailyAt must use 24-hour HH:mm format."
}
foreach ($Value in @($AwsSecretId, $AwsRegion, $SourceHost, $SourceDatabase, $SourceUser)) {
    if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "Scheduled-task arguments contain unsafe characters."
    }
}

# Validate AWS access, the secret URL, and all source/target allow-lists before
# persisting a scheduled task.  The dry-run never connects to either database.
$DryRunParameters = @{
    AwsSecretId = $AwsSecretId
    AwsRegion = $AwsRegion
    SourceHost = $SourceHost
    SourceDatabase = $SourceDatabase
    SourceUser = $SourceUser
    DryRun = $true
}
if ($RetainPrivateContent) {
    $DryRunParameters.RetainPrivateContent = $true
}
if ($StrictSourceRole) {
    $DryRunParameters.StrictSourceRole = $true
}
& $CloneScript @DryRunParameters

$PowerShellArguments = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", ('"' + $CloneScript + '"'),
    "-AwsSecretId", ('"' + $AwsSecretId + '"'),
    "-AwsRegion", ('"' + $AwsRegion + '"'),
    "-SourceHost", ('"' + $SourceHost + '"'),
    "-SourceDatabase", ('"' + $SourceDatabase + '"'),
    "-SourceUser", ('"' + $SourceUser + '"'),
    "-Confirmation", "RESET-cloudmiddle_local"
)
if ($RetainPrivateContent) {
    $PowerShellArguments += "-RetainPrivateContent"
}
if ($StrictSourceRole) {
    $PowerShellArguments += "-StrictSourceRole"
}

$Executable = Join-Path $PSHOME "powershell.exe"
$ActionParameters = @{
    Execute = $Executable
    Argument = ($PowerShellArguments -join " ")
    WorkingDirectory = $RepoRoot
}
$ScheduledAction = New-ScheduledTaskAction @ActionParameters
$At = [DateTime]::ParseExact(
    $DailyAt,
    "HH:mm",
    [Globalization.CultureInfo]::InvariantCulture
)
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$SettingsParameters = @{
    StartWhenAvailable = $true
    RunOnlyIfNetworkAvailable = $true
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
    MultipleInstances = "IgnoreNew"
    ExecutionTimeLimit = (New-TimeSpan -Hours 2)
    RestartCount = 2
    RestartInterval = (New-TimeSpan -Minutes 15)
}
$Settings = New-ScheduledTaskSettingsSet @SettingsParameters
$PrincipalParameters = @{
    UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    LogonType = "Interactive"
    RunLevel = "Limited"
}
$Principal = New-ScheduledTaskPrincipal @PrincipalParameters

$RegisterParameters = @{
    TaskName = $TaskName
    Description = (
        "Clone production PostgreSQL into the isolated local Docker database; " +
        "sanitize every account and use staging/atomic swap."
    )
    Action = $ScheduledAction
    Trigger = $Trigger
    Settings = $Settings
    Principal = $Principal
    Force = $true
}
Register-ScheduledTask @RegisterParameters | Out-Null

Write-Host "Registered daily task '$TaskName' at $DailyAt."
Write-Host "It runs only in this interactive Windows account so Docker Desktop and AWS profile access are available."
