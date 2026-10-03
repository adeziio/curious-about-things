<#
.SYNOPSIS
    Stops only the Cloudflare tunnel that belongs to this project.

.DESCRIPTION
    Several projects run side by side, each with its own cloudflared
    process. Killing them by image name would tear down every other
    project's tunnel, so this matches on the local port the tunnel
    serves - which is unique per project because it matches that
    project's own web server.

    Harmless when this project never started a tunnel: it simply
    matches nothing.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass `
        -File scripts\stop_tunnel.ps1 -Port 8000
#>
param(
    [Parameter(Mandatory = $true)]
    [int]$Port
)

$ErrorActionPreference = 'SilentlyContinue'

# CommandLine LIKE is unreliable in the WMI provider here (it matches
# nothing even for a bare wildcard), so filter in PowerShell instead.
$processes = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq 'cloudflared.exe' -and
        $_.CommandLine -like "*localhost:$Port*"
    }

if (-not $processes) {

    Write-Host "No tunnel running for port $Port."

    exit 0
}

foreach ($process in $processes) {

    Write-Host "Stopping tunnel for port $Port (PID $($process.ProcessId))."

    # /T also ends the console wrapper that launched cloudflared.
    Stop-Process -Id $process.ProcessId -Force
}