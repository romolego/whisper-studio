param(
    [switch]$NoBrowser,
    [switch]$BackendOnly,
    [switch]$WithTelegram
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot "run"
$logsDir = Join-Path $projectRoot "logs"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$dedicatedBotPython = Join-Path $projectRoot "vendor\telegram-python\WhisperTelegram.exe"
$botPythonPath = if (Test-Path -LiteralPath $dedicatedBotPython) { $dedicatedBotPython } else { $pythonPath }
$cudaBin = Join-Path $projectRoot "vendor\cuda\bin"
$backendPidFile = Join-Path $runtimeDir "backend.pid"
$frontendPidFile = Join-Path $runtimeDir "frontend.pid"
$frontendPortFile = Join-Path $runtimeDir "frontend.port"

New-Item -ItemType Directory -Force -Path $runtimeDir, $logsDir | Out-Null

function Resolve-NodePath {
    # Путь к node.exe: PATH, затем обычные места установки. Жёсткий
    # C:\Program Files\nodejs подходил только этой машине.
    $command = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }
    $candidates = @(
        (Join-Path $env:ProgramFiles "nodejs\node.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "nodejs\node.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\nodejs\node.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Среда Python не найдена. Запустите «Установить Whisper.bat»."
}

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "dist"))) {
    throw "Веб-интерфейс ещё не собран. Запустите «Установить Whisper.bat»."
}

function Get-ServiceProcess(
    [string]$pidFile,
    [ValidateSet("backend", "frontend", "telegram")][string]$service
) {
    if (-not (Test-Path -LiteralPath $pidFile)) {
        return $null
    }
    try {
        $savedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    }
    catch {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" `
        -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    $commandLine = [string]$process.CommandLine
    $isExpected = switch ($service) {
        "backend" {
            $process.ExecutablePath -eq $pythonPath -and
                $commandLine -match "backend\.app:app"
        }
        "telegram" {
            $process.ExecutablePath -in @($pythonPath, $dedicatedBotPython) -and
                $commandLine -match "telegram_bot"
        }
        default {
            $commandLine -match [regex]::Escape(
                (Join-Path $projectRoot "scripts\start_frontend.mjs")
            )
        }
    }

    if (-not $isExpected) {
        # Windows can reuse a PID after an unclean shutdown. The unrelated
        # process must neither be accepted as Whisper nor be terminated.
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    return $process
}

function Wait-ForUrl([string]$url, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return $true
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Test-LocalPortAvailable([int]$port) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            $port
        )
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

function Get-FreeLocalPort([int]$preferredPort, [int[]]$reservedPorts) {
    foreach ($candidate in $preferredPort..($preferredPort + 30)) {
        if ($reservedPorts -contains $candidate) {
            continue
        }
        if (Test-LocalPortAvailable $candidate) {
            return $candidate
        }
    }
    throw "Не удалось найти свободный локальный порт для интерфейса."
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
    $ownerExpected = $ownerCommand -match "backend\.app:app" -and
        $ownerCommand -match "--port\s+8765"
    if (-not $ownerExpected) {
        return $null
    }
    if ($owner.ExecutablePath -eq $pythonPath) {
        return $owner
    }

    # Windows-venv запускает базовый python.exe дочерним процессом. PID-файл
    # должен указывать на launcher, чтобы stop.ps1 завершал всё дерево.
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

# Межпроцессная защита дополняет lock в Webinar Analyzer. Она нужна, если
# start.ps1 почти одновременно вызовут два разных процесса (например Analyzer
# и ручной запуск): проверка PID и запись backend.pid должны быть атомарны.
$backendStartMutex = [System.Threading.Mutex]::new(
    $false,
    "Local\WebinarAnalyzerWhisperBackend8765Start"
)
$backendStartMutexHeld = $false
try {
    try {
        $backendStartMutexHeld = $backendStartMutex.WaitOne(
            [TimeSpan]::FromSeconds(55)
        )
    }
    catch [System.Threading.AbandonedMutexException] {
        # Предыдущий владелец аварийно завершился; mutex уже принадлежит нам.
        $backendStartMutexHeld = $true
    }
    if (-not $backendStartMutexHeld) {
        throw "Истекло время ожидания блокировки запуска Whisper."
    }

    # Повторная проверка выполняется уже под mutex: первый процесс мог успеть
    # полностью поднять backend, пока второй ожидал блокировку.
    $backendProcess = Get-ServiceProcess $backendPidFile "backend"
    if (-not $backendProcess) {
        # После аварии PID-файл может исчезнуть, хотя проверенный процесс всё
        # ещё слушает порт. Подхватываем только именно наш uvicorn; чужой
        # процесс на 8765 не принимается за успешно запущенный Whisper.
        $backendProcess = Get-VerifiedBackendPortProcess
        if ($backendProcess) {
            Set-Content -LiteralPath $backendPidFile -Value $backendProcess.ProcessId
        }
        elseif (-not (Test-LocalPortAvailable 8765)) {
            throw (
                "Порт 8765 занят посторонним или неопознанным процессом. " +
                "Whisper не запущен."
            )
        }
    }
    if (-not $backendProcess) {
        $backendEnvironmentPath = $env:PATH
        if (Test-Path -LiteralPath $cudaBin) {
            $env:PATH = "$cudaBin;$env:PATH"
        }
        try {
            $backend = Start-Process `
                -FilePath $pythonPath `
                -ArgumentList @(
                    "-m",
                    "uvicorn",
                    "backend.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8765"
                ) `
                -WorkingDirectory $projectRoot `
                -RedirectStandardOutput (Join-Path $logsDir "backend.out.log") `
                -RedirectStandardError (Join-Path $logsDir "backend.err.log") `
                -WindowStyle Hidden `
                -PassThru
            Set-Content -LiteralPath $backendPidFile -Value $backend.Id
        }
        finally {
            $env:PATH = $backendEnvironmentPath
        }
    }

    if (-not (Wait-ForUrl "http://127.0.0.1:8765/api/health" 45)) {
        $backendLog = Join-Path $logsDir "backend.err.log"
        if (Test-Path -LiteralPath $backendLog) {
            Get-Content -LiteralPath $backendLog -Tail 20 | Write-Host
        }
        throw "Сервер распознавания не запустился. Проверьте logs\backend.err.log."
    }
    # Один ответ /health недостаточен: его мог вернуть старый процесс, пока
    # только что созданный уже завершился из-за занятого порта.
    $backendProcess = Get-ServiceProcess $backendPidFile "backend"
    if (-not $backendProcess) {
        throw (
            "Процесс запуска Whisper завершился, хотя порт 8765 отвечает. " +
            "Проверьте logs\backend.err.log."
        )
    }
}
finally {
    if ($backendStartMutexHeld) {
        $backendStartMutex.ReleaseMutex()
    }
    $backendStartMutex.Dispose()
}

if ($BackendOnly) {
    Write-Host ""
    Write-Host "Локальный Whisper API запущен: http://127.0.0.1:8765" -ForegroundColor Green
    exit 0
}

# Телеграм-бот. Условий два, и они про разное: токен в файле настроек бота
# отвечает, настроен ли он вообще, а telegram_autostart в настройках сервиса —
# нужен ли он сейчас. Так эксперимент выключается переключателем в интерфейсе,
# не стирая токен и не отзывая его у @BotFather. -WithTelegram запускает бота
# явно только на этот сеанс и не включает постоянную настройку автозапуска.
$botPidFile = Join-Path $runtimeDir "telegram.pid"
$botConfigPath = Join-Path $projectRoot "data" | Join-Path -ChildPath "telegram.json"
$botProcess = Get-ServiceProcess $botPidFile "telegram"
if (
    -not $botProcess -and
    (Test-Path -LiteralPath $botConfigPath) -and
    (Test-Path -LiteralPath (Join-Path $projectRoot "telegram_bot\__main__.py"))
) {
    $botConfigured = $false
    try {
        $botConfig = Get-Content -LiteralPath $botConfigPath -Raw |
            ConvertFrom-Json
        $botConfigured = -not [string]::IsNullOrWhiteSpace(
            [string]$botConfig.token
        )
    }
    catch {
        $botConfigured = $false
    }
    # Настройка лежит в базе сервиса, а не в файле бота: её меняют из
    # интерфейса. Backend к этому моменту уже отвечает — его дождались выше.
    $botAllowed = [bool]$WithTelegram
    if (-not $WithTelegram) {
        try {
            $serviceSettings = Invoke-RestMethod `
                -Uri "http://127.0.0.1:8765/api/settings" `
                -TimeoutSec 5
            $botAllowed = ([string]$serviceSettings.telegram_autostart -eq "on")
        }
        catch {
            # Ошибка чтения настроек не должна запускать отложенного бота.
            $botAllowed = $false
            Write-Warning "Не удалось прочитать настройку запуска Telegram; бот не запущен."
        }
    }

    if ($botConfigured -and $botAllowed) {
        $bot = Start-Process `
            -FilePath $botPythonPath `
            -ArgumentList @("-m", "telegram_bot") `
            -WorkingDirectory $projectRoot `
            -RedirectStandardOutput (Join-Path $logsDir "telegram.out.log") `
            -RedirectStandardError (Join-Path $logsDir "telegram.err.log") `
            -WindowStyle Hidden `
            -PassThru
        Set-Content -LiteralPath $botPidFile -Value $bot.Id
    }
}

$frontendProcess = Get-ServiceProcess $frontendPidFile "frontend"
$publicPort = 3000
if ($frontendProcess -and (Test-Path -LiteralPath $frontendPortFile)) {
    try {
        $publicPort = [int](Get-Content -LiteralPath $frontendPortFile -Raw)
    }
    catch {
        $publicPort = 3000
    }
}

if (-not $frontendProcess) {
    $publicPort = Get-FreeLocalPort 3000 @(3001, 8765)
    $internalPort = Get-FreeLocalPort 3001 @($publicPort, 8765)
    $nodePath = Resolve-NodePath
    if (-not $nodePath) {
        throw "Node.js не найден. Запустите «Установить Whisper.bat»."
    }
    $previousPublicPort = $env:WHISPER_PUBLIC_PORT
    $previousInternalPort = $env:WHISPER_INTERNAL_PORT
    $env:WHISPER_PUBLIC_PORT = [string]$publicPort
    $env:WHISPER_INTERNAL_PORT = [string]$internalPort
    try {
        $frontend = Start-Process `
            -FilePath $nodePath `
            -ArgumentList @(
                (Join-Path $projectRoot "scripts\start_frontend.mjs")
            ) `
            -WorkingDirectory $projectRoot `
            -RedirectStandardOutput (Join-Path $logsDir "frontend.out.log") `
            -RedirectStandardError (Join-Path $logsDir "frontend.err.log") `
            -WindowStyle Hidden `
            -PassThru
        Set-Content -LiteralPath $frontendPidFile -Value $frontend.Id
        Set-Content -LiteralPath $frontendPortFile -Value $publicPort
    }
    finally {
        $env:WHISPER_PUBLIC_PORT = $previousPublicPort
        $env:WHISPER_INTERNAL_PORT = $previousInternalPort
    }
}

$frontendUrl = "http://127.0.0.1:$publicPort"
if (-not (Wait-ForUrl $frontendUrl 45)) {
    $frontendLog = Join-Path $logsDir "frontend.err.log"
    if (Test-Path -LiteralPath $frontendLog) {
        Get-Content -LiteralPath $frontendLog -Tail 20 | Write-Host
    }
    throw "Веб-интерфейс не запустился. Проверьте logs\frontend.err.log."
}

if (-not $NoBrowser) {
    Start-Process $frontendUrl
}
Write-Host ""
Write-Host "Локальный Whisper запущен: $frontendUrl" -ForegroundColor Green
