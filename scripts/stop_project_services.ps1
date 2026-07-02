param(
    [string[]]$Ports = @("8000", "8001", "8002")
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stopped = @()

foreach ($portValue in $Ports) {
    foreach ($portText in ([string]$portValue).Split(",")) {
        $portText = $portText.Trim()
        if (-not $portText) { continue }
        $port = [int]$portText
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $commandLine = [string]$process.CommandLine
        if ($commandLine -like "*uvicorn app.server:app*" -and $commandLine -like "*$projectRoot*") {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction Stop
            $stopped += "uvicorn:$($listener.OwningProcess):$port"
        }
    }
    }
}

$cloudflared = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue
foreach ($process in $cloudflared) {
    $commandLine = [string]$process.CommandLine
    if ($commandLine -like "*trycloudflare*" -or $commandLine -like "* tunnel *") {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        $stopped += "cloudflared:$($process.ProcessId)"
    }
}

if ($stopped.Count) {
    Write-Host "Stopped:"
    $stopped | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "No Mnemosyne local services were running on the checked ports."
}
