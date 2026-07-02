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
    Invoke-Checked "node web app check" { node --check web\app.js }
    Invoke-Checked "node admin app check" { node --check admin_web\admin.js }
} finally {
    Pop-Location
}

Write-Host "All local verification checks passed."
