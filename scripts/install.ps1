param(
    [ValidateSet("auto", "cpu", "cuda")]
    [string]$Device = "auto",
    [ValidateSet("auto", "cu126", "cu128")]
    [string]$CudaVariant = "auto",
    [switch]$Plan,
    [switch]$SkipDiarization,
    [switch]$SkipFrontend
)

# Установка сервиса на чистой машине.
#
# Скрипт написан под Windows PowerShell 5.1, а не под PowerShell 7, и это не
# случайность: на свежем ноутбуке седьмого ещё нет, а установить его — одна из
# задач самого скрипта. Отсюда отсутствие тернарных операторов, `??` и прочего
# синтаксиса, который 5.1 не понимает.
#
# Скрипт идемпотентен: повторный запуск ничего не ломает и не переустанавливает
# то, что уже стоит. Это важнее краткости — его будут запускать после сбоя сети
# посреди установки.

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
    # Старая консоль без поддержки UTF-8 — текст будет с артефактами, но
    # установка от этого не зависит.
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$diarizationVenv = Join-Path $projectRoot ".venv_diarization_cpu"
$diarizationGpuVenv = Join-Path $projectRoot ".venv_diarization_gpu"
$diarizationPython = Join-Path $diarizationVenv "Scripts\python.exe"
$bundleCache = Join-Path $projectRoot "bundle\hf-cache"
$hubCache = Join-Path $env:USERPROFILE ".cache\huggingface\hub"
$serviceWheels = Join-Path $projectRoot "bundle\wheels"
$diarizationWheels = Join-Path $projectRoot "bundle\wheels-diarization"

$script:failures = @()
$script:warnings = @()

. (Join-Path $PSScriptRoot "install_helpers.ps1")
$hasNvidia = $false
$nvidia = Get-Command "nvidia-smi" -ErrorAction SilentlyContinue
if ($nvidia) {
    try {
        $gpuNames = & $nvidia.Source --query-gpu=name --format=csv,noheader 2>$null
        $hasNvidia = ($LASTEXITCODE -eq 0) -and [bool]$gpuNames
    }
    catch { $hasNvidia = $false }
}
$installDevice = Get-InstallDevice $Device $hasNvidia
if ($CudaVariant -eq "auto") {
    $CudaVariant = "cu126"
    if ($hasNvidia) {
        try {
            $capabilities = & $nvidia.Source --query-gpu=compute_cap --format=csv,noheader 2>$null
            if ($LASTEXITCODE -eq 0) {
                foreach ($capability in $capabilities) {
                    if ([double]::Parse($capability, [Globalization.CultureInfo]::InvariantCulture) -ge 10) {
                        $CudaVariant = "cu128"
                    }
                }
            }
        }
        catch { }
    }
}
$selectedDiarizationVenv = $diarizationVenv
if ($installDevice -eq "cuda") { $selectedDiarizationVenv = $diarizationGpuVenv }
$selectedDiarizationPython = Join-Path $selectedDiarizationVenv "Scripts\python.exe"
if ($Plan) {
    [PSCustomObject]@{
        requested_device = $Device
        selected_device = $installDevice
        torch_variant = $(if ($installDevice -eq "cuda") { $CudaVariant } else { "cpu" })
        diarization_environment = $selectedDiarizationVenv
        install_cuda_libraries = ($installDevice -eq "cuda")
        skip_diarization = [bool]$SkipDiarization
    } | ConvertTo-Json
    exit 0
}

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "== $text" -ForegroundColor Cyan
}

function Write-Ok([string]$text) {
    Write-Host "   [+] $text" -ForegroundColor Green
}

function Write-Note([string]$text) {
    Write-Host "   [?] $text" -ForegroundColor Yellow
    $script:warnings += $text
}

function Write-Fail([string]$text) {
    Write-Host "   [-] $text" -ForegroundColor Red
    $script:failures += $text
}

function Update-PathFromRegistry {
    # После winget новая программа есть на диске, но не в PATH текущего
    # процесса: переменная читается один раз при старте. Без этого шага
    # питон, поставленный три строки назад, «не найден».
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:PATH = @($machine, $user) -join ";"
}

function Test-Program([string]$name) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($name -eq "node" -and $command) {
        try {
            $version = & $command.Source -p "process.versions.node" 2>$null
            return ($LASTEXITCODE -eq 0) -and ([version]$version -ge [version]"22.13.0")
        }
        catch { return $false }
    }
    return [bool]$command
}

function Invoke-External {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $projectRoot
    )
    # Start-Process с -Wait, а не оператор вызова: нужен код возврата и
    # гарантия, что вывод дочернего процесса дошёл до консоли целиком.
    $process = Start-Process -FilePath $FilePath -ArgumentList @($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) `
        -WorkingDirectory $WorkingDirectory -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}

function Install-Package([string]$id, [string]$title, [string]$probe) {
    if ($probe -and (Test-Program $probe)) {
        Write-Ok "$title уже установлен"
        return $true
    }
    if (-not (Test-Program "winget")) {
        Write-Fail "$title отсутствует, и winget недоступен — установите вручную"
        return $false
    }
    Write-Host "   ... устанавливается $title" -ForegroundColor DarkGray
    $code = Invoke-External -FilePath "winget" -Arguments @(
        "install", "--id", $id, "--exact", "--silent",
        "--accept-source-agreements", "--accept-package-agreements"
    )
    Update-PathFromRegistry
    if ($probe -and (Test-Program $probe)) {
        Write-Ok "$title установлен"
        return $true
    }
    # winget возвращает ненулевой код и когда пакет уже стоял свежей версии.
    # Верим не коду, а тому, нашлась ли программа после обновления PATH.
    Write-Fail "$title установить не удалось (код winget $code)"
    return $false
}

function Resolve-BasePython {
    # Нужен именно 3.11: на нём собрана и проверена рабочая машина, и на нём
    # заведомо есть колёса для faster-whisper, torch и pyannote.
    #
    # Проверять наличие py.exe недостаточно. На рабочем ноутбуке вполне может
    # стоять Python 3.13 со своим лаунчером: `py` найдётся, установка 3.11
    # пропустится, а `py -3.11` потом не сработает. Поэтому проверка спрашивает
    # у лаунчера именно 3.11 и верит только успешному ответу.
    if (Test-Program "py") {
        $version = $null
        try {
            $version = & py -3.11 -c "import sys; print(sys.version.split()[0])" 2>$null
        }
        catch {
            # Лаунчер без нужной версии пишет в stderr, и при
            # ErrorActionPreference=Stop это превращается в исключение.
            # Отсутствие 3.11 — обычный ответ, а не сбой установки.
            $version = $null
        }
        if ($version) {
            return @{ Path = "py"; Arguments = @("-3.11") }
        }
    }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Python311\python.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return @{ Path = $candidate; Arguments = @() }
        }
    }
    return $null
}

function Install-Python311 {
    $found = Resolve-BasePython
    if ($found) {
        Write-Ok "Python 3.11 уже установлен"
        return $found
    }
    if (-not (Test-Program "winget")) {
        Write-Fail "Python 3.11 отсутствует, и winget недоступен — установите вручную"
        return $null
    }
    Write-Host "   ... устанавливается Python 3.11" -ForegroundColor DarkGray
    $null = Invoke-External -FilePath "winget" -Arguments @(
        "install", "--id", "Python.Python.3.11", "--exact", "--silent",
        "--accept-source-agreements", "--accept-package-agreements"
    )
    Update-PathFromRegistry
    $found = Resolve-BasePython
    if ($found) {
        Write-Ok "Python 3.11 установлен"
    }
    else {
        Write-Fail "Python 3.11 установить не удалось"
    }
    return $found
}

function New-PythonVenv([hashtable]$basePython, [string]$target) {
    $arguments = $basePython.Arguments + @("-m", "venv", $target)
    $code = Invoke-External -FilePath $basePython.Path -Arguments $arguments
    return ($code -eq 0) -and (Test-Path -LiteralPath (Join-Path $target "Scripts\python.exe"))
}

function Invoke-Pip([string]$python, [string[]]$arguments) {
    $full = @("-m", "pip", "--timeout", "120", "--retries", "5") + $arguments
    return (Invoke-External -FilePath $python -Arguments $full)
}

function Install-Requirements {
    <#
        Ставит пакеты из колёс, привезённых в архиве, а если их нет — из сети.

        Колёса в архиве покрывают ровно ту связку, на которой собирался архив:
        Python 3.11 под Windows x64. Установщик тянет именно 3.11, так что
        связка совпадает. Но полагаться на это вслепую нельзя — если чего-то
        в наборе не хватит, установка молча провалилась бы вместо того, чтобы
        доскачать недостающее. Отсюда возврат в сеть при неудаче.
    #>
    param(
        [string]$Python,
        [string]$Wheels,
        [string[]]$Targets,
        [string]$Title
    )
    $offline = (Test-Path -LiteralPath $Wheels) -and
        @(Get-ChildItem -LiteralPath $Wheels -Filter "*.whl" -ErrorAction SilentlyContinue).Count -gt 0

    if ($offline) {
        Write-Host "   ... $Title из архива, без скачивания" -ForegroundColor DarkGray
        $code = Invoke-Pip $Python (
            @("install", "--no-index", "--find-links", $Wheels) + $Targets
        )
        if ($code -eq 0) {
            return 0
        }
        Write-Host "   ... в архиве не всё, добираются недостающие" -ForegroundColor DarkGray
    }
    else {
        Write-Host "   ... $Title из сети" -ForegroundColor DarkGray
    }
    return (Invoke-Pip $Python (@("install") + $Targets))
}


Write-Host "Установка локального Whisper" -ForegroundColor White
Write-Host "Каталог: $projectRoot"
Write-Host "Устройство установки: $installDevice ($Device)"

# --- программы --------------------------------------------------------------

Write-Step "Программы"

Update-PathFromRegistry
if (-not (Test-Program "winget")) {
    Write-Note "winget не найден: недостающие программы придётся ставить вручную"
    Write-Note "winget входит в «Установщик приложений» из Microsoft Store"
}

$basePython = Install-Python311
$null = Install-Package "Gyan.FFmpeg" "FFmpeg" "ffmpeg"
$null = Install-Package "Microsoft.PowerShell" "PowerShell 7" "pwsh"
$nodeReady = Install-Package "OpenJS.NodeJS.LTS" "Node.js" "node"

if (-not $basePython) {
    Write-Host ""
    Write-Host "Без Python 3.11 продолжать нечем. Установка прервана." -ForegroundColor Red
    exit 1
}

# --- среда сервиса ----------------------------------------------------------

Write-Step "Среда Python для сервиса"

if (Test-Path -LiteralPath $venvPython) {
    Write-Ok "Среда .venv уже создана"
}
else {
    if (-not (New-PythonVenv $basePython $venv)) {
        Write-Fail "Не удалось создать .venv"
        exit 1
    }
    Write-Ok "Среда .venv создана"
}

if (-not (Test-Path -LiteralPath $serviceWheels)) {
    $null = Invoke-Pip $venvPython @("install", "--upgrade", "pip", "--quiet")
}
$code = Install-Requirements -Python $venvPython -Wheels $serviceWheels `
    -Title "пакеты сервиса" `
    -Targets @("--requirement", (Join-Path $projectRoot "requirements.txt"))
if ($code -eq 0) {
    Write-Ok "Пакеты сервиса установлены"
}
else {
    Write-Fail "Не удалось установить пакеты из requirements.txt"
}

# --- библиотеки CUDA для Whisper --------------------------------------------

if ($installDevice -eq "cuda") {
    Write-Step "Ускорение Whisper на NVIDIA"
    $code = Invoke-External $venvPython @("-m", "scripts.install_cuda", "--variant", $CudaVariant)
    if ($code -eq 0) { Write-Ok "Библиотеки CUDA/cuDNN на месте" }
    else { Write-Fail "Не удалось восстановить библиотеки CUDA/cuDNN" }
}

# --- среда разметки спикеров ------------------------------------------------

Write-Step "Среда для разметки спикеров: $installDevice"
if ($SkipDiarization) {
    Write-Note "Пропущено по ключу -SkipDiarization"
}
else {
    $created = $true
    if (-not (Test-Path -LiteralPath $selectedDiarizationPython)) {
        $created = New-PythonVenv $basePython $selectedDiarizationVenv
    }
    if (-not $created) {
        Write-Fail "Не удалось создать среду разметки спикеров"
    }
    else {
        $null = Invoke-Pip $selectedDiarizationPython @("install", "--upgrade", "pip", "--quiet")
        $torchVariant = "cpu"
        $wheels = $diarizationWheels
        if ($installDevice -eq "cuda") {
            $torchVariant = $CudaVariant
            # bundle/wheels-diarization contains CPU wheels; never use them for GPU.
            $wheels = Join-Path $projectRoot "bundle\wheels-diarization-gpu"
        }
        $torchCode = Install-Requirements -Python $selectedDiarizationPython -Wheels $wheels `
            -Title "PyTorch ($torchVariant)" -Targets @(
                "--index-url", "https://download.pytorch.org/whl/$torchVariant",
                "torch==2.11.0+$torchVariant", "torchaudio==2.11.0+$torchVariant"
            )
        if ($torchCode -ne 0) {
            Write-Fail "Не удалось установить PyTorch ($torchVariant)"
        }
        else {
            $code = Install-Requirements -Python $selectedDiarizationPython -Wheels $wheels `
                -Title "Pyannote и Nemotron" -Targets @(
                    "torch==2.11.0+$torchVariant", "torchaudio==2.11.0+$torchVariant",
                    "--extra-index-url", "https://download.pytorch.org/whl/$torchVariant",
                    "--requirement", (Join-Path $projectRoot "requirements-diarization.txt")
                )
            if ($code -eq 0) { Write-Ok "Pyannote и Nemotron установлены" }
            else { Write-Fail "Не удалось установить Pyannote и Nemotron" }
        }
    }
}

# --- модель разметки спикеров -----------------------------------------------

Write-Step "Модель разметки спикеров"

$modelCache = Join-Path $hubCache "models--pyannote--speaker-diarization-community-1"
$bundledModel = Join-Path $bundleCache "models--pyannote--speaker-diarization-community-1"

if (Test-Path -LiteralPath (Join-Path $modelCache "snapshots")) {
    Write-Ok "Модель уже в кеше Hugging Face"
}
elseif (Test-Path -LiteralPath $bundledModel) {
    # Модель приехала в архиве. Это избавляет от token и от повторного
    # принятия условий Community-1 на чистой машине.
    New-Item -ItemType Directory -Force -Path $hubCache | Out-Null
    Copy-Item -LiteralPath $bundledModel -Destination $hubCache -Recurse -Force
    Write-Ok "Модель взята из архива"
}
else {
    Write-Note "Модели нет. Откройте Настройки -> Спикеры и введите token Hugging Face"
}

# Nemotron открытый, токен ему не нужен, поэтому модель просто скачивается.
$nemotronCache = Join-Path $hubCache "models--nvidia--Nemotron-3-Diarization"
$bundledNemotron = Join-Path $bundleCache "models--nvidia--Nemotron-3-Diarization"
$nemotronPython = $selectedDiarizationPython

if (Test-Path -LiteralPath $bundledNemotron) {
    if (-not (Test-Path -LiteralPath $nemotronCache)) {
        New-Item -ItemType Directory -Force -Path $hubCache | Out-Null
        Copy-Item -LiteralPath $bundledNemotron -Destination $hubCache -Recurse -Force
    }
}
if ($SkipDiarization) {
    Write-Note "Загрузка Nemotron пропущена по -SkipDiarization"
}
elseif (-not (Test-Path -LiteralPath $nemotronPython)) {
    Write-Fail "Nemotron не установлен: нет среды разметки спикеров"
}
else {
    Write-Host "   ... проверяется и докачивается закреплённая модель Nemotron, около 400 МБ" -ForegroundColor DarkGray
    $nemotronWorker = Join-Path $projectRoot "backend\nemotron_worker.py"
    $code = Invoke-External -FilePath $nemotronPython -Arguments @($nemotronWorker, "--download")
    if ($code -eq 0) { Write-Ok "Модель Nemotron 3 на месте" }
    else { Write-Fail "Nemotron не скачался. Повторите установку или скачайте модель в Настройки -> Спикеры" }
}

# --- модели распознавания ---------------------------------------------------

Write-Step "Модели распознавания"

$russianModel = Join-Path $projectRoot "models\turbo-russian\model.bin"
$englishModel = Join-Path $projectRoot "models\turbo\model.bin"
if ((Test-Path -LiteralPath $russianModel) -and (Test-Path -LiteralPath $englishModel)) {
    Write-Ok "Обе модели на месте"
}
else {
    Write-Host "   ... скачиваются модели, это несколько гигабайт" -ForegroundColor DarkGray
    $code = Invoke-External -FilePath $venvPython -Arguments @("-m", "scripts.download_models")
    if ($code -eq 0) {
        Write-Ok "Модели скачаны"
    }
    else {
        Write-Fail "Не удалось скачать модели распознавания"
    }
}

# --- веб-интерфейс ----------------------------------------------------------

Write-Step "Веб-интерфейс"

if ($SkipFrontend) {
    Write-Note "Пропущено по ключу -SkipFrontend"
}
elseif (-not $nodeReady -and -not (Test-Program "node")) {
    Write-Fail "Без Node.js интерфейс не запустится (сам сервер распознавания будет работать)"
}
else {
    $npm = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
    if (-not $npm) {
        $npm = Get-Command "npm" -ErrorAction SilentlyContinue
    }
    if (-not $npm) {
        Write-Fail "npm не найден рядом с Node.js"
    }
    else {
        if (Test-Path -LiteralPath (Join-Path $projectRoot "node_modules\vinext")) {
            Write-Ok "Пакеты интерфейса уже установлены"
        }
        else {
            Write-Host "   ... npm ci, несколько минут" -ForegroundColor DarkGray
            $code = Invoke-External -FilePath $npm.Source -Arguments @("ci", "--no-audit", "--no-fund")
            if ($code -eq 0) {
                Write-Ok "Пакеты интерфейса установлены"
            }
            else {
                Write-Fail "npm ci завершился с ошибкой"
            }
        }

        if (Test-Path -LiteralPath (Join-Path $projectRoot "dist\client")) {
            Write-Ok "Интерфейс уже собран"
        }
        else {
            Write-Host "   ... сборка интерфейса" -ForegroundColor DarkGray
            $code = Invoke-External -FilePath $npm.Source -Arguments @("run", "build")
            if ($code -eq 0) {
                Write-Ok "Интерфейс собран"
            }
            else {
                Write-Fail "Сборка интерфейса не удалась"
            }
        }
    }
}

# --- устройство и проверка вычислений ---------------------------------------

if (Test-Path -LiteralPath $venvPython) {
    $code = Invoke-External $venvPython @("-m", "scripts.configure_runtime", "--device", $Device)
    if ($code -ne 0) { Write-Fail "Не удалось сохранить выбор устройства" }
    $checkArguments = @("-m", "scripts.check_runtime", "--device", $installDevice)
    if (-not $SkipDiarization) {
        $checkArguments += @("--diarization-python", $selectedDiarizationPython)
    }
    $code = Invoke-External $venvPython $checkArguments
    if ($code -ne 0) { Write-Fail "Вычисления на $installDevice не прошли проверку" }
}

# --- проверка ---------------------------------------------------------------

Write-Step "Проверка"

if (Test-Path -LiteralPath $venvPython) {
    $doctorCode = Invoke-External -FilePath $venvPython -Arguments @("-m", "scripts.doctor")
    if ($doctorCode -ne 0) {
        Write-Fail "Проверка установки не прошла — см. сообщения выше"
    }
}

Write-Host ""
if ($script:failures.Count -gt 0) {
    Write-Host "Установка завершена с ошибками:" -ForegroundColor Red
    foreach ($failure in $script:failures) {
        Write-Host "  - $failure" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Повторный запуск установщика безопасен: он доделает то, что не вышло." -ForegroundColor Yellow
    exit 1
}

Write-Host "Установка завершена." -ForegroundColor Green
if ($script:warnings.Count -gt 0) {
    foreach ($warning in $script:warnings) {
        Write-Host "  ? $warning" -ForegroundColor Yellow
    }
}
Write-Host "Запускайте «Запустить Whisper.bat»." -ForegroundColor Green
exit 0
