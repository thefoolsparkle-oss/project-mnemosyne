param(
    [string]$OutputDir = "",
    [switch]$IncludeLogs,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $projectRoot "backups"
}
if (-not [System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = Join-Path $projectRoot $OutputDir
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = New-Item -ItemType Directory -Force -Path $OutputDir
$backupDir = Join-Path $backupRoot.FullName "mnemosyne-backup-$timestamp"
$null = New-Item -ItemType Directory -Force -Path $backupDir

$included = New-Object System.Collections.Generic.List[string]
$missing = New-Object System.Collections.Generic.List[string]

function Copy-BackupItem {
    param(
        [string]$RelativePath,
        [string]$TargetRelativePath = ""
    )
    $source = Join-Path $projectRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source)) {
        $missing.Add($RelativePath)
        return
    }
    if (-not $TargetRelativePath) {
        $TargetRelativePath = $RelativePath
    }
    $destination = Join-Path $backupDir $TargetRelativePath
    $destinationParent = Split-Path -Parent $destination
    if ($destinationParent) {
        $null = New-Item -ItemType Directory -Force -Path $destinationParent
    }
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
    $included.Add($RelativePath)
}

Copy-BackupItem "data\app.db"
Copy-BackupItem "data\app.db-wal"
Copy-BackupItem "data\app.db-shm"
Copy-BackupItem "data\uploads"
Copy-BackupItem "config.yaml"
Copy-BackupItem "README.md"
Copy-BackupItem "SYSTEM_PLAN.md"

if ($IncludeLogs) {
    Get-ChildItem -LiteralPath $projectRoot -Filter "*.log" -File -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-BackupItem $_.Name (Join-Path "logs" $_.Name)
    }
}

$checksums = [ordered]@{}
Get-ChildItem -LiteralPath $backupDir -File -Recurse | Sort-Object FullName | ForEach-Object {
    $relativePath = $_.FullName.Substring($backupDir.Length).TrimStart("\", "/")
    $normalizedPath = $relativePath.Replace("\", "/")
    $checksums[$normalizedPath] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}

$manifest = [ordered]@{
    schema = "mnemosyne_local_backup_v1"
    created_at = (Get-Date).ToString("o")
    project_root = $projectRoot
    backup_dir = $backupDir
    include_logs = [bool]$IncludeLogs
    included = @($included)
    missing = @($missing)
    checksums = $checksums
    notes = @(
        "This local backup intentionally excludes .env files and API keys.",
        "For a live SQLite database, app.db-wal and app.db-shm are copied when present."
    )
}
$manifestPath = Join-Path $backupDir "manifest.json"
$manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $manifestPath -Encoding UTF8

if ($Zip) {
    $zipPath = "$backupDir.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $backupDir "*") -DestinationPath $zipPath -Force
    Write-Host "Backup created: $backupDir"
    Write-Host "Archive created: $zipPath"
} else {
    Write-Host "Backup created: $backupDir"
}
Write-Host "Included: $($included.Count)"
if ($missing.Count) {
    Write-Host "Missing optional items: $($missing -join ', ')"
}
