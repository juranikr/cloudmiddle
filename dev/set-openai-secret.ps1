# Run interactively on Windows. Never pass the API key on the command line.
[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()]
    [string]$SecretId = "tourmiddle-dev/app",
    [ValidatePattern('^[a-z]{2}(-[a-z]+)+-\d+$')]
    [string]$Region = "ap-northeast-2"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-SecretAws {
    param([string[]]$AwsArguments)
    $awsOutput = & aws @AwsArguments --region $Region --output json --no-cli-pager 2>$null | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "AWS Secrets Manager request failed. Check your AWS identity, region, and permissions."
    }
    try {
        return ($awsOutput | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw "AWS returned an unexpected response; no secret contents will be displayed."
    }
}

function Read-AppSecret {
    param([string]$VersionId)
    $readArguments = @("secretsmanager", "get-secret-value", "--secret-id", $SecretId)
    if ($VersionId) {
        $readArguments += @("--version-id", $VersionId)
    }
    $response = Invoke-SecretAws -AwsArguments $readArguments
    if (-not ($response.PSObject.Properties.Name -contains "SecretString")) {
        throw "The selected secret is not a JSON SecretString. Nothing was changed."
    }
    try {
        $parsed = $response.SecretString | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "The selected secret contains invalid JSON. Nothing was changed."
    }
    if ($null -eq $parsed -or $parsed -isnot [PSCustomObject]) {
        throw "The selected secret must be a JSON object. Nothing was changed."
    }
    return $parsed
}

if ($env:OS -ne "Windows_NT") {
    throw "This helper requires Windows ACLs. Use it from an interactive Windows PowerShell session."
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "Install AWS CLI and authenticate before running this helper."
}

$secureKey = $null
$keyPointer = [IntPtr]::Zero
$plainKey = $null
$secretObject = $null
$tempDirectory = $null
$tempFile = $null
$secretJson = $null

try {
    # Verify read access before prompting, then re-read after entry to merge the latest fields.
    $null = Read-AppSecret
    Write-Host "Updating only OPENAI_API_KEY in '$SecretId' ($Region). Existing fields will be retained."
    $secureKey = Read-Host "OpenAI API key (hidden)" -AsSecureString
    if ($secureKey.Length -eq 0) {
        throw "The API key cannot be empty. Nothing was changed."
    }
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($plainKey) -or $plainKey -match '\s') {
        throw "The API key must not contain whitespace. Nothing was changed."
    }

    $secretObject = Read-AppSecret
    $preservedFields = @{}
    foreach ($property in $secretObject.PSObject.Properties) {
        if ($property.Name -ne "OPENAI_API_KEY") {
            $preservedFields[$property.Name] = ConvertTo-Json -InputObject $property.Value -Depth 100 -Compress
        }
    }
    $secretObject | Add-Member -NotePropertyName "OPENAI_API_KEY" -NotePropertyValue $plainKey -Force
    $secretJson = ConvertTo-Json -InputObject $secretObject -Depth 100 -Compress

    # Create an empty random directory, lock it to this Windows identity, then write.
    # The key never appears in process arguments, shell history, or script output.
    $tempDirectory = Join-Path ([IO.Path]::GetTempPath()) ("cloudmiddle-secret-" + [Guid]::NewGuid().ToString("N"))
    $null = New-Item -ItemType Directory -Path $tempDirectory
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $directoryAcl = New-Object Security.AccessControl.DirectorySecurity
    $directoryAcl.SetAccessRuleProtection($true, $false)
    $directoryAcl.SetOwner($currentUser)
    $directoryRule = New-Object Security.AccessControl.FileSystemAccessRule(
        $currentUser,
        [Security.AccessControl.FileSystemRights]::FullControl,
        ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $directoryAcl.AddAccessRule($directoryRule)
    Set-Acl -LiteralPath $tempDirectory -AclObject $directoryAcl

    $tempFile = Join-Path $tempDirectory "secret.json"
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($tempFile, $secretJson, $utf8)
    $tempAcl = Get-Acl -LiteralPath $tempFile
    $tempAcl.SetAccessRuleProtection($true, $true)
    Set-Acl -LiteralPath $tempFile -AclObject $tempAcl

    $writeResponse = Invoke-SecretAws -AwsArguments @(
        "secretsmanager", "put-secret-value", "--secret-id", $SecretId,
        "--secret-string", ("file://" + $tempFile),
        "--client-request-token", [Guid]::NewGuid().ToString()
    )
    if (-not ($writeResponse.PSObject.Properties.Name -contains "VersionId")) {
        throw "The write response did not include a version ID. Verify the secret field before deployment."
    }

    # Validate the exact version just written without printing any field values.
    $verified = Read-AppSecret -VersionId $writeResponse.VersionId
    $verifiedKey = $verified.PSObject.Properties["OPENAI_API_KEY"]
    if ($null -eq $verifiedKey -or [string]$verifiedKey.Value -cne $plainKey) {
        throw "OPENAI_API_KEY field validation failed. Do not enable the Codex backend yet."
    }
    foreach ($fieldName in $preservedFields.Keys) {
        $verifiedField = $verified.PSObject.Properties[$fieldName]
        if ($null -eq $verifiedField -or (ConvertTo-Json -InputObject $verifiedField.Value -Depth 100 -Compress) -cne $preservedFields[$fieldName]) {
            throw "Existing-field validation failed. Inspect the secret version history before deployment."
        }
    }
    Write-Host "Validated: OPENAI_API_KEY is present; all existing fields were preserved. No key values were displayed."
    Write-Host "This does not deploy or restart ECS. Keep agent_schedule_enabled=false until manual validation succeeds."
}
catch {
    # Never include exception details: parsing/CLI diagnostics could contain secret material.
    Write-Error "Secret update did not complete validation. No secret values will be displayed. Check AWS identity/permissions and the secret version history before retrying."
    exit 1
}
finally {
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    if ($null -ne $secureKey) {
        $secureKey.Dispose()
    }
    if ($tempFile -and [IO.File]::Exists($tempFile)) {
        [IO.File]::Delete($tempFile)
    }
    if ($tempDirectory -and [IO.Directory]::Exists($tempDirectory)) {
        [IO.Directory]::Delete($tempDirectory)
    }
    $plainKey = $null
    $secretJson = $null
    $secretObject = $null
}
