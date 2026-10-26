$ErrorActionPreference = "SilentlyContinue"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot "run"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$dedicatedBotPython = Join-Path $projectRoot "vendor\telegram-python\WhisperTelegram.exe"

function Stop-ProcessTree([int]$processId) {
    $children = Get-CimInstance Win32_Process |
        Where-Object { $_.ParentProcessId -eq $processId }
    foreach ($child in $children) {
        Stop-ProcessTree $child.ProcessId
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

function Get-VerifiedBackendPortProcess {
    $connection = Get-NetTCPConnection `
        -LocalAddress "127.0.0.1" `
        -LocalPort 8765 `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $connection) {
        return $null
    }
    $owner = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$($connection.OwningProcess)" `
        -ErrorAction SilentlyContinue
    if (-not $owner) {
        return $null
    }
    $ownerCommand = [string]$owner.CommandLine
    if (
        $ownerCommand -notmatch "backend\.app:app" -or
        $ownerCommand -notmatch "--port\s+8765"
    ) {
        return $null
    }
    if ($owner.ExecutablePath -eq $pythonPath) {
        return $owner
    }
    $parent = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$($owner.ParentProcessId)" `
        -ErrorAction SilentlyContinue
    $parentCommand = if ($parent) { [string]$parent.CommandLine } else { "" }
    if (
        $parent -and
        $parent.ExecutablePath -eq $pythonPath -and
        $parentCommand -match "backend\.app:app" -and
        $parentCommand -match "--port\s+8765"
    ) {
        return $parent
    }
    return $null
}

# Телеграм-бот останавливается первым: он единственный, кто может в этот
# момент писать в чат про готовый файл, и делать это уже после остановки
# сервиса ему незачем.
foreach ($name in @("telegram", "frontend", "backend")) {
    $pidFile = Join-Path $runtimeDir "$name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }

    try {
        $servicePid = [int](Get-Content -LiteralPath $pidFile -Raw)
    }
    catch {
        $servicePid = 0
    }

    $process = if ($servicePid -gt 0) {
        Get-CimInstance Win32_Process -Filter "ProcessId=$servicePid" `
            -ErrorAction SilentlyContinue
    }
    else {
        $null
    }
    $commandLine = if ($process) { [string]$process.CommandLine } else { "" }
    $isExpected = switch ($name) {
        "backend" {
            $process -and
                $process.ExecutablePath -eq $pythonPath -and
                $commandLine -match "backend\.app:app"
        }
        "telegram" {
            $process -and
                $process.ExecutablePath -in @($pythonPath, $dedicatedBotPython) -and
                $commandLine -match "telegram_bot"
        }
        default {
            $process -and
                $commandLine -match [regex]::Escape(
                    (Join-Path $projectRoot "scripts\start_frontend.mjs")
                )
        }
    }

    if ($isExpected) {
        Stop-ProcessTree $servicePid
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# Если PID-файл потерян или устарел, всё равно завершаем только верифицированный
# uvicorn этого проекта. Это предотвращает «живой старый backend + новый
# backend, упавший на занятом порту» после перезапуска.
$orphanBackend = Get-VerifiedBackendPortProcess
if ($orphanBackend) {
    Stop-ProcessTree $orphanBackend.ProcessId
}

Remove-Item -LiteralPath (Join-Path $runtimeDir "frontend.port") `
    -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Локальный Whisper остановлен." -ForegroundColor Yellow
