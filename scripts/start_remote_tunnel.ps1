param(
    [int]$Port = 8001
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$toolsDir = Join-Path $projectRoot "tools"
$cloudflared = Join-Path $toolsDir "cloudflared.exe"
$stdoutLog = Join-Path $projectRoot "cloudflared.out.log"
$stderrLog = Join-Path $projectRoot "cloudflared.err.log"

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

if (-not (Test-Path $cloudflared)) {
    Write-Host "Downloading cloudflared..."
    Invoke-WebRequest `
        -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile $cloudflared
    Unblock-File $cloudflared
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    Write-Warning "No local service is listening on port $Port. Start uvicorn first, then run this script again."
}

Remove-Item -Force $stdoutLog, $stderrLog -ErrorAction SilentlyContinue

$process = Start-Process `
    -WindowStyle Hidden `
    -FilePath $cloudflared `
    -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate") `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WorkingDirectory $projectRoot `
    -PassThru

Write-Host "cloudflared started. ProcessId: $($process.Id)"
Write-Host "Waiting for the temporary URL..."

$url = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $logs = @()
    if (Test-Path $stderrLog) { $logs += Get-Content $stderrLog -ErrorAction SilentlyContinue }
    if (Test-Path $stdoutLog) { $logs += Get-Content $stdoutLog -ErrorAction SilentlyContinue }

    $match = $logs | Select-String -Pattern "https://[a-zA-Z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
    if ($match) {
        $url = $match.Matches[0].Value
        break
    }
}

if ($url) {
    Write-Host ""
    Write-Host "Temporary remote URL:"
    Write-Host $url
    Write-Host ""
    Write-Host "Keep this cloudflared process running while remote devices are testing."
} else {
    Write-Warning "No temporary URL was found yet. Check cloudflared.err.log for details."
}
