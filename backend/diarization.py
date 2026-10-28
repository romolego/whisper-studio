from __future__ import annotations

import bisect
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from . import cancellation, runtime, storage, remote_client


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "diarization"
TOKEN_PATH = DATA_DIR / "huggingface.token"
CACHE_DIR = DATA_DIR / "models"
MODEL_ID = "pyannote/speaker-diarization-community-1"

# Движков разметки два, и выбирается он на каждый файл. Pyannote — прежний и
# проверенный; Nemotron 3 Diarization от NVIDIA (сентябрь 2026) различает до
# восьми голосов, размечает перебивания и занимает на видеокарте около 1.4 ГБ
# против 5.3 ГБ у Pyannote. Какой лучше на конкретных записях — решает
# сравнение, поэтому ни один не вытесняет другой.
ENGINES = ("pyannote", "nemotron")
ENGINE_TITLES = {"pyannote": "Pyannote", "nemotron": "Nemotron 3"}

NEMOTRON_MODEL_ID = "nvidia/Nemotron-3-Diarization"
NEMOTRON_FILES = ("config.json", "processor_config.json", "model.safetensors")
NEMOTRON_WORKER_PATH = ROOT / "backend" / "nemotron_worker.py"
NEMOTRON_MODEL_CACHE = (
    Path.home()
    / ".cache"
    / "huggingface"
    / "hub"
    / "models--nvidia--Nemotron-3-Diarization"
)
# Для показа прогресса загрузки: model.safetensors весит 397 МБ, остальное —
# пара килобайт.
NEMOTRON_DOWNLOAD_BYTES = 397_300_000

# Pyannote живёт в отдельной среде: у него свой torch, который незачем тащить
# в среду сервиса. Сред может быть две — с CUDA-сборкой torch и с процессорной.
#
# Порядок означает «бери лучшее из установленного». На машине с видеокартой
# стоит GPU-среда, на ноутбуке без видеокарты установщик создаёт только
# процессорную. Путь на H:\ остался от первой установки на стационарном
# компьютере и проверяется последним: на другой машине такого диска нет, и
# раньше это молча лишало ноутбук разметки спикеров.
GPU_VENV = ROOT / ".venv_diarization_gpu"
CPU_VENV = ROOT / ".venv_diarization_cpu"
LEGACY_VENV = Path(r"H:\zvuk\.venv_diarization")


def _venv_runtime(venv: Path) -> tuple[Path, Path] | None:
    """Интерпретатор среды и признак установленного в ней pyannote."""
    python_path = venv / "Scripts" / "python.exe"
    if not python_path.exists():
        return None
    # Версия не зашита в имя каталога: pip на новой машине может поставить не
    # ту же 4.0.7, и жёсткое имя сделало бы рабочую установку «невидимой».
    for marker in sorted(
        (venv / "Lib" / "site-packages").glob("pyannote_audio-*.dist-info")
    ):
        return python_path, marker
    return None


def _external_runtime() -> tuple[Path, Path] | None:
    """Интерпретатор и признак установленного pyannote, GPU-среда в приоритете."""
    environments = (CPU_VENV, GPU_VENV) if runtime.preferred_device() == "cpu" else (GPU_VENV, CPU_VENV)
    for venv in (*environments, LEGACY_VENV):
        found = _venv_runtime(venv)
        if found is not None:
            return found
    return None


def _nemotron_runtime() -> Path | None:
    """Интерпретатор среды, в которой есть transformers с Nemotron 3.

    Nemotron живёт в той же среде, что и Pyannote, — ему нужен тот же torch.
    Признак — каталог модели внутри transformers, а не версия пакета:
    поддержка появилась в исходниках раньше, чем в выпуске на PyPI. librosa
    нужна извлекателю признаков модели, без неё worker падает при загрузке.
    """
    environments = (CPU_VENV, GPU_VENV) if runtime.preferred_device() == "cpu" else (GPU_VENV, CPU_VENV)
    for venv in environments:
        python_path = venv / "Scripts" / "python.exe"
        packages = venv / "Lib" / "site-packages"
        if (
            python_path.exists()
            and (
                packages / "transformers" / "models" / "nemotron3_diarization"
            ).is_dir()
            and (packages / "librosa").is_dir()
        ):
            return python_path
    return None


def _nemotron_model_ready() -> bool:
    snapshots = NEMOTRON_MODEL_CACHE / "snapshots"
    if not snapshots.exists():
        return False
    return any(
        all((item / name).exists() for name in NEMOTRON_FILES)
        for item in snapshots.iterdir()
        if item.is_dir()
    )


def _nemotron_ready() -> bool:
    return (
        _nemotron_runtime() is not None
        and NEMOTRON_WORKER_PATH.exists()
        and _nemotron_model_ready()
    )
SHARED_MODEL_CACHE = (
    Path.home()
    / ".cache"
    / "huggingface"
    / "hub"
    / "models--pyannote--speaker-diarization-community-1"
)
WORKER_PATH = ROOT / "backend" / "diarization_worker.py"


def _local_package_installed() -> bool:
    try:
        return importlib.util.find_spec("pyannote.audio") is not None
    except (ImportError, ModuleNotFoundError):
        return False


def _shared_model_ready() -> bool:
    """Лежит ли модель в общем кеше Hugging Face, где её ищет worker.

    Worker запускается с HF_HUB_OFFLINE=1 и смотрит только в кеш пользователя.
    Поэтому готовность решает именно этот каталог, а не какой-либо другой.
    """
    snapshots = SHARED_MODEL_CACHE / "snapshots"
    if not snapshots.exists():
        return False
    return any(
        (item / "config.yaml").exists()
        for item in snapshots.iterdir()
        if item.is_dir()
    )


def _external_ready() -> bool:
    return (
        _external_runtime() is not None
        and WORKER_PATH.exists()
        and _shared_model_ready()
    )


def _saved_token() -> str:
    if not TOKEN_PATH.exists():
        return ""
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


_device_cache: dict[str, str] = {}
_device_lock = threading.Lock()


def _runtime_device(python_path: Path) -> str:
    """На чём реально будет считать среда: cuda или cpu.

    Результат кешируется на весь срок жизни процесса. Проверка запускает
    отдельный интерпретатор с импортом torch и занимает больше десяти секунд,
    а интерфейс опрашивает /api/diarization/status каждые 1.8 с — без кеша это
    забивало бы пул потоков сервера. Смена устройства требует переустановки
    зависимостей, то есть всё равно перезапуска сервиса.
    """
    key = str(python_path)
    with _device_lock:
        if key in _device_cache:
            return _device_cache[key]
        value = _probe_device(python_path)
        # «unknown» не кешируем: это, скорее всего, разовый сбой запуска.
        if value != "unknown":
            _device_cache[key] = value
        return value


def _probe_device(python_path: Path) -> str:
    try:
        probe = subprocess.run(
            [
                str(python_path),
                "-c",
                "import torch;"
                "ok = torch.cuda.is_available();"
                "cc = torch.cuda.get_device_capability(0) if ok else None;"
                "arch = ('sm_%d%d' % cc) if cc else '';"
                "print('cuda' if ok and arch in torch.cuda.get_arch_list()"
                " else 'cpu')",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        value = (probe.stdout or "").strip().splitlines()
        return value[-1] if value and value[-1] in {"cuda", "cpu"} else "unknown"
    except Exception:
        return "unknown"


def _pyannote_available() -> bool:
    return _external_ready() or (
        _local_package_installed() and bool(_saved_token())
    )


def engine_available(engine: str) -> bool:
    """Готов ли движок считать прямо сейчас. Только проверки файлов — дёшево."""
    if remote_client.selected():
        return bool(remote_client.speaker_status()["engines"].get(engine, {}).get("available"))
    if engine == "nemotron":
        return _nemotron_ready()
    return _pyannote_available()


def default_engine() -> str:
    value = storage.get_settings().get("diarization_engine", "nemotron")
    return value if value in ENGINES else "nemotron"


def resolve_engine(requested: str | None) -> str:
    """Движок из запроса, а если его не назвали — из настроек."""
    return requested if requested in ENGINES else default_engine()


def _active_device(configured: str, runtime_device: str) -> str:
    if configured == "cpu":
        return "cpu"
    if configured == "cuda" and runtime_device == "cuda":
        return "cuda"
    return runtime_device


def _nemotron_status(configured_device: str) -> dict[str, Any]:
    runtime = _nemotron_runtime()
    model_ready = _nemotron_model_ready()
    runtime_device = _runtime_device(runtime) if runtime else "cpu"
    with _nemotron_download_lock:
        download = dict(_nemotron_download)
    if download["status"] == "running":
        download["downloaded_bytes"] = _cache_size(NEMOTRON_MODEL_CACHE)
    return {
        "engine": "nemotron",
        "title": ENGINE_TITLES["nemotron"],
        "model_name": NEMOTRON_MODEL_ID,
        "package_installed": runtime is not None,
        "model_ready": model_ready,
        "available": runtime is not None
        and model_ready
        and NEMOTRON_WORKER_PATH.exists(),
        "runtime": str(runtime.parents[1]) if runtime else "",
        "device": _active_device(configured_device, runtime_device),
        "cuda_available": runtime_device == "cuda",
        "download": download,
        "download_total_bytes": NEMOTRON_DOWNLOAD_BYTES,
    }


def status() -> dict[str, Any]:
    """Состояние разметки спикеров.

    Ключи верхнего уровня описывают Pyannote — так было до второго движка, и
    на них завязаны проверка установки и старые клиенты. Оба движка целиком
    лежат в engines, а any_available отвечает на вопрос «можно ли вообще
    разделять голоса».
    """
    if remote_client.selected():
        return remote_client.speaker_status()
    external = _external_ready()
    local = _local_package_installed()
    token_saved = bool(_saved_token())
    local_model = _shared_model_ready()
    runtime = _external_runtime()
    configured_device = storage.get_settings()["diarization_device"]
    runtime_device = _runtime_device(runtime[0]) if runtime else "cpu"
    pyannote = {
        "engine": "pyannote",
        "title": ENGINE_TITLES["pyannote"],
        "model_name": MODEL_ID,
        "package_installed": external or local,
        "token_saved": token_saved,
        "model_ready": external or local_model,
        "available": external or (local and token_saved),
        "runtime": str(runtime[0].parents[1]) if runtime else "local",
        "device": _active_device(configured_device, runtime_device),
        "cuda_available": runtime_device == "cuda",
    }
    nemotron = _nemotron_status(configured_device)
    return {
        "package_installed": pyannote["package_installed"],
        "token_saved": token_saved,
        "model_ready": pyannote["model_ready"],
        "available": pyannote["available"],
        "model_name": MODEL_ID,
        "runtime": pyannote["runtime"],
        "device": pyannote["device"],
        "configured_device": configured_device,
        "cuda_available": pyannote["cuda_available"] or nemotron["cuda_available"],
        "any_available": pyannote["available"] or nemotron["available"],
        "default_engine": default_engine(),
        "engines": {"pyannote": pyannote, "nemotron": nemotron},
    }


def _cache_size(folder: Path) -> int:
    """Сколько уже лежит в кеше модели, включая недокачанные .incomplete.

    Файлы снимка — символьные ссылки на blobs, поэтому каждая цель
    считается один раз.
    """
    if not folder.exists():
        return 0
    total = 0
    seen: set[Path] = set()
    for item in folder.rglob("*"):
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


_nemotron_download_lock = threading.Lock()
_nemotron_download: dict[str, Any] = {"status": "idle", "message": ""}


def _download_nemotron_worker(python_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop("HF_HUB_OFFLINE", None)
    environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        result = subprocess.run(
            [str(python_path), str(NEMOTRON_WORKER_PATH), "--download"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            env=environment,
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        ok = result.returncode == 0 and _nemotron_model_ready()
        detail = ((result.stderr or "").strip().splitlines() or [""])[-1]
        state = {
            "status": "done" if ok else "error",
            "message": "" if ok else (
                "Модель не скачалась. Проверьте интернет и повторите. "
                f"{detail}"
            )[:400],
        }
    except subprocess.TimeoutExpired:
        state = {
            "status": "error",
            "message": "Загрузка шла больше часа и была прервана. Повторите.",
        }
    except Exception as exc:  # noqa: BLE001
        state = {"status": "error", "message": str(exc)[:400]}
    with _nemotron_download_lock:
        _nemotron_download.clear()
        _nemotron_download.update(state)


def download_nemotron() -> dict[str, Any]:
    """Скачать модель Nemotron в фоне; ход виден в status()."""
    if _nemotron_model_ready():
        return status()
    runtime = _nemotron_runtime()
    if runtime is None:
        raise RuntimeError(
            "В среде разметки нет transformers с поддержкой Nemotron. "
            "Запустите «Установить Whisper.bat»."
        )
    with _nemotron_download_lock:
        if _nemotron_download.get("status") == "running":
            return status()
        _nemotron_download.clear()
        _nemotron_download.update({"status": "running", "message": ""})
    threading.Thread(
        target=_download_nemotron_worker,
        args=(runtime,),
        name="nemotron-download",
        daemon=True,
    ).start()
    return status()


# Скачивает модель тот же интерпретатор, который потом будет считать, и всегда
# в общий кеш Hugging Face — туда, куда смотрит worker. Раньше загрузка шла в
# data/diarization/models, а worker искал в кеше пользователя: на этой машине
# расхождение не всплывало только потому, что модель попала в кеш ещё до
# сервиса. На чистой машине активация из интерфейса не сработала бы.
_DOWNLOAD_SCRIPT = """
import os
from pyannote.audio import Pipeline

pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-community-1",
    token=os.environ["PYANNOTE_TOKEN"],
)
if pipeline is None:
    raise SystemExit("pipeline-is-none")
print("ok")
"""


def _download_model(python_path: Path | None, token: str) -> None:
    """Положить модель в общий кеш. python_path=None — считать своим процессом."""
    if python_path is None:
        from pyannote.audio import Pipeline

        pipeline = Pipeline.from_pretrained(MODEL_ID, token=token)
        if pipeline is None:
            raise RuntimeError(
                "Модель не загрузилась. Проверьте условия Community-1 и token."
            )
        return

    environment = dict(os.environ)
    # Токен уходит переменной окружения, а не аргументом: аргументы процесса
    # видны в диспетчере задач любому пользователю машины.
    environment["PYANNOTE_TOKEN"] = token
    environment.pop("HF_HUB_OFFLINE", None)
    environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
    result = subprocess.run(
        [str(python_path), "-c", _DOWNLOAD_SCRIPT],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0 or "ok" not in (result.stdout or ""):
        detail = ((result.stderr or "").strip().splitlines() or [""])[-1]
        raise RuntimeError(
            "Модель не загрузилась. Проверьте, что на huggingface.co приняты "
            f"условия Community-1 и что token действующий. {detail}"[:400]
        )


def activate(token: str) -> dict[str, Any]:
    if _external_ready():
        return status()
    clean = token.strip()
    if not clean or not re.match(r"^hf_[A-Za-z0-9]{10,}$", clean):
        raise RuntimeError("Укажите действующий Hugging Face token вида hf_…")

    runtime = _external_runtime()
    if runtime is not None:
        _download_model(runtime[0], clean)
    elif _local_package_installed():
        _download_model(None, clean)
    else:
        raise RuntimeError(
            "Компонент Pyannote не найден. Запустите «Установить Whisper.bat»."
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(clean, encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return status()


def _turns_external(
    audio_path: Path,
    task_id: str | None = None,
) -> list[tuple[float, float, str]]:
    runtime = _external_runtime()
    if runtime is None:
        raise RuntimeError("Среда Pyannote не найдена")
    return _run_worker(runtime[0], WORKER_PATH, audio_path, task_id, "Pyannote")


def _turns_nemotron(
    audio_path: Path,
    task_id: str | None = None,
) -> list[tuple[float, float, str]]:
    runtime = _nemotron_runtime()
    if runtime is None or not _nemotron_model_ready():
        raise RuntimeError(
            "Nemotron не установлен. Откройте Настройки → Спикеры."
        )
    return _run_worker(
        runtime, NEMOTRON_WORKER_PATH, audio_path, task_id, "Nemotron"
    )


def _run_worker(
    python_path: Path,
    worker_path: Path,
    audio_path: Path,
    task_id: str | None,
    title: str,
) -> list[tuple[float, float, str]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DATA_DIR / f"{uuid.uuid4().hex}.json"
    environment = dict(os.environ)
    environment["HF_HUB_OFFLINE"] = "1"
    environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
    environment["WHISPER_DIARIZATION_DEVICE"] = storage.get_settings()[
        "diarization_device"
    ]
    try:
        process = cancellation.run(
            task_id,
            [
                str(python_path),
                str(worker_path),
                str(audio_path),
                str(output_path),
            ],
            cwd=str(ROOT),
            env=environment,
        )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()[-1600:]
            raise RuntimeError(
                f"{title} не смог разделить голоса"
                + (f": {detail}" if detail else "")
            )
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        return [
            (float(item["start"]), float(item["end"]), str(item["speaker"]))
            for item in payload
        ]
    finally:
        output_path.unlink(missing_ok=True)


def _turns_local(audio_path: Path) -> list[tuple[float, float, str]]:
    from pyannote.audio import Pipeline

    token = _saved_token()
    if not token:
        raise RuntimeError("Разделение по спикерам не активировано")
    pipeline = Pipeline.from_pretrained(MODEL_ID, token=token)
    if pipeline is None:
        raise RuntimeError("Не удалось загрузить модель Pyannote")
    try:
        import torch

        pipeline.to(torch.device("cpu"))
    except Exception:
        pass
    output = pipeline(str(audio_path))
    annotation = getattr(output, "exclusive_speaker_diarization", None)
    if annotation is None:
        annotation = getattr(output, "speaker_diarization", None)
    if annotation is None:
        annotation = output
    return [
        (float(turn.start), float(turn.end), str(speaker))
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]


def _speaker_at(
    start: float,
    end: float,
    turns: list[tuple[float, float, str]],
) -> str | None:
    best_speaker: str | None = None
    best_overlap = 0.0
    center = (start + end) / 2
    for turn_start, turn_end, speaker in turns:
        overlap = max(0.0, min(end, turn_end) - max(start, turn_start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = speaker
    if best_speaker is not None:
        return best_speaker
    for turn_start, turn_end, speaker in turns:
        if turn_start <= center <= turn_end:
            return speaker
    return None


class _TurnIndex:
    """Реплики, отсортированные по началу, для быстрого поиска соседей.

    _speaker_at перебирает все реплики на каждое слово: на двухчасовой встрече
    это 17 тысяч слов на две тысячи реплик, 23 секунды чистого Python. Индекс
    отдаёт только те реплики, что могут пересечься со словом, — в исходном
    порядке, поэтому при равном перекрытии выигрывает та же реплика, что и
    при полном переборе.
    """

    def __init__(self, turns: list[tuple[float, float, str]]) -> None:
        self.turns = turns
        self.order = sorted(range(len(turns)), key=lambda index: turns[index][0])
        self.starts = [turns[index][0] for index in self.order]
        self.longest = max((end - start for start, end, _ in turns), default=0.0)

    def near(self, start: float, end: float) -> list[tuple[float, float, str]]:
        # Реплика, задевающая [start, end] или содержащая его середину,
        # начинается не раньше start - longest и не позже end.
        low = bisect.bisect_left(self.starts, start - self.longest - 1e-6)
        high = bisect.bisect_right(self.starts, end + 1e-6)
        return [self.turns[index] for index in sorted(self.order[low:high])]


def assign_speakers(
    audio_path: Path,
    segments: list[dict[str, Any]],
    task_id: str | None = None,
    time_offset: float = 0.0,
    engine: str = "pyannote",
) -> list[dict[str, Any]]:
    """Проставить спикеров в фрагментах.

    time_offset — позиция начала audio_path в исходной записи. Метки фрагментов
    всегда живут на шкале исходника, поэтому на обрезанном куске реплики
    движка нужно сдвинуть на ту же величину.
    """
    remote = remote_client.selected()
    if remote:
        turns = [tuple(item) for item in remote_client.compute(
            remote, audio_path, {"kind": "diarization", "engine": engine}, task_id
        )["turns"]]
    elif engine == "nemotron":
        turns = _turns_nemotron(audio_path, task_id)
    elif _external_ready():
        turns = _turns_external(audio_path, task_id)
    else:
        turns = _turns_local(audio_path)
    if time_offset:
        turns = [
            (start + time_offset, end + time_offset, speaker)
            for start, end, speaker in turns
        ]
    if task_id is not None:
        cancellation.check(task_id)
    index = _TurnIndex(turns)
    for segment in segments:
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        segment["speaker"] = _speaker_at(start, end, index.near(start, end))
        for word in segment.get("words") or []:
            word_start = float(word.get("start") or start)
            word_end = float(word.get("end") or word_start)
            word["speaker"] = _speaker_at(
                word_start, word_end, index.near(word_start, word_end)
            )
    return segments
