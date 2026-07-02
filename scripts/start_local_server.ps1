param(
    [int]$Port = 8001,
    [string]$Python = "C:\Users\Yue\AppData\Local\Programs\Python\Python314\python.exe"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$stdoutLog = Join-Path $projectRoot "uvicorn.local.out.log"
$stderrLog = Join-Path $projectRoot "uvicorn.local.err.log"

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

$Python = Resolve-PythonExecutable $Python

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "Local server is already listening on http://127.0.0.1:$Port"
    return
}

Remove-Item -Force $stdoutLog, $stderrLog -ErrorAction SilentlyContinue

$process = Start-Process `
    -WindowStyle Hidden `
    -FilePath $Python `
    -ArgumentList @("-m", "uvicorn", "app.server:app", "--host", "127.0.0.1", "--port", "$Port") `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WorkingDirectory $projectRoot `
    -PassThru
Write-Host "uvicorn started. ProcessId: $($process.Id)"
Write-Host "Using Python: $Python"

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port" -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        # Keep waiting until uvicorn finishes startup or logs an error.
    }
}

if ($ready) {
    Write-Host "Local URL: http://127.0.0.1:$Port"
    Write-Host "Admin URL: http://127.0.0.1:$Port/admin"
} else {
    Write-Warning "Local server did not become ready yet."
    if ($process.HasExited) {
        Write-Warning "uvicorn exited with code $($process.ExitCode)."
    }
    if (Test-Path $stderrLog) {
        Write-Warning "Last stderr lines:"
        Get-Content $stderrLog -Tail 20 | ForEach-Object { Write-Warning $_ }
    }
}
