param(
    [string]$BindAddress = "",
    [ValidateRange(1024, 65535)][int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & (Join-Path $PSScriptRoot "start.ps1") -NoBrowser
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "Studio failed to start." }
    $arguments = @("-m", "scripts.remote_adapter", "--port", "$Port")
    if ($BindAddress) { $arguments += @("--bind", $BindAddress) }
    & (Join-Path $projectRoot ".venv\Scripts\python.exe") @arguments
    $adapterExit = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $adapterExit
