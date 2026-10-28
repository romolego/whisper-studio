$ErrorActionPreference = "Stop"
try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8765/api/remote/host/stop" -TimeoutSec 10 | Out-Null
    Write-Host "Remote adapter stopped. Local Studio remains running."
}
catch {
    Write-Error "Could not contact Studio. If Studio is stopped, the adapter is also stopped."
    exit 1
}
