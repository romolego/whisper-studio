"""Из чего собран сервис: модели, программы, пакеты — и что из этого стоит.

Раньше это знание жило только в голове и в истории установки. Если бы слетела
Windows или сервис понадобился на другой машине, восстанавливать пришлось бы
наугад: какая модель, откуда скачана, куда положена, какие версии.

Модуль отвечает на четыре вопроса по каждому компоненту: нужен ли он, есть ли
он, где лежит и откуда берётся. Отдельно — можно ли поставить его прямо из
сервиса или придётся руками (и тогда какой командой).

Важно: отсутствие компонента здесь не ошибка сервиса, а состояние. Без
английской модели русские файлы распознаются как обычно, без Pyannote просто
нет разметки спикеров. Поэтому у каждого компонента есть `impact` — что именно
не будет работать.
"""
from __future__ import annotations

import importlib.metadata as metadata
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import diarization, storage


ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
VENDOR_CUDA = ROOT / "vendor" / "cuda" / "bin"
VENV = ROOT / ".venv"

# Держится 15 секунд: опрос запускает несколько внешних процессов (ffmpeg,
# node, nvidia-smi) и занимает около секунды. Интерфейс открывает настройки и
# перечитывает раздел, а не опрашивает его в цикле.
_CACHE_TTL = 15.0
_cache: tuple[float, list[dict[str, Any]]] | None = None
_cache_lock = threading.Lock()

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(command: list[str], pattern: str | None = None) -> str | None:
    """Версия внешней программы или None, если её нет."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr or "").strip()
    if not output:
        return None
    if pattern:
        found = re.search(pattern, output)
        return found.group(1) if found else output.splitlines()[0][:80]
    return output.splitlines()[0][:80]


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    # В кеше Hugging Face файлы снимка — символьные ссылки на blobs. Без учёта
    # уже посчитанных целей модель выходила вдвое тяжелее, чем есть.
    total = 0
    seen: set[Path] = set()
    for item in path.rglob("*"):
        try:
            if not item.is_file():
                continue
            real = item.resolve()
            if real in seen:
                continue
            seen.add(real)
            total += real.stat().st_size
        except OSError:
            continue
    return total


def _installed_at(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _component(
    key: str,
    title: str,
    group: str,
    present: bool,
    *,
    required: bool = True,
    detail: str = "",
    path: str = "",
    source: str = "",
    impact: str = "",
    install: str = "manual",
    command: str = "",
    size_bytes: int = 0,
    installed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "group": group,
        "required": required,
        "present": present,
        "detail": detail,
        "path": path,
        "source": source,
        "impact": impact,
        # service — сервис умеет поставить сам, manual — только руками.
        "install": install if not present else "none",
        "command": command,
        "size_bytes": size_bytes,
        "installed_at": installed_at,
    }


# --- модели -----------------------------------------------------------------

WHISPER_MODELS = (
    (
        "model_ru",
        "Whisper для русского",
        MODELS_DIR / "turbo-russian",
        "coriollon/whisper-large-v3-turbo-russian",
        "Русские файлы и диктовка не распознаются",
    ),
    (
        "model_en",
        "Whisper для английского",
        MODELS_DIR / "turbo",
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "Английские файлы не распознаются; русские работают",
    ),
)


def _whisper_component(
    key: str, title: str, folder: Path, repo: str, impact: str
) -> dict[str, Any]:
    binary = folder / "model.bin"
    present = binary.is_file()
    return _component(
        key,
        title,
        "model",
        present,
        # Английская модель необязательна: без неё русские файлы и диктовка
        # работают как обычно. Считать сервис неготовым из-за неё было бы
        # неправдой.
        required=key != "model_en",
        detail=(
            f"large-v3-turbo · {'int8' if 'russian' in repo else 'float16'} на диске"
            if present
            else "не скачана"
        ),
        path=str(folder),
        source=f"https://huggingface.co/{repo}",
        impact=impact,
        install="service",
        size_bytes=_dir_size(folder),
        installed_at=_installed_at(binary) if present else None,
    )


def _pyannote_component() -> dict[str, Any]:
    status = diarization.status()
    present = bool(status.get("available"))
    runtime = str(status.get("runtime") or "")
    return _component(
        "model_pyannote",
        "Pyannote для разметки спикеров",
        "model",
        present,
        required=False,
        detail=(
            f"{status.get('model_name')} · считает на "
            f"{'видеокарте' if status.get('device') == 'cuda' else 'процессоре'}"
            if present
            else "не активирована"
        ),
        path=runtime,
        source="https://huggingface.co/pyannote/speaker-diarization-community-1",
        impact="Разделение по спикерам недоступно, распознавание работает",
        install="service",
        size_bytes=_dir_size(diarization.SHARED_MODEL_CACHE),
        installed_at=_installed_at(diarization.SHARED_MODEL_CACHE)
        if present
        else None,
    )


def _nemotron_component() -> dict[str, Any]:
    status = diarization.status()["engines"]["nemotron"]
    present = bool(status.get("available"))
    if present:
        detail = (
            f"{status.get('model_name')} · считает на "
            f"{'видеокарте' if status.get('device') == 'cuda' else 'процессоре'}"
        )
    elif not status.get("package_installed"):
        detail = "в среде разметки нет transformers с поддержкой модели"
    else:
        detail = "модель не скачана"
    return _component(
        "model_nemotron",
        "Nemotron 3 для разметки спикеров",
        "model",
        present,
        required=False,
        detail=detail,
        path=str(status.get("runtime") or ""),
        source=f"https://huggingface.co/{diarization.NEMOTRON_MODEL_ID}",
        impact="Спикеров размечает только Pyannote",
        install="service" if status.get("package_installed") else "manual",
        command="" if status.get("package_installed") else "Установить Whisper.bat",
        size_bytes=_dir_size(diarization.NEMOTRON_MODEL_CACHE),
        installed_at=_installed_at(diarization.NEMOTRON_MODEL_CACHE)
        if present
        else None,
    )


# --- программы --------------------------------------------------------------


def _runtime_components() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    items.append(
        _component(
            "python",
            "Python",
            "runtime",
            True,
            detail=sys.version.split()[0],
            path=sys.executable,
            source="https://www.python.org/downloads/",
            impact="Без него не запускается сервер распознавания",
            command="winget install Python.Python.3.11",
        )
    )

    for name, title, impact in (
        ("ffmpeg", "FFmpeg", "Не читаются видео и не извлекается звук"),
        ("ffprobe", "ffprobe", "Не определяются длительность и аудиодорожки"),
    ):
        found = shutil.which(name)
        items.append(
            _component(
                name,
                title,
                "runtime",
                bool(found),
                detail=_run([name, "-version"], r"version (\S+)") or "",
                path=found or "",
                source="https://www.gyan.dev/ffmpeg/builds/",
                impact=impact,
                command="winget install Gyan.FFmpeg",
            )
        )

    node = shutil.which("node")
    items.append(
        _component(
            "node",
            "Node.js",
            "runtime",
            bool(node),
            detail=(_run(["node", "--version"]) or "").lstrip("v"),
            path=node or "",
            source="https://nodejs.org/",
            impact="Не открывается веб-интерфейс; сам сервер распознавания работает",
            command="winget install OpenJS.NodeJS.LTS",
        )
    )

    pwsh = shutil.which("pwsh")
    items.append(
        _component(
            "pwsh",
            "PowerShell 7",
            "runtime",
            bool(pwsh),
            detail=_run(["pwsh", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"])
            or "",
            path=pwsh or "",
            source="https://github.com/PowerShell/PowerShell/releases",
            impact="Не работают запуск через .bat и окна выбора файлов",
            command="winget install Microsoft.PowerShell",
        )
    )

    # Одним вызовом, а не двумя: каждый запуск nvidia-smi стоит около 0.3 с.
    gpu_line = _run(
        ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]
    )
    gpu, _, driver = (gpu_line or "").partition(",")
    gpu, driver = gpu.strip(), driver.strip()
    items.append(
        _component(
            "nvidia",
            "Драйвер NVIDIA",
            "runtime",
            bool(driver),
            required=False,
            detail=f"{gpu} · драйвер {driver}" if driver else "видеокарта не найдена",
            source="https://www.nvidia.com/Download/index.aspx",
            impact=(
                "Распознавание уйдёт на процессор и станет примерно в 20 раз "
                "медленнее"
            ),
            command="",
        )
    )

    cuda_present = VENDOR_CUDA.is_dir() and any(VENDOR_CUDA.glob("cublas64_*.dll"))
    items.append(
        _component(
            "cuda_libs",
            "Библиотеки CUDA (cuBLAS, cuDNN)",
            "runtime",
            cuda_present,
            required=False,
            detail="лежат в проекте, отдельная установка CUDA не нужна"
            if cuda_present
            else "нет",
            path=str(VENDOR_CUDA),
            source="https://developer.nvidia.com/cudnn",
            impact="Распознавание на видеокарте недоступно, останется процессор",
            size_bytes=_dir_size(VENDOR_CUDA),
            installed_at=_installed_at(VENDOR_CUDA) if cuda_present else None,
        )
    )

    return items


# --- пакеты Python ----------------------------------------------------------

PYTHON_PACKAGES = (
    ("faster-whisper", "Движок распознавания"),
    ("ctranslate2", "Вычислительное ядро Whisper"),
    ("fastapi", "Веб-API сервиса"),
    ("uvicorn", "Сервер API"),
    ("yt-dlp", "Скачивание по ссылке"),
    ("python-docx", "Экспорт в DOCX"),
    ("huggingface-hub", "Загрузка моделей"),
    ("python-multipart", "Загрузка файлов"),
)


def _package_components() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name, purpose in PYTHON_PACKAGES:
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = ""
        items.append(
            _component(
                f"pkg_{name}",
                name,
                "python",
                bool(version),
                detail=f"{version} · {purpose}" if version else purpose,
                path=str(VENV) if version else "",
                source=f"https://pypi.org/project/{name}/",
                impact=purpose + " не работает",
                command=(
                    r".venv\Scripts\python.exe -m pip install -r requirements.txt"
                ),
            )
        )
    return items


# --- сборка -----------------------------------------------------------------


def inventory(refresh: bool = False) -> dict[str, Any]:
    """Полный список компонентов с состоянием."""
    global _cache
    with _cache_lock:
        if not refresh and _cache and time.monotonic() - _cache[0] < _CACHE_TTL:
            items = _cache[1]
        else:
            # Группы опрашиваются параллельно: программы запускают внешние
            # процессы (ffmpeg, node, pwsh, nvidia-smi), модели считают размер
            # каталогов, Pyannote проверяет свою среду. Последовательно это
            # занимало больше шести секунд на открытии настроек.
            from concurrent.futures import ThreadPoolExecutor

            def models() -> list[dict[str, Any]]:
                return [_whisper_component(*model) for model in WHISPER_MODELS]

            with ThreadPoolExecutor(max_workers=4) as pool:
                tasks = [
                    pool.submit(models),
                    pool.submit(
                        lambda: [_pyannote_component(), _nemotron_component()]
                    ),
                    pool.submit(_runtime_components),
                    pool.submit(_package_components),
                ]
                items = [item for task in tasks for item in task.result()]
            _cache = (time.monotonic(), items)

    missing_required = [
        item["key"] for item in items if item["required"] and not item["present"]
    ]
    missing_optional = [
        item["key"]
        for item in items
        if not item["required"] and not item["present"]
    ]
    return {
        "items": items,
        "ready": not missing_required,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "total_size_bytes": sum(item["size_bytes"] for item in items),
        "project_root": str(ROOT),
        "database_path": str(storage.DB_PATH),
    }


def invalidate() -> None:
    """Сбросить кеш — после установки чего-нибудь."""
    global _cache
    with _cache_lock:
        _cache = None


# --- скачивание моделей прямо из сервиса ------------------------------------

def _watched(key: str) -> tuple[Path, ...]:
    """Каталоги, по которым считается прогресс загрузки.

    Русская модель качается в `_russian_download` и только потом переносится в
    `turbo-russian`, поэтому учитываются оба места — иначе в момент переноса
    прогресс проваливался бы в ноль.

    Функция, а не константа: MODELS_DIR подменяется в тестах, и посчитанные при
    импорте пути смотрели бы в настоящий каталог моделей.
    """
    if key == "model_ru":
        return (MODELS_DIR / "_russian_download", MODELS_DIR / "turbo-russian")
    return (MODELS_DIR / "turbo",)

# Ожидаемые размеры, если Hugging Face не ответил на запрос метаданных. Взяты
# с этой установки: без них прогресс пришлось бы показывать вслепую.
_FALLBACK_TOTALS = {"model_ru": 819_107_000, "model_en": 1_621_665_000}

_download_lock = threading.Lock()
_download_thread: threading.Thread | None = None
_download_state: dict[str, Any] = {"status": "idle", "targets": []}


def _repo_total_bytes(repo: str, patterns: tuple[str, ...]) -> int:
    """Сколько байт предстоит скачать. 0, если Hugging Face недоступен."""
    try:
        from fnmatch import fnmatch

        from huggingface_hub import HfApi

        info = HfApi().model_info(repo, files_metadata=True)
    except Exception:
        return 0
    total = 0
    for item in info.siblings or []:
        name = getattr(item, "rfilename", "")
        if any(fnmatch(name, pattern) for pattern in patterns):
            total += int(getattr(item, "size", 0) or 0)
    return total


def _downloaded_bytes(key: str) -> int:
    return sum(_dir_size(path) for path in _watched(key))


def missing_models() -> list[str]:
    return [
        key
        for key, _title, folder, _repo, _impact in WHISPER_MODELS
        if not (folder / "model.bin").is_file()
    ]


def _monitor(keys: list[str]) -> None:
    """Обновляет байты, скорость и остаток, пока идёт загрузка."""
    previous_total = 0
    previous_at = time.monotonic()
    while True:
        with _download_lock:
            if _download_state.get("status") != "running":
                return
            targets = _download_state["targets"]
        current = 0
        for target in targets:
            done = _downloaded_bytes(target["key"])
            target["downloaded"] = done
            current += done
        now = time.monotonic()
        elapsed = now - previous_at
        if elapsed >= 1.0:
            # Скорость по последнему интервалу, сглаженная пополам с прошлой:
            # мгновенное значение слишком дёргается, чтобы его читать.
            instant = max(0.0, (current - previous_total) / elapsed)
            with _download_lock:
                before = float(_download_state.get("speed_bytes") or 0)
                speed = instant if before == 0 else (before + instant) / 2
                _download_state["speed_bytes"] = speed
                total = int(_download_state.get("total_bytes") or 0)
                _download_state["downloaded_bytes"] = current
                _download_state["eta_seconds"] = (
                    int((total - current) / speed)
                    if speed > 0 and total > current
                    else None
                )
            previous_total = current
            previous_at = now
        time.sleep(0.5)


def _download_worker(keys: list[str]) -> None:
    from .transcriber import prepare_model_files

    try:
        prepare_model_files(_set_download_message)
        with _download_lock:
            _download_state["status"] = "done"
            _download_state["message"] = "Модели загружены"
            _download_state["downloaded_bytes"] = _download_state.get(
                "total_bytes", 0
            )
            _download_state["eta_seconds"] = 0
    except Exception as exc:  # noqa: BLE001
        with _download_lock:
            _download_state["status"] = "error"
            _download_state["error"] = str(exc)[:400]
            _download_state["message"] = "Загрузка не удалась"
    finally:
        with _download_lock:
            _download_state["finished_at"] = storage.utc_now()
        invalidate()


def _set_download_message(message: str) -> None:
    with _download_lock:
        _download_state["message"] = message


def start_model_download() -> dict[str, Any]:
    """Запустить загрузку отсутствующих моделей Whisper."""
    global _download_thread
    with _download_lock:
        if _download_state.get("status") == "running":
            return dict(_download_state)
        keys = missing_models()
        if not keys:
            _download_state.clear()
            _download_state.update(
                {
                    "status": "done",
                    "message": "Обе модели уже на месте",
                    "targets": [],
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                }
            )
            return dict(_download_state)

        targets = []
        total = 0
        for key, title, folder, repo, _impact in WHISPER_MODELS:
            if key not in keys:
                continue
            patterns = (
                ("ct2_int8_float16/*",)
                if key == "model_ru"
                else (
                    "config.json",
                    "preprocessor_config.json",
                    "model.bin",
                    "tokenizer.json",
                    "vocabulary.*",
                )
            )
            expected = _repo_total_bytes(repo, patterns) or _FALLBACK_TOTALS[key]
            total += expected
            targets.append(
                {
                    "key": key,
                    "title": title,
                    "total": expected,
                    "downloaded": _downloaded_bytes(key),
                }
            )

        _download_state.clear()
        _download_state.update(
            {
                "status": "running",
                "message": "Начинаю загрузку…",
                "targets": targets,
                "downloaded_bytes": sum(item["downloaded"] for item in targets),
                "total_bytes": total,
                "speed_bytes": 0.0,
                "eta_seconds": None,
                "error": "",
                "started_at": storage.utc_now(),
                "finished_at": None,
            }
        )
        state = dict(_download_state)

    _download_thread = threading.Thread(
        target=_download_worker, args=(keys,), name="model-download", daemon=True
    )
    _download_thread.start()
    threading.Thread(
        target=_monitor, args=(keys,), name="model-download-monitor", daemon=True
    ).start()
    return state


def download_status() -> dict[str, Any]:
    with _download_lock:
        state = dict(_download_state)
        state["targets"] = [dict(item) for item in state.get("targets", [])]
    total = int(state.get("total_bytes") or 0)
    done = int(state.get("downloaded_bytes") or 0)
    state["percent"] = round(min(100.0, done / total * 100), 1) if total else None
    return state
