# Pure helpers: safe to dot-source from PowerShell 5.1 and installer tests.
function Get-InstallDevice([string]$Requested, [bool]$HasNvidia) {
    if ($Requested -eq "cuda" -and -not $HasNvidia) {
        throw "NVIDIA GPU/driver not found. Install the driver or use -Device cpu."
    }
    if ($Requested -ne "auto") { return $Requested }
    if ($HasNvidia) { return "cuda" }
    return "cpu"
}

function ConvertTo-NativeArgument([string]$Value) {
    # Windows CommandLineToArgvW rules, including quotes and trailing slashes.
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}
