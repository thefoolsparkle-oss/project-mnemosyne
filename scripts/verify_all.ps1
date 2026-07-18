param(
    [string]$Python = "C:\Users\Yue\AppData\Local\Programs\Python\Python314\python.exe"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

function Test-PythonExecutable {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    try {
        $output = & $Candidate -c "import sys; print(sys.executable)" 2>$null
        return $LASTEXITCODE -eq 0 -and $output
    } catch {
        return $false
    }
}

function Resolve-PythonExecutable {
    param([string]$Preferred)
    $candidates = @()
    if ($Preferred) { $candidates += $Preferred }
    $localPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"
    $candidates += $localPython
    foreach ($name in @("python.exe", "python3.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-PythonExecutable $candidate) { return $candidate }
    }
    throw "No working Python executable was found. Pass -Python with the full path to python.exe."
}

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Test-PowerShellScriptSyntax {
    param([string[]]$Paths)
    foreach ($path in $Paths) {
        $tokens = $null
        $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $path), [ref]$tokens, [ref]$errors) | Out-Null
        if ($errors.Count) {
            $messages = $errors | ForEach-Object { "${path}: $($_.Message)" }
            throw ($messages -join [Environment]::NewLine)
        }
    }
}

function Assert-Condition {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Invoke-BackupSmokeTest {
    $tempRoot = [System.IO.Path]::GetTempPath()
    $backupOutput = Join-Path $tempRoot ("mnemosyne-backup-smoke-" + [guid]::NewGuid().ToString("N"))
    $restoreOutput = Join-Path $tempRoot ("mnemosyne-backup-restore-" + [guid]::NewGuid().ToString("N"))
    $resolvedOutput = $null
    $resolvedRestore = $null
    try {
        & powershell.exe -ExecutionPolicy Bypass -File "scripts\backup_local_data.ps1" -OutputDir $backupOutput
        if ($LASTEXITCODE -ne 0) {
            throw "backup_local_data.ps1 failed with exit code $LASTEXITCODE."
        }

        $resolvedOutput = (Resolve-Path -LiteralPath $backupOutput).Path
        Assert-Condition $resolvedOutput.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) "Backup smoke output escaped the temp directory."

        $latest = Get-ChildItem -LiteralPath $backupOutput -Directory |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        Assert-Condition ($null -ne $latest) "Backup smoke did not create a backup directory."

        $manifestPath = Join-Path $latest.FullName "manifest.json"
        Assert-Condition (Test-Path -LiteralPath $manifestPath) "Backup smoke did not create manifest.json."

        $manifest = Get-Content -Raw -Encoding utf8 -LiteralPath $manifestPath | ConvertFrom-Json
        Assert-Condition ($manifest.schema -eq "mnemosyne_local_backup_v1") "Backup manifest schema is unexpected."
        Assert-Condition (($manifest.notes -join " ") -match "excludes \.env files and API keys") "Backup manifest lost the sensitive-file exclusion note."

        foreach ($required in @("config.yaml", "README.md", "SYSTEM_PLAN.md")) {
            Assert-Condition ($manifest.included -contains $required) "Backup manifest missing required item: $required."
            Assert-Condition (Test-Path -LiteralPath (Join-Path $latest.FullName $required)) "Backup output missing required file: $required."
            $checksum = $manifest.checksums.PSObject.Properties[$required].Value
            Assert-Condition ($checksum -match "^[0-9a-f]{64}$") "Backup manifest missing checksum for required file: $required."
        }

        & powershell.exe -ExecutionPolicy Bypass -File "scripts\verify_local_backup.ps1" -BackupDir $latest.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "verify_local_backup.ps1 rejected the generated backup with exit code $LASTEXITCODE."
        }

        $null = New-Item -ItemType Directory -Force -Path $restoreOutput
        Copy-Item -Path (Join-Path $latest.FullName "*") -Destination $restoreOutput -Recurse -Force
        $resolvedRestore = (Resolve-Path -LiteralPath $restoreOutput).Path
        Assert-Condition $resolvedRestore.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) "Backup restore rehearsal escaped the temp directory."
        & powershell.exe -ExecutionPolicy Bypass -File "scripts\verify_local_backup.ps1" -BackupDir $resolvedRestore
        if ($LASTEXITCODE -ne 0) {
            throw "verify_local_backup.ps1 rejected the temporary restore rehearsal with exit code $LASTEXITCODE."
        }

        Set-Content -LiteralPath (Join-Path $resolvedRestore "README.md") -Value "tampered backup smoke file" -Encoding utf8
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $null = & powershell.exe -ExecutionPolicy Bypass -File "scripts\verify_local_backup.ps1" -BackupDir $resolvedRestore 2>&1
            $tamperExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($tamperExitCode -eq 0) {
            throw "verify_local_backup.ps1 accepted a tampered restore rehearsal."
        }
        $global:LASTEXITCODE = 0
    } finally {
        if ($resolvedRestore -and $resolvedRestore.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedRestore -Recurse -Force -ErrorAction SilentlyContinue
        } elseif (Test-Path -LiteralPath $restoreOutput) {
            $restoreCandidate = (Resolve-Path -LiteralPath $restoreOutput).Path
            if ($restoreCandidate.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                Remove-Item -LiteralPath $restoreCandidate -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        if ($resolvedOutput -and $resolvedOutput.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedOutput -Recurse -Force -ErrorAction SilentlyContinue
        } elseif (Test-Path -LiteralPath $backupOutput) {
            $candidate = (Resolve-Path -LiteralPath $backupOutput).Path
            if ($candidate.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

$Python = Resolve-PythonExecutable $Python
Write-Host "Using Python: $Python"

Push-Location $projectRoot
try {
    Invoke-Checked "compileall" { & $Python -m compileall app scripts }
    Invoke-Checked "verify_phase1_flows" { & $Python scripts\verify_phase1_flows.py }
    Invoke-Checked "verify_phase2_growth" { & $Python scripts\verify_phase2_growth.py }
    Invoke-Checked "verify_phase3_expression" { & $Python scripts\verify_phase3_expression.py }
    Invoke-Checked "verify_group_chat" { & $Python scripts\verify_group_chat.py }
    Invoke-Checked "verify_http_smoke" { & $Python scripts\verify_http_smoke.py --in-process }
    Invoke-Checked "verify_llm_config" { & $Python scripts\verify_llm_config.py }
    Invoke-Checked "diagnose_llm_env" { & $Python scripts\diagnose_llm_env.py }
    Invoke-Checked "node web app check" { node --check web\app.js }
    Invoke-Checked "node admin app check" { node --check admin_web\admin.js }
    Invoke-Checked "powershell script syntax" {
        Test-PowerShellScriptSyntax @(
            "scripts\start_local_server.ps1",
            "scripts\start_remote_tunnel.ps1",
            "scripts\stop_project_services.ps1",
            "scripts\backup_local_data.ps1",
            "scripts\verify_local_backup.ps1"
        )
    }
    Invoke-Checked "backup local data smoke" { Invoke-BackupSmokeTest }
} finally {
    Pop-Location
}

Write-Host "All local verification checks passed."
