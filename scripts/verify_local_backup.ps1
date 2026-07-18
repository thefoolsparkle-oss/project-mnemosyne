param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDir
)

$ErrorActionPreference = "Stop"

function Assert-BackupCondition {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function ConvertTo-BackupRelativePath {
    param([string]$Value)
    $normalized = ([string]$Value).Replace('\', '/').TrimStart([char[]]@(47))
    Assert-BackupCondition (-not [string]::IsNullOrWhiteSpace($normalized)) "Backup manifest contains an empty file path."
    Assert-BackupCondition (-not [System.IO.Path]::IsPathRooted($normalized)) "Backup manifest contains an absolute file path: $Value"
    Assert-BackupCondition (-not ($normalized -split "/" | Where-Object { $_ -eq ".." })) "Backup manifest contains an escaping file path: $Value"
    return $normalized
}

$resolvedBackupDir = (Resolve-Path -LiteralPath $BackupDir -ErrorAction Stop).Path
Assert-BackupCondition ((Get-Item -LiteralPath $resolvedBackupDir).PSIsContainer) "Backup path is not a directory: $resolvedBackupDir"

$manifestPath = Join-Path $resolvedBackupDir "manifest.json"
Assert-BackupCondition (Test-Path -LiteralPath $manifestPath -PathType Leaf) "Backup manifest.json is missing."
$manifest = Get-Content -Raw -Encoding utf8 -LiteralPath $manifestPath | ConvertFrom-Json
Assert-BackupCondition ($manifest.schema -eq "mnemosyne_local_backup_v1") "Unsupported backup manifest schema."
Assert-BackupCondition ($null -ne $manifest.checksums) "Backup manifest does not contain checksums."

$declaredChecksums = @{}
foreach ($property in $manifest.checksums.PSObject.Properties) {
    $relativePath = ConvertTo-BackupRelativePath $property.Name
    $checksum = [string]$property.Value
    Assert-BackupCondition ($checksum -match "^[0-9a-f]{64}$") "Invalid SHA-256 checksum for $relativePath."
    Assert-BackupCondition (-not $declaredChecksums.ContainsKey($relativePath)) "Backup manifest declares $relativePath more than once."
    $declaredChecksums[$relativePath] = $checksum
}
Assert-BackupCondition ($declaredChecksums.Count -gt 0) "Backup manifest contains no checksummed files."

$actualFiles = @{}
Get-ChildItem -LiteralPath $resolvedBackupDir -File -Recurse | ForEach-Object {
    $relativePath = $_.FullName.Substring($resolvedBackupDir.Length).TrimStart([char[]]@(92, 47)).Replace('\', '/')
    if ($relativePath -ne "manifest.json") {
        $actualFiles[$relativePath] = $_.FullName
    }
}

foreach ($relativePath in $declaredChecksums.Keys) {
    Assert-BackupCondition ($actualFiles.ContainsKey($relativePath)) "Backup file is missing: $relativePath"
    $actualChecksum = (Get-FileHash -LiteralPath $actualFiles[$relativePath] -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-BackupCondition ($actualChecksum -eq $declaredChecksums[$relativePath]) "Backup checksum mismatch: $relativePath"
}
foreach ($relativePath in $actualFiles.Keys) {
    Assert-BackupCondition ($declaredChecksums.ContainsKey($relativePath)) "Backup contains an undeclared file: $relativePath"
    Assert-BackupCondition ($relativePath -notmatch "(^|/)\.env($|\.)") "Backup must not contain environment files: $relativePath"
}

foreach ($required in @("config.yaml", "README.md", "SYSTEM_PLAN.md")) {
    Assert-BackupCondition ($manifest.included -contains $required) "Backup manifest missing required item: $required"
    Assert-BackupCondition ($actualFiles.ContainsKey($required)) "Backup missing required file: $required"
}

if ($actualFiles.ContainsKey("data/app.db")) {
    $stream = [System.IO.File]::OpenRead($actualFiles["data/app.db"])
    try {
        $header = New-Object byte[] 16
        $read = $stream.Read($header, 0, $header.Length)
    } finally {
        $stream.Dispose()
    }
    $sqliteHeader = [System.Text.Encoding]::ASCII.GetString($header, 0, $read)
    Assert-BackupCondition ($sqliteHeader -eq "SQLite format 3$([char]0)") "Backup database does not have a SQLite header."
}

Write-Host "Backup verification passed: $resolvedBackupDir ($($declaredChecksums.Count) files)"
