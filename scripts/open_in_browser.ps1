<#
.SYNOPSIS
    Opens a URL in the default browser once the local server is reachable.

.DESCRIPTION
    runner.bat learns the Cloudflare URL before the web server has started
    listening, because the tunnel is created first and only then does
    `python -m web.server` run. Opening the browser at that moment shows
    an error page, so this waits for the server to actually accept a
    connection on its local port and only then opens the URL.

    The public tunnel URL is preferred, because Instagram has to fetch the
    video from a public address. If no tunnel is available it falls back
    to the local URL so the UI still opens.

    Failure to open a browser is never fatal - the URL is always printed
    in the runner's own console.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\open_in_browser.ps1 -Port 8000 -Url "https://x.trycloudflare.com"
#>
param(
    [Parameter(Mandatory = $true)]
    [int]$Port,

    [string]$Url,

    # How long to wait for the server to start listening.
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = 'SilentlyContinue'

# Without a tunnel there is nothing public to open, so use the local
# address - the UI is still fully usable locally.
if (-not $Url) {
    $Url = "http://localhost:$Port"
}

function Test-ServerReady {
    param([int]$Port)

    # A TCP connect is enough: it proves the listener is accepting, which
    # is what the browser needs, and avoids depending on the UI path.
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', $Port)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

while ((Get-Date) -lt $deadline) {
    if (Test-ServerReady -Port $Port) { break }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-ServerReady -Port $Port)) {
    Write-Host "Server on port $Port did not come up within $TimeoutSeconds s - not opening a browser."
    Write-Host "Open this manually once the server is ready: $Url"
    exit 0
}

Write-Host "Opening $Url"

Start-Process $Url