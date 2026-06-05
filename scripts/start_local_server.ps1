param(
    [int]$Port = 8001,
    [string]$Python = "C:\Users\Yue\AppData\Local\Programs\Python\Python314\python.exe"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "Local server is already listening on http://127.0.0.1:$Port"
    return
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Python
$psi.Arguments = "-m uvicorn app.server:app --host 127.0.0.1 --port $Port"
$psi.WorkingDirectory = $projectRoot
$psi.UseShellExecute = $true
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

$process = [System.Diagnostics.Process]::Start($psi)
Write-Host "uvicorn started. ProcessId: $($process.Id)"

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
}
