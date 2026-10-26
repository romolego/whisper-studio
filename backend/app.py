from __future__ import annotations

import json
import itertools
import logging
import mimetypes
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import cancellation
from . import diarization as diarization_service
from . import (
    components,
    dictation,
    exporter,
    integrations,
    idle_models,
    media,
    speaker_samples,
    storage,
    video_downloader,
)
from .transcriber import (
    _write_outputs as _write_dictation_outputs,
    diarize_existing_job,
    transcribe_job,
    release_models,
    loaded_model_keys,
)


# Чем размечать спикеров. None в запросе — «как в настройках».
DiarizationEngine = Literal["pyannote", "nemotron"]


app = FastAPI(title="Локальный Whisper API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    # Интерфейс живёт на 3000, но start.ps1 при занятом порте берёт следующий
    # свободный из диапазона 3000-3030. Со списком из одного порта это ломало
    # сервис наглухо: страница открывалась, а браузер молча резал все запросы к
    # API. Регексп покрывает весь диапазон и только loopback — посторонний сайт
    # по-прежнему получает отказ, потому что Starlette сверяет origin целиком.
    allow_origin_regex=(
        r"http://(127\.0\.0\.1|localhost|\[::1\]):"
        r"30(0[0-9]|1[0-9]|2[0-9]|30)"
    ),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # Постраничные списки возвращают общее количество заголовком. Без явного
    # разрешения браузер его прячет, и пагинатор не знает, сколько страниц.
    expose_headers=["X-Total-Count"],
)

logger = logging.getLogger("whisper.worker")

# Две независимые очереди с отдельными рабочими потоками.
#
# Раньше очередь была одна: у диктовки был приоритет 0 против 1 у файлов, но он
# помогал только в ожидании — уже запущенный файл досчитывался до конца, и на
# шестичасовой записи диктовка ждала до 54 минут. Теперь диктовка не стоит за
# файлами вообще, а доступ к видеокарте между классами работы арбитражит
# backend/gpu_turns.py: транскрибация уступает на ближайшем сегменте.
batch_queue: queue.PriorityQueue[tuple[int, int, str, str]] = (
    queue.PriorityQueue()
)
dictation_queue: queue.PriorityQueue[tuple[int, int, str, str]] = (
    queue.PriorityQueue()
)
task_sequence = itertools.count()
worker_threads: dict[str, threading.Thread] = {}
EXPORT_KINDS = {"md", "txt", "docx", "srt", "vtt", "json"}
ROOT = Path(__file__).resolve().parents[1]


class ProjectCreate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    folder_id: str | None = None


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    folder_id: str | None = None


class ProjectsBulkMove(BaseModel):
    # Ограничение сверху — защита от случайного гигантского запроса, а не
    # рабочий лимит: столько проектов вручную не выделяют.
    ids: list[str] = Field(min_length=1, max_length=500)
    folder_id: str | None = None


class ProjectsBulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class FolderPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # Показывать ли проекты папки в общей ленте. Сама папка остаётся в
    # навигации: скрытая папка — это архив, а не удалённый раздел.
    feed_visible: bool | None = None


class SettingsPatch(BaseModel):
    default_language: Literal["auto", "ru", "en"] | None = None
    default_quality: Literal["fast", "accurate"] | None = None
    default_timestamps: Literal["none", "segments", "words"] | None = None
    default_formatting: Literal["formatted", "plain"] | None = None
    default_export: Literal["docx", "txt", "srt", "vtt", "json"] | None = None
    default_view_timestamps: Literal["none", "segments", "words"] | None = None
    default_view_formatting: Literal["formatted", "plain"] | None = None
    default_view_speakers: Literal["off", "on"] | None = None
    default_diarization: Literal["off", "on"] | None = None
    dictation_default_language: Literal["auto", "ru", "en"] | None = None
    dictation_default_quality: Literal["fast", "accurate"] | None = None
    project_name_format: Literal[
        "date_title",
        "date_compact",
        "date_dotted",
        "date_time_title",
        "title_date",
        "title_only",
    ] | None = None
    project_view: Literal["flat", "folders"] | None = None
    default_import_mode: Literal["copy", "reference"] | None = None
    projects_page_size: Literal[
        "10", "20", "50", "100", "500", "1000", "all"
    ] | None = None
    dictation_page_size: Literal[
        "10", "20", "50", "100", "500", "1000", "all"
    ] | None = None
    keep_extracted_audio: Literal["off", "on"] | None = None
    download_quality: Literal[
        "audio", "360", "480", "720", "1080", "max"
    ] | None = None
    download_transcribe: Literal["on", "off"] | None = None
    integration_feed: Literal["show", "hide"] | None = None
    telegram_autostart: Literal["on", "off"] | None = None
    youtube_cookies: Literal[
        "off",
        "auto",
        "firefox",
        "chrome",
        "edge",
        "opera",
        "brave",
        "vivaldi",
        "chromium",
    ] | None = None
    diarization_device: Literal["auto", "cuda", "cpu"] | None = None
    diarization_engine: DiarizationEngine | None = None
    theme: Literal["light", "dark", "auto"] | None = None
    managed_storage_root: str | None = Field(default=None, max_length=1000)
    temporary_storage_root: str | None = Field(default=None, max_length=1000)


class LocalFilesPick(BaseModel):
    session_id: str


class FolderPick(BaseModel):
    key: Literal["managed_storage_root", "temporary_storage_root"]


class OpenFolder(BaseModel):
    path: str = Field(max_length=2000)


class UrlProbe(BaseModel):
    url: str = Field(min_length=1, max_length=4000)
    # Код доступа к записи. Нужен Zoom: запись закрыта паролем, который вводится
    # на её странице, а не передаётся в ссылке.
    passcode: str | None = Field(default=None, max_length=200)


class UrlDownloadStart(BaseModel):
    url: str = Field(min_length=1, max_length=4000)
    quality: str = Field(
        default="auto",
        pattern=r"^(auto|audio|[1-9]\d{1,3})$",
        max_length=5,
    )
    project_id: str | None = Field(default=None, max_length=64)
    project_name: str | None = Field(default=None, max_length=120)
    passcode: str | None = Field(default=None, max_length=200)
    folder_id: str | None = None
    transcription_language: Literal["auto", "ru", "en"] | None = None
    transcription_quality: Literal["fast", "accurate"] | None = None
    transcription_model: Literal["studio", "large-v3"] = "studio"
    transcription_timestamps: Literal["none", "segments", "words"] | None = None
    transcription_formatting: Literal["formatted", "plain"] | None = None
    transcription_diarization: bool | None = None
    transcription_diarization_engine: DiarizationEngine | None = None
    # Ставить ли расшифровку сразу после загрузки. Выключается, когда нужно
    # только забрать запись: очередь распознавания занята надолго, а файлы
    # хочется получить сейчас и разобрать потом. Пусто — как в настройках.
    transcribe: bool | None = None


class IntegrationProject(BaseModel):
    external_id: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=120)


class JobRerun(BaseModel):
    language: Literal["auto", "ru", "en"]
    quality: Literal["fast", "accurate"]
    model: Literal["studio", "large-v3"] | None = None
    diarization: bool = False
    diarization_engine: DiarizationEngine | None = None


class JobDiarize(BaseModel):
    engine: DiarizationEngine | None = None


class MediaQueueItem(BaseModel):
    media_id: str
    audio_stream_index: int
    # Границы обрезки в секундах от начала исходного файла. None в конце —
    # «до самого конца дорожки».
    trim_start_seconds: float = Field(default=0, ge=0)
    trim_end_seconds: float | None = Field(default=None, ge=0)


class MediaEnqueue(BaseModel):
    items: list[MediaQueueItem] = Field(min_length=1)
    language: Literal["auto", "ru", "en"] = "ru"
    quality: Literal["fast", "accurate"] = "accurate"
    timestamps: Literal["none", "segments", "words"] = "segments"
    formatting: Literal["formatted", "plain"] = "formatted"
    diarization: bool = False
    diarization_engine: DiarizationEngine | None = None


class WebinarAnalyzerReferenceJob(BaseModel):
    """Локальная постановка файла из Webinar Analyzer без копирования."""

    source_path: str = Field(min_length=1, max_length=4000)
    project_name: str = Field(default="Webinar Analyzer", max_length=150)
    external_project_id: str = Field(default="", max_length=200)
    request_id: str = Field(default="", max_length=200)
    language: Literal["auto", "ru", "en"] = "ru"
    quality: Literal["fast", "accurate"] = "accurate"
    timestamps: Literal["none", "segments", "words"] = "segments"
    formatting: Literal["formatted", "plain"] = "formatted"
    diarization: bool = False
    diarization_engine: DiarizationEngine | None = None
    audio_stream_index: int | None = None


class DiarizationActivate(BaseModel):
    token: str = Field(min_length=1, max_length=500)


class SpeakerAliasPatch(BaseModel):
    name: str = Field(default="", max_length=80)


class DictationToProject(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    folder_id: str | None = None


class DictationMessagePatch(BaseModel):
    text: str | None = Field(default=None, max_length=2_000_000)
    status: Literal["draft", "confirmed"] | None = None


def _diarization_engine(enabled: bool, requested: str | None) -> str | None:
    """Движок разметки для нового задания, или None, если разметка не нужна.

    Движок, не готовый к работе, — это 409 с объяснением, а не молчаливая
    подмена другим: человек выбрал Nemotron ради сравнения, и тихий Pyannote
    вместо него испортил бы это сравнение.
    """
    if not enabled:
        return None
    engine = diarization_service.resolve_engine(requested)
    if not diarization_service.engine_available(engine):
        title = diarization_service.ENGINE_TITLES[engine]
        raise HTTPException(
            status_code=409,
            detail=(
                f"Разделение по спикерам ({title}) не готово. "
                "Откройте Настройки → Спикеры."
            ),
        )
    return engine


def _with_engine(options: dict, engine: str | None) -> dict:
    if engine:
        options["diarization_engine"] = engine
    return options


def _queue_for(task_kind: str) -> queue.PriorityQueue[tuple[int, int, str, str]]:
    return dictation_queue if task_kind == "dictation" else batch_queue


def enqueue_project_job(job_id: str, priority: int = 1) -> None:
    batch_queue.put((priority, next(task_sequence), "project", job_id))


def enqueue_dictation_fragment(fragment_id: str, priority: int = 0) -> None:
    dictation_queue.put((priority, next(task_sequence), "dictation", fragment_id))


def enqueue_diarization_job(job_id: str, priority: int = 1) -> None:
    batch_queue.put((priority, next(task_sequence), "diarization", job_id))


def _task_queue_snapshot() -> list[tuple[int, int, str, str]]:
    """Обе очереди одним списком, диктовка первой.

    Порядок здесь определяет номера в интерфейсе, поэтому диктовка идёт
    впереди: она и в самом деле обрабатывается отдельным потоком, не дожидаясь
    файлов.
    """
    items: list[tuple[int, int, str, str]] = []
    for work_queue in (dictation_queue, batch_queue):
        with work_queue.mutex:
            items.extend(sorted(list(work_queue.queue)))
    return items


def _promote_queued_task(task_kind: str, task_id: str) -> None:
    if any(
        priority < 0 and kind == task_kind and queued_id == task_id
        for priority, _, kind, queued_id in _task_queue_snapshot()
    ):
        return
    _queue_for(task_kind).put((-1, next(task_sequence), task_kind, task_id))


def _job_task_kind(job: dict) -> str:
    if job["options"].get("_rerun_mode") == "diarization":
        return "diarization"
    return "project"


def _queue_job_item(
    job: dict,
    projects: dict[str, dict],
    *,
    position: int | None,
    priority: bool,
) -> dict:
    operation = (
        "Разметка спикеров"
        if _job_task_kind(job) == "diarization"
        else "Транскрибация"
    )
    project = projects.get(job["session_id"])
    return {
        "id": job["id"],
        "kind": "job",
        "operation": operation,
        "title": job["original_name"],
        "project_id": job["session_id"],
        "project_name": project["name"] if project else "",
        "status": job["status"],
        "position": position,
        "priority": priority,
        "cancel_requested": job["id"] in cancellation.requested_ids(),
    }


def _queue_dictation_item(
    fragment: dict,
    *,
    position: int | None,
    priority: bool,
) -> dict:
    return {
        "id": fragment["id"],
        "kind": "dictation",
        "operation": "Диктовка",
        "title": f"Диктовка · фрагмент {fragment['position']}",
        "project_id": None,
        "project_name": "Быстрая диктовка",
        "status": fragment["status"],
        "position": position,
        "priority": priority,
        "cancel_requested": fragment["id"] in cancellation.requested_ids(),
    }


def queue_overview() -> dict:
    jobs = storage.list_jobs()
    jobs_by_id = {item["id"]: item for item in jobs}
    projects = {item["id"]: item for item in storage.list_sessions()}
    dictation_fragments = {
        fragment["id"]: fragment
        for message in storage.list_dictation_messages()
        for fragment in message["fragments"]
    }

    active: dict | None = None
    for job_state in jobs:
        if job_state["status"] == "processing":
            active = _queue_job_item(
                job_state,
                projects,
                position=None,
                priority=False,
            )
            break
    if active is None:
        for fragment in dictation_fragments.values():
            if fragment["status"] == "processing":
                active = _queue_dictation_item(
                    fragment,
                    position=None,
                    priority=False,
                )
                break

    waiting: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for priority, _, task_kind, task_id in _task_queue_snapshot():
        key = (task_kind, task_id)
        if key in seen:
            continue
        if task_kind == "dictation":
            fragment = dictation_fragments.get(task_id)
            if not fragment or fragment["status"] != "queued":
                continue
            item = _queue_dictation_item(
                fragment,
                position=len(waiting) + 1,
                priority=priority < 0,
            )
        else:
            job_state = jobs_by_id.get(task_id)
            if not job_state or job_state["status"] != "queued":
                continue
            item = _queue_job_item(
                job_state,
                projects,
                position=len(waiting) + 1,
                priority=priority < 0,
            )
        seen.add(key)
        waiting.append(item)

    # Сторож здесь, а не в отдельном потоке: интерфейс опрашивает очередь
    # постоянно, поэтому проверка живости бесплатна.
    _ensure_workers()
    return {
        "active": active,
        "waiting": waiting,
        "total": len(waiting) + (1 if active else 0),
        # Раньше это поле было только в /api/health, который интерфейс не
        # запрашивал: если очередь вставала, узнать об этом было негде.
        "worker_alive": workers_alive(),
    }


def _still_queued(task_kind: str, task_id: str) -> bool:
    """Задача могла быть остановлена или уже обработана, пока ждала в очереди.

    Защищает и от повторной записи одного и того же id в очередь: после
    обработки статус уже не 'queued', поэтому дубликат просто пропускается.
    """
    if task_kind == "dictation":
        return storage.get_dictation_fragment(task_id)["status"] == "queued"
    return storage.get_job(task_id)["status"] == "queued"


MODEL_CACHE = idle_models.IdleModels(release_models, seconds=60)


def _run_task(task_kind: str, task_id: str) -> None:
    with MODEL_CACHE.activity():
        if task_kind == "dictation":
            dictation.transcribe_dictation_fragment(task_id)
        elif task_kind == "diarization":
            diarize_existing_job(task_id)
        else:
            transcribe_job(task_id)


def _mark_task_failed(task_kind: str, task_id: str, error: Exception) -> None:
    """Записать провал задачи, не рискуя рабочим потоком.

    Раньше эта запись жила прямо в `except` рабочего цикла и ловила только
    KeyError. Любая другая ошибка базы — та же «database is locked» — вылетала
    из цикла наружу, поток-демон умирал, и очередь молча вставала навсегда.
    Единственное место, где это было бы видно, — `worker_alive` в /api/health,
    который интерфейс не запрашивал.
    """
    try:
        if task_kind == "dictation":
            storage.update_dictation_fragment(
                task_id,
                status="failed",
                error=str(error),
                finished_at=storage.utc_now(),
            )
        else:
            storage.update_job(
                task_id,
                status="failed",
                progress=0,
                error=str(error),
                finished_at=storage.utc_now(),
            )
    except KeyError:
        pass
    except Exception:
        logger.exception(
            "Не удалось записать провал задачи %s %s", task_kind, task_id
        )


def worker_loop(
    work_queue: queue.PriorityQueue[tuple[int, int, str, str]],
) -> None:
    """Рабочий цикл, который обязан переживать любую ошибку задачи."""
    while True:
        try:
            _, _, task_kind, task_id = work_queue.get()
        except Exception:
            logger.exception("Сбой чтения очереди")
            time.sleep(0.5)
            continue
        try:
            if _still_queued(task_kind, task_id):
                _run_task(task_kind, task_id)
        except KeyError:
            pass
        except Exception as exc:
            _mark_task_failed(task_kind, task_id, exc)
        finally:
            # Ни один из этих шагов не имеет права уронить поток.
            try:
                cancellation.release(task_id)
            except Exception:
                logger.exception("Сбой освобождения задачи %s", task_id)
            try:
                work_queue.task_done()
            except Exception:
                logger.exception("Сбой task_done для %s", task_id)


WORKERS: tuple[tuple[str, queue.PriorityQueue[tuple[int, int, str, str]]], ...] = (
    ("whisper-worker", batch_queue),
    ("whisper-dictation", dictation_queue),
)


def _ensure_workers() -> None:
    """Поднять рабочие потоки; мёртвый поток заменить новым.

    Вызывается и при старте, и из queue_overview: интерфейс опрашивает очередь
    постоянно, поэтому проверка идёт бесплатно и служит сторожем.
    """
    for name, work_queue in WORKERS:
        thread = worker_threads.get(name)
        if thread is not None and thread.is_alive():
            continue
        if thread is not None:
            logger.error("Рабочий поток %s не жив, поднимаю заново", name)
        thread = threading.Thread(
            target=worker_loop,
            args=(work_queue,),
            name=name,
            daemon=True,
        )
        worker_threads[name] = thread
        thread.start()


def workers_alive() -> bool:
    return len(worker_threads) == len(WORKERS) and all(
        thread.is_alive() for thread in worker_threads.values()
    )


def _finalize_url_download(
    project_id: str,
    downloaded_path: Path,
    download: dict,
) -> str:
    resolved_path = downloaded_path.resolve()
    for existing in storage.list_jobs(session_id=project_id):
        try:
            existing_path = Path(existing["stored_path"]).resolve()
        except (KeyError, OSError):
            continue
        if existing_path == resolved_path:
            return str(existing["id"])

    metadata = media.probe_media(resolved_path)
    if not metadata["audio_streams"]:
        # Без этой проверки был IndexError, и загрузка помечалась как ошибка
        # скачивания, хотя файл скачан целиком и лежит на диске.
        raise RuntimeError(
            f"В «{resolved_path.name}» нет звуковой дорожки. "
            "Файл скачан и сохранён, но расшифровывать в нём нечего."
        )
    if not download.get("transcribe", True):
        # Просили только скачать. Файл заводится в проекте тем же способом, что
        # и добавленный вручную: он готов к работе, лежит в списке файлов и
        # ждёт, пока его поставят в очередь — сейчас или через неделю.
        item = storage.create_media(
            uuid.uuid4().hex,
            project_id,
            resolved_path.name,
            resolved_path,
            metadata,
            storage_mode="managed",
        )
        logging.getLogger("whisper").info(
            "Скачано без расшифровки: %s (%s)", resolved_path.name, item["id"]
        )
        return ""
    audio_stream = metadata["audio_streams"][0]
    current_settings = storage.get_settings()
    requested = download.get("transcription_options") or {}
    requested_diarization = bool(
        requested.get(
            "diarization",
            current_settings["default_diarization"] == "on",
        )
    )
    # Остальные пути постановки в очередь отдают на это 409 с внятным текстом
    # (enqueue_staged_media и соседи). Здесь спикеры отключались молча: человек
    # просил разметку, скачивание проходило, разметки не было и никто не
    # объяснял почему. Теперь причина хотя бы попадает в ошибку задания.
    engine = diarization_service.resolve_engine(
        requested.get("diarization_engine")
    )
    diarization_available = diarization_service.engine_available(engine)
    diarization_enabled = requested_diarization and diarization_available
    diarization_skipped = requested_diarization and not diarization_available
    options = {
        "language": requested.get(
            "language", current_settings["default_language"]
        ),
        "quality": requested.get(
            "quality", current_settings["default_quality"]
        ),
        "timestamps": requested.get("timestamps") or current_settings["default_timestamps"],
        "formatting": requested.get("formatting") or current_settings["default_formatting"],
        "model": requested.get("model", "studio"),
        "audio_stream_index": int(audio_stream["index"]),
        "audio_track": audio_stream,
        "diarization": diarization_enabled,
        "source_url": download.get("url"),
        "source_provider": download.get("provider"),
    }
    project_source = storage.get_session(project_id).get("integration")
    if project_source in integrations.REGISTRY:
        options["integration_source"] = project_source
    _with_engine(options, engine if diarization_enabled else None)
    job = storage.create_job(
        project_id,
        resolved_path.name,
        resolved_path,
        options,
        storage_mode="managed",
    )
    if diarization_skipped:
        storage.update_job(
            job["id"],
            error=(
                "Разделение по спикерам "
                f"({diarization_service.ENGINE_TITLES[engine]}) не готово, "
                "файл поставлен в очередь без разметки. "
                "Откройте Настройки → Спикеры."
            ),
        )
    enqueue_project_job(job["id"])
    return str(job["id"])


def _purge_temporary_files() -> int:
    """Убрать мусор из корня рабочей папки, оставшийся после аварий.

    Штатное завершение задачи чистит за собой (`finally` в transcribe_job), но
    после жёсткого убийства процесса извлечённая дорожка остаётся на диске — на
    многочасовом видео это гигабайты. Подкаталоги (`waveforms`,
    `track-previews`) не трогаются: это кеш со своим сроком жизни, он чистится
    по media_id.
    """
    removed = 0
    try:
        root = storage.temporary_dir()
    except Exception:
        return 0
    for path in root.glob("*"):
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("Не удалось удалить временный файл %s", path)
    if removed:
        logger.info("Удалено временных файлов: %s", removed)
    return removed


@app.on_event("startup")
def startup() -> None:
    storage.init_db()
    # Проекты интеграций, созданные до появления папок, подбираются один раз при
    # старте: иначе порядок навёлся бы только для новых файлов, а уже готовые
    # разборы остались бы лежать в общей ленте без опознавательных знаков.
    adopted = integrations.adopt_legacy_projects()
    if adopted:
        logging.getLogger("whisper").info(
            "Проектов интеграций перенесено в папки: %s", adopted
        )
    # Готовые файлы проекта появляются сами после каждого распознавания. Тем,
    # что закончились раньше этого правила, они дописываются здесь — один раз:
    # у кого файлы уже на месте, тот пропускается без чтения результатов.
    filled = exporter.backfill_project_files()
    if filled:
        logging.getLogger("whisper").info(
            "Проектов с дописанными файлами расшифровки: %s", filled
        )
    _purge_temporary_files()
    for job_id in storage.queued_job_ids():
        queued_job = storage.get_job(job_id)
        if queued_job["options"].get("_rerun_mode") == "diarization":
            enqueue_diarization_job(job_id)
        else:
            enqueue_project_job(job_id)
    for fragment_id in storage.queued_dictation_fragment_ids():
        enqueue_dictation_fragment(fragment_id)
    _ensure_workers()
    video_downloader.configure_completion_handler(
        _finalize_url_download
    )
    video_downloader.recover_interrupted_downloads()


def _with_queue_positions(items: list[dict]) -> list[dict]:
    positions = {
        job_id: index
        for index, job_id in enumerate(storage.queued_job_ids(), start=1)
    }
    pending_stop = cancellation.requested_ids()
    for item in items:
        item["queue_position"] = positions.get(item["id"])
        # Остановка выполняющейся задачи снимается не мгновенно: интерфейс
        # должен честно показывать «Останавливаю…», и это состояние обязано
        # переживать перезагрузку страницы, поэтому берём его с сервера.
        item["cancel_requested"] = item["id"] in pending_stop
    return items


def _run_picker(script_name: str, *arguments: str) -> str:
    command = [
        "pwsh.exe",
        "-NoProfile",
        "-STA",
        "-File",
        str(ROOT / "scripts" / script_name),
        *arguments,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Окно выбора не открылось")
    return result.stdout.strip()


def _source_path(item: dict) -> Path:
    value = item.get("source_path") or item.get("stored_path")
    if not value:
        raise FileNotFoundError
    path = Path(value)
    if not path.exists():
        raise FileNotFoundError(path)
    return path.resolve()


def _reveal_in_explorer(path: Path) -> None:
    """Открыть родительскую папку и выделить конкретный файл в Explorer."""
    resolved = path.resolve()
    # Explorer требует синтаксис /select,"полный путь". Если передать ключ и
    # путь одним автоматически заключённым аргументом, он может проигнорировать
    # цель и открыть системную папку «Документы».
    quoted_path = subprocess.list2cmdline([str(resolved)])
    subprocess.Popen(
        f"explorer.exe /select,{quoted_path}",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


@app.get("/api/health")
def health() -> dict:
    # Тот же сторож, что и в queue_overview: этот эндпоинт опрашивают start.ps1
    # и внешняя интеграция, и им тоже незачем видеть мёртвый поток.
    _ensure_workers()
    return {
        "ok": True,
        "queued": (
            len(storage.queued_job_ids())
            + len(storage.queued_dictation_fragment_ids())
        ),
        "worker_alive": workers_alive(),
        "workers": {
            name: thread.is_alive()
            for name, thread in worker_threads.items()
        },
        "loaded_models": loaded_model_keys(),
        "model_idle_seconds": MODEL_CACHE.seconds,
    }


@app.get("/api/queue")
def queue_state() -> dict:
    return queue_overview()


@app.get("/api/settings")
def settings() -> dict[str, str]:
    return storage.get_settings()


@app.patch("/api/settings")
def edit_settings(payload: SettingsPatch) -> dict[str, str]:
    values = payload.model_dump(exclude_none=True)
    try:
        return storage.update_settings(values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/pick-folder")
def pick_settings_folder(payload: FolderPick) -> dict[str, str]:
    current = storage.get_settings()[payload.key]
    try:
        selected = _run_picker("pick_folder.ps1", "-InitialPath", current)
        if not selected:
            return storage.get_settings()
        return storage.update_settings({payload.key: selected})
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/system/open-folder")
def open_folder(payload: OpenFolder) -> dict[str, bool]:
    path = Path(payload.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Папка не найдена")
    target = path if path.is_dir() else path.parent
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Папка не найдена")
    os.startfile(str(target))
    return {"ok": True}


def _shutdown_local_service() -> None:
    time.sleep(0.8)
    runtime_dir = ROOT / "run"
    frontend_pid_file = runtime_dir / "frontend.pid"
    backend_pid_file = runtime_dir / "backend.pid"

    try:
        frontend_pid = int(frontend_pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        frontend_pid = 0
    if frontend_pid > 0:
        subprocess.run(
            ["taskkill", "/PID", str(frontend_pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    frontend_pid_file.unlink(missing_ok=True)

    try:
        saved_backend_pid = int(
            backend_pid_file.read_text(encoding="utf-8").strip()
        )
    except (FileNotFoundError, ValueError):
        saved_backend_pid = 0
    if saved_backend_pid == os.getpid():
        backend_pid_file.unlink(missing_ok=True)
    os._exit(0)


@app.post("/api/system/shutdown")
def shutdown_service(
    x_whisper_local: str | None = Header(default=None),
) -> dict[str, bool]:
    if x_whisper_local != "shutdown":
        raise HTTPException(status_code=403, detail="Команда не подтверждена")
    threading.Thread(
        target=_shutdown_local_service,
        name="whisper-shutdown",
        daemon=False,
    ).start()
    return {"ok": True}


@app.get("/api/folders")
def folders() -> list[dict]:
    return storage.list_folders()


@app.post("/api/folders")
def new_folder(payload: FolderCreate) -> dict:
    return storage.create_folder(payload.name)


@app.patch("/api/folders/{folder_id}")
def edit_folder(folder_id: str, payload: FolderPatch) -> dict:
    values = payload.model_dump(exclude_unset=True)
    try:
        return storage.update_folder(
            folder_id,
            name=values.get("name"),
            feed_visible=values.get("feed_visible"),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Папка не найдена") from exc


@app.delete("/api/folders/{folder_id}")
def remove_folder(
    folder_id: str, delete_projects: bool = False
) -> dict[str, bool]:
    if delete_projects:
        active = video_downloader.active_project_ids()
        affected = {
            item["id"]
            for item in storage.list_sessions()
            if item.get("folder_id") == folder_id
        }
        if active & affected:
            raise HTTPException(
                status_code=409,
                detail="В папке есть проект, который сейчас скачивается",
            )
    try:
        storage.delete_folder(folder_id, delete_projects=delete_projects)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Папка не найдена") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/url-downloads/probe")
def probe_url_download(payload: UrlProbe) -> dict:
    try:
        result = video_downloader.probe_metadata(payload.url, payload.passcode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Интерфейсу нужно знать, что дело в коде доступа, а не в битой ссылке:
        # по этому признаку он показывает поле для кода.
        raise HTTPException(
            status_code=400,
            detail=video_downloader.humanize_error(exc),
            headers={
                "X-Whisper-Needs-Passcode": (
                    "1" if video_downloader.needs_passcode(payload.url) else "0"
                )
            },
        ) from exc
    result["needs_passcode"] = video_downloader.needs_passcode(payload.url)
    return result


@app.get("/api/url-downloads")
def url_downloads() -> list[dict]:
    return video_downloader.list_downloads()


@app.post("/api/url-downloads", status_code=202)
def start_url_download(payload: UrlDownloadStart) -> dict:
    try:
        metadata = video_downloader.probe_metadata(payload.url, payload.passcode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=video_downloader.humanize_error(exc),
        ) from exc

    current_settings = storage.get_settings()
    transcribe = (
        payload.transcribe
        if payload.transcribe is not None
        else current_settings["download_transcribe"] == "on"
    )
    transcription_options = {
        "model": payload.transcription_model,
        "timestamps": payload.transcription_timestamps,
        "formatting": payload.transcription_formatting,
        "language": (
            payload.transcription_language
            or current_settings["default_language"]
        ),
        "quality": (
            payload.transcription_quality
            or current_settings["default_quality"]
        ),
        "diarization": (
            payload.transcription_diarization
            if payload.transcription_diarization is not None
            else current_settings["default_diarization"] == "on"
        ),
    }
    engine = _diarization_engine(
        transcribe and transcription_options["diarization"],
        payload.transcription_diarization_engine,
    )
    if engine:
        transcription_options["diarization_engine"] = engine

    created_project = False
    if payload.project_id:
        try:
            project = storage.get_session(payload.project_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Проект не найден",
            ) from exc
    else:
        try:
            project = storage.create_session(
                payload.project_name or metadata["title"],
                payload.folder_id,
            )
            created_project = True
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Папка не найдена") from exc

    try:
        download = video_downloader.start_download(
            project["id"],
            payload.url,
            payload.quality,
            metadata,
            transcription_options,
            passcode=payload.passcode,
            transcribe=transcribe,
        )
    except ValueError as exc:
        if created_project:
            storage.delete_session(project["id"])
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        if created_project:
            storage.delete_session(project["id"])
        raise
    return {
        "project": project,
        "download": download,
        "metadata": metadata,
    }


@app.get("/api/url-downloads/{project_id}")
def url_download_status(project_id: str) -> dict:
    try:
        storage.get_session(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc
    state = video_downloader.get_status(project_id)
    return state or {"project_id": project_id, "status": "idle"}


@app.post("/api/url-downloads/{project_id}/pause")
def pause_url_download(project_id: str) -> dict:
    if not video_downloader.pause(project_id):
        raise HTTPException(
            status_code=409,
            detail="Активная загрузка не найдена",
        )
    return video_downloader.get_status(project_id) or {"status": "pausing"}


@app.post("/api/url-downloads/{project_id}/resume", status_code=202)
def resume_url_download(project_id: str) -> dict:
    ok, error = video_downloader.resume(project_id)
    if not ok:
        raise HTTPException(status_code=409, detail=error)
    return video_downloader.get_status(project_id) or {"status": "resuming"}


@app.post("/api/url-downloads/{project_id}/cancel")
def cancel_url_download(project_id: str) -> dict:
    if not video_downloader.cancel(project_id):
        raise HTTPException(
            status_code=409,
            detail="Загрузка уже завершена или не найдена",
        )
    return video_downloader.get_status(project_id) or {"status": "stopping"}


def _with_integration_titles(projects: list[dict]) -> list[dict]:
    """Дописать читаемое название сервиса к проектам интеграций.

    Название живёт в реестре на сервере, поэтому интерфейсу не нужно держать
    вторую копию списка сервисов — добавление сервиса остаётся одной записью.
    """
    for project in projects:
        key = project.get("integration")
        if key:
            project["integration_title"] = integrations.title(key)
    return projects


def _with_download_state(items: list[dict]) -> list[dict]:
    for item in items:
        download = video_downloader.get_status(item["id"])
        item["download_status"] = download["status"] if download else None
        item["download_percent"] = download["percent"] if download else None
        item["download_error"] = download["error"] if download else None
    return _with_integration_titles(items)


def _page_bounds(page: int, per_page: int | None) -> tuple[int | None, int]:
    """Границы страницы. `per_page = None` — «Все», без разбиения."""
    if per_page is None or per_page <= 0:
        return None, 0
    limit = min(per_page, 5000)
    return limit, max(0, (max(1, page) - 1) * limit)


@app.get("/api/projects")
@app.get("/api/sessions")
def projects(
    response: Response,
    folder: str | None = None,
    page: int = 1,
    per_page: int | None = None,
    order: Literal["recent", "folder"] = "recent",
) -> list[dict]:
    """Страница ленты проектов.

    Общее количество уходит заголовком `X-Total-Count`: тело остаётся
    обычным списком, и старые вызовы без `per_page` работают как раньше —
    отдают библиотеку целиком.
    """
    in_feed = folder in (None, "", "all")
    if in_feed:
        folder_filter: str | None | object = ...
    elif folder == "none":
        folder_filter = None
    else:
        folder_filter = folder
    # Скрытия — свойство общей ленты. Внутри папки показываем всё: человек
    # зашёл в неё именно затем, чтобы увидеть содержимое.
    hide_integrations = (
        in_feed and storage.get_settings()["integration_feed"] == "hide"
    )
    limit, offset = _page_bounds(page, per_page)
    total = storage.count_sessions(
        folder_filter, hide_integrations, hide_quiet_folders=in_feed
    )
    items = storage.list_sessions(
        folder_filter,
        hide_integrations,
        hide_quiet_folders=in_feed,
        order=order,
        offset=offset,
        limit=limit,
    )
    response.headers["X-Total-Count"] = str(total)
    return _with_download_state(items)


@app.get("/api/projects/counts")
def project_counts() -> dict[str, int]:
    """Счётчики для навигации: они не зависят от открытой страницы.

    Объявлено выше `/api/projects/{project_id}`: маршруты разбираются по
    порядку, и иначе «counts» ушло бы в него как идентификатор проекта.
    """
    hide_integrations = storage.get_settings()["integration_feed"] == "hide"
    return {
        "feed": storage.count_sessions(
            hide_integrations=hide_integrations, hide_quiet_folders=True
        ),
        "loose": storage.count_sessions(
            None, hide_integrations, hide_quiet_folders=True
        ),
        "total": storage.count_sessions(),
    }


@app.get("/api/projects/{project_id}")
def project(project_id: str) -> dict:
    """Один проект — для открытого экрана, когда его нет на текущей странице."""
    try:
        item = storage.get_session(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc
    return _with_download_state([item])[0]


@app.post("/api/projects")
@app.post("/api/sessions")
def new_project(payload: ProjectCreate) -> dict:
    try:
        return storage.create_session(payload.name, payload.folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Папка не найдена") from exc


@app.patch("/api/projects/{project_id}")
@app.patch("/api/sessions/{project_id}")
def edit_project(project_id: str, payload: ProjectPatch) -> dict:
    values = payload.model_dump(exclude_unset=True)
    try:
        project = storage.update_session(project_id, **values)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Проект или папка не найдены"
        ) from exc
    # Файлы расшифровки названы по проекту: после переименования их надо
    # переписать, иначе рядом останется расшифровка под прежним именем.
    if "name" in values:
        exporter.refresh_project_files(project_id)
    return project


@app.delete("/api/projects/{project_id}")
def remove_project(project_id: str) -> dict[str, bool]:
    if project_id in video_downloader.active_project_ids():
        raise HTTPException(
            status_code=409,
            detail="Сначала остановите скачивание этого проекта",
        )
    try:
        storage.delete_session(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    video_downloader.forget(project_id)
    return {"ok": True}


@app.post("/api/projects/bulk/folder")
def bulk_move_projects(payload: ProjectsBulkMove) -> dict:
    """Перенести несколько проектов в папку (или вынуть из папок).

    Проекты обрабатываются по одному, и отказ по одному не отменяет остальные:
    у выделения из десятка проектов один недоступный не должен блокировать
    операцию целиком. Пропущенные возвращаются с причиной.
    """
    moved: list[str] = []
    skipped: list[dict[str, str]] = []
    for project_id in payload.ids:
        try:
            storage.update_session(project_id, folder_id=payload.folder_id)
            moved.append(project_id)
        except KeyError:
            skipped.append({"id": project_id, "reason": "Проект или папка не найдены"})
    return {"moved": len(moved), "skipped": skipped}


@app.post("/api/projects/bulk/delete")
def bulk_delete_projects(payload: ProjectsBulkDelete) -> dict:
    """Удалить несколько проектов вместе с их файлами.

    Те же правила, что и у одиночного удаления: проект с активной загрузкой по
    ссылке или с выполняющимся файлом не удаляется. Такие пропускаются с
    причиной, остальные удаляются.
    """
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    active = video_downloader.active_project_ids()
    for project_id in payload.ids:
        if project_id in active:
            skipped.append(
                {
                    "id": project_id,
                    "reason": "Идёт скачивание по ссылке",
                }
            )
            continue
        try:
            storage.delete_session(project_id)
        except KeyError:
            skipped.append({"id": project_id, "reason": "Проект не найден"})
            continue
        except RuntimeError as exc:
            skipped.append({"id": project_id, "reason": str(exc)})
            continue
        video_downloader.forget(project_id)
        deleted.append(project_id)
    return {"deleted": len(deleted), "skipped": skipped}


@app.get("/api/media")
def staged_media(session_id: str | None = None) -> list[dict]:
    return storage.list_media(session_id=session_id)


@app.post("/api/media")
async def upload_media(
    files: list[UploadFile] = File(...),
    session_id: str = Form(...),
) -> list[dict]:
    try:
        storage.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc

    created: list[dict] = []
    for upload in files:
        media_id = uuid.uuid4().hex
        safe_name = Path(upload.filename or "media").name
        suffix = Path(safe_name).suffix
        folder = storage.session_dir(session_id) / "media"
        folder.mkdir(parents=True, exist_ok=True)
        # Копия исходника называется как исходник. Раньше здесь стоял
        # идентификатор записи, и папка `media` выглядела набором
        # шестнадцатеричных имён, по которым нельзя было понять, что внутри.
        destination = storage.unique_child(
            folder,
            storage.safe_file_name(Path(safe_name).stem, fallback="Запись"),
            suffix,
        )
        try:
            with destination.open("wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    target.write(chunk)
            metadata = media.probe_media(destination)
            created.append(
                storage.create_media(
                    media_id,
                    session_id,
                    safe_name,
                    destination,
                    metadata,
                )
            )
        except RuntimeError as exc:
            if destination.exists():
                destination.unlink()
            raise HTTPException(
                status_code=400,
                detail=f"Не удалось открыть «{safe_name}»: {exc}",
            ) from exc
        finally:
            await upload.close()
    return created


@app.post("/api/local-files/pick")
def pick_local_files(payload: LocalFilesPick) -> list[dict]:
    try:
        storage.get_session(payload.session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc
    try:
        raw = _run_picker("pick_files.ps1")
        selected = json.loads(raw or "[]")
    except (RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(selected, str):
        selected = [selected]
    created: list[dict] = []
    for value in selected:
        source = Path(value)
        if not source.is_file():
            continue
        try:
            metadata = media.probe_media(source)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Не удалось открыть «{source.name}»: {exc}",
            ) from exc
        created.append(
            storage.create_media(
                uuid.uuid4().hex,
                payload.session_id,
                source.name,
                source,
                metadata,
                source_path=source,
                storage_mode="reference",
            )
        )
    return created


@app.delete("/api/media/{media_id}")
def remove_staged_media(media_id: str) -> dict[str, bool]:
    try:
        storage.delete_media(media_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Файл не найден") from exc
    media.clear_track_previews(media_id)
    return {"ok": True}


@app.get("/api/media/{media_id}/track-preview")
def staged_media_track_preview(
    media_id: str,
    stream_index: int,
    start_seconds: float = 0,
):
    try:
        item = storage.get_media(media_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Файл не найден") from exc

    stream = next(
        (
            candidate
            for candidate in item["audio_streams"]
            if int(candidate["index"]) == stream_index
        ),
        None,
    )
    if stream is None:
        raise HTTPException(
            status_code=400,
            detail="Выбранной аудиодорожки нет в этом файле",
        )

    source = Path(item["stored_path"])
    if not source.is_file():
        raise HTTPException(
            status_code=404,
            detail="Исходный файл больше не найден по сохранённому пути",
        )
    total_duration = max(0.0, float(item["duration_seconds"]))
    maximum_start = max(0.0, total_duration - 1.0)
    safe_start = min(max(0.0, start_seconds), maximum_start)
    preview_duration = min(30.0, max(1.0, total_duration - safe_start))
    try:
        preview = media.create_track_preview(
            media_id,
            source,
            stream_index,
            safe_start,
            preview_duration,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось подготовить прослушивание: {exc}",
        ) from exc
    return FileResponse(
        preview,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Preview-Start": f"{safe_start:.3f}",
            "X-Preview-Duration": f"{preview_duration:.3f}",
        },
    )


def _file_clock(seconds: float) -> str:
    """Время для имени файла: двоеточие в именах Windows не разрешено."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}-{minutes:02}-{secs:02}"
    return f"{minutes:02}-{secs:02}"


def _staged_track(media_id: str, stream_index: int) -> tuple[dict, Path]:
    """Запись о файле и путь к исходнику с проверкой выбранной дорожки."""
    try:
        item = storage.get_media(media_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Файл не найден") from exc

    if not any(
        int(candidate["index"]) == stream_index
        for candidate in item["audio_streams"]
    ):
        raise HTTPException(
            status_code=400,
            detail="Выбранной аудиодорожки нет в этом файле",
        )

    source = Path(item["stored_path"])
    if not source.is_file():
        raise HTTPException(
            status_code=404,
            detail="Исходный файл больше не найден по сохранённому пути",
        )
    return item, source


@app.get("/api/media/{media_id}/waveform")
def staged_media_waveform(media_id: str, stream_index: int) -> dict:
    _, source = _staged_track(media_id, stream_index)
    try:
        return media.build_waveform(media_id, source, stream_index)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось построить дорожку: {exc}",
        ) from exc


@app.get("/api/media/{media_id}/audio")
def staged_media_audio(media_id: str, stream_index: int):
    """Вся выбранная дорожка одним файлом — для прослушивания при обрезке.

    Отдаётся с поддержкой диапазонов, поэтому курсор по дорожке переставляется
    мгновенно и на многочасовой записи.
    """
    _, source = _staged_track(media_id, stream_index)
    try:
        media.build_waveform(media_id, source, stream_index)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось подготовить дорожку: {exc}",
        ) from exc
    return FileResponse(
        media.track_audio_path(media_id, stream_index),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/media/{media_id}/fragment")
def staged_media_fragment(
    media_id: str,
    stream_index: int,
    trim_start_seconds: float = 0,
    trim_end_seconds: float | None = None,
):
    """Скачать ровно тот кусок, который уйдёт в распознавание."""
    item, source = _staged_track(media_id, stream_index)
    try:
        start, end = media.trim_bounds(
            item["duration_seconds"],
            trim_start_seconds,
            trim_end_seconds,
        )
        fragment = media.create_fragment(
            media_id,
            source,
            stream_index,
            start,
            end,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stem = Path(item["original_name"]).stem
    finish = "конец" if end is None else _file_clock(end)
    return FileResponse(
        fragment,
        media_type="audio/mpeg",
        filename=f"{stem} {_file_clock(start)}-{finish}.mp3",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/media/enqueue")
def enqueue_staged_media(payload: MediaEnqueue) -> list[dict]:
    engine = _diarization_engine(payload.diarization, payload.diarization_engine)
    validated: list[tuple[dict, MediaQueueItem, dict, tuple[float, float | None]]] = []
    for choice in payload.items:
        try:
            item = storage.get_media(choice.media_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Один из файлов не найден"
            ) from exc
        stream = next(
            (
                candidate
                for candidate in item["audio_streams"]
                if candidate["index"] == choice.audio_stream_index
            ),
            None,
        )
        if stream is None:
            raise HTTPException(
                status_code=400,
                detail=f"В «{item['original_name']}» нет выбранной аудиодорожки",
            )
        try:
            bounds = media.trim_bounds(
                item["duration_seconds"],
                choice.trim_start_seconds,
                choice.trim_end_seconds,
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"«{item['original_name']}»: {exc}",
            ) from exc
        validated.append((item, choice, stream, bounds))

    created = []
    for item, choice, stream, (trim_start, trim_end) in validated:
        options = {
            "language": payload.language,
            "quality": payload.quality,
            "timestamps": payload.timestamps,
            "formatting": payload.formatting,
            "audio_stream_index": choice.audio_stream_index,
            "audio_track": stream,
            "diarization": payload.diarization,
        }
        _with_engine(options, engine)
        if trim_start > 0 or trim_end is not None:
            options["trim_start_seconds"] = round(trim_start, 3)
            options["trim_end_seconds"] = (
                round(trim_end, 3) if trim_end is not None else None
            )
        job = storage.create_job(
            item["session_id"],
            item["original_name"],
            Path(item["stored_path"]),
            options,
            source_path=Path(item["source_path"]) if item.get("source_path") else None,
            storage_mode=item.get("storage_mode") or "managed",
        )
        storage.remove_media_record(item["id"])
        media.clear_track_previews(item["id"])
        created.append(job)
        enqueue_project_job(job["id"])
    return created


@app.get("/api/integrations")
def integrations_list() -> list[dict]:
    """Какие внешние сервисы пользуются этим Whisper и когда пользовались."""
    return integrations.connected()


@app.post("/api/integrations/project")
def integration_project(
    payload: IntegrationProject,
    x_whisper_integration: str | None = Header(default=None),
) -> dict:
    """Проект внешнего сервиса: находится по external_id или создаётся.

    Ручка общая, а не своя у каждого сервиса: логика одна и та же —
    папка сервиса, проект на его проект, отметка источника, — а сервисы
    отличаются только записью в REGISTRY. Телеграм-боту этого достаточно,
    чтобы дальше пользоваться обычными ручками загрузки и очереди.
    """
    integration = integrations.by_header(x_whisper_integration)
    if integration is None:
        raise HTTPException(status_code=403, detail="Интеграция не подтверждена")
    return integrations.project(integration, payload.external_id, payload.name)


@app.get("/api/components")
def components_inventory(refresh: bool = False) -> dict:
    """Из чего собран сервис и что из этого установлено."""
    return components.inventory(refresh=refresh)


@app.post("/api/components/models/download")
def components_download_models() -> dict:
    """Скачать отсутствующие модели Whisper.

    Только Whisper: Pyannote требует токена Hugging Face и принятия условий
    модели, для него есть отдельная активация. Программы вроде Python и FFmpeg
    ставятся системными установщиками с правами администратора — сервис для них
    показывает команду, но не выполняет её сам.
    """
    return components.start_model_download()


@app.get("/api/components/models/download")
def components_download_status() -> dict:
    return components.download_status()


@app.get("/api/diarization/status")
def diarization_status() -> dict:
    return diarization_service.status()


@app.post("/api/diarization/activate")
def diarization_activate(payload: DiarizationActivate) -> dict:
    try:
        return diarization_service.activate(payload.token)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/diarization/nemotron/download")
def diarization_nemotron_download() -> dict:
    """Скачать модель Nemotron 3 Diarization. Токен не нужен, модель открытая."""
    try:
        return diarization_service.download_nemotron()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _with_fragment_cancel_flags(messages: list[dict]) -> list[dict]:
    pending_stop = cancellation.requested_ids()
    for message in messages:
        for fragment in message["fragments"]:
            fragment["cancel_requested"] = fragment["id"] in pending_stop
    return messages


@app.get("/api/dictations")
def dictation_messages(
    response: Response,
    page: int = 1,
    per_page: int | None = None,
) -> list[dict]:
    """Страница истории диктовок.

    Черновик приходит всегда и вне разбиения: это рабочее место, а не запись
    истории, и переключение страницы не должно его прятать. В
    `X-Total-Count` — количество подтверждённых сообщений.
    """
    limit, offset = _page_bounds(page, per_page)
    if limit is None:
        items = storage.list_dictation_messages(limit=None)
    else:
        items = storage.list_dictation_messages(limit=None, status="draft")
        items += storage.list_dictation_messages(
            limit=limit, status="confirmed", offset=offset
        )
    response.headers["X-Total-Count"] = str(
        storage.count_dictation_messages("confirmed")
    )
    return _with_fragment_cancel_flags(items)


@app.post("/api/dictations")
def new_dictation_message() -> dict:
    return storage.create_dictation_message()


@app.patch("/api/dictations/{message_id}")
def edit_dictation_message(
    message_id: str,
    payload: DictationMessagePatch,
) -> dict:
    try:
        return storage.update_dictation_message(
            message_id,
            text=payload.text,
            status=payload.status,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Диктовка не найдена",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/dictations/history")
def clear_dictation_history() -> dict[str, int | bool]:
    try:
        deleted = storage.clear_confirmed_dictation_messages()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "deleted": deleted}


@app.delete("/api/dictations/{message_id}")
def remove_dictation_message(message_id: str) -> dict[str, bool]:
    try:
        storage.delete_dictation_message(message_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Диктовка не найдена",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/dictations/{message_id}/fragments")
async def add_dictation_fragment(
    message_id: str,
    audio: UploadFile = File(...),
    duration_seconds: float = Form(0),
    language: Literal["auto", "ru", "en"] = Form("ru"),
    quality: Literal["fast", "accurate"] = Form("accurate"),
) -> dict:
    try:
        storage.get_dictation_message(message_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Диктовка не найдена",
        ) from exc

    fragment_id = uuid.uuid4().hex
    base_mime = (audio.content_type or "").split(";", 1)[0].lower()
    if not base_mime or base_mime == "application/octet-stream":
        base_mime = (
            mimetypes.guess_type(audio.filename or "")[0] or "audio/webm"
        )
    suffix = {
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mp4": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
    }.get(base_mime, Path(audio.filename or "").suffix.lower() or ".webm")
    destination_dir = storage.dictation_message_dir(message_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{fragment_id}{suffix}"

    try:
        with destination.open("wb") as target:
            while chunk := await audio.read(1024 * 1024):
                target.write(chunk)
    finally:
        await audio.close()

    if not destination.exists() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Запись микрофона пуста")

    status = "queued"
    probe_error: str | None = None
    actual_duration = max(0.0, duration_seconds)
    try:
        metadata = media.probe_media(destination)
        actual_duration = float(metadata["duration_seconds"]) or actual_duration
        if not metadata["audio_streams"]:
            raise RuntimeError("В записи не найдена аудиодорожка")
    except RuntimeError as exc:
        status = "failed"
        probe_error = (
            "Аудио сохранено, но пока не читается: "
            f"{exc}"
        )

    try:
        fragment = storage.create_dictation_fragment(
            fragment_id,
            message_id,
            destination,
            base_mime,
            actual_duration,
            language,
            quality,
            status=status,
            error=probe_error,
        )
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if status == "queued":
        enqueue_dictation_fragment(fragment_id)
    return fragment


@app.post("/api/dictation-fragments/{fragment_id}/retry")
def retry_dictation_fragment(fragment_id: str) -> dict:
    try:
        fragment = storage.get_dictation_fragment(fragment_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Фрагмент не найден",
        ) from exc
    if fragment["status"] in {"queued", "processing"}:
        raise HTTPException(
            status_code=409,
            detail="Фрагмент уже находится в очереди",
        )
    if fragment["status"] == "completed":
        raise HTTPException(
            status_code=409,
            detail="Готовый фрагмент не требует повторного распознавания",
        )
    if not Path(fragment["audio_path"]).exists():
        raise HTTPException(
            status_code=404,
            detail="Исходное аудио фрагмента не найдено",
        )
    cancellation.release(fragment_id)
    updated = storage.update_dictation_fragment(
        fragment_id,
        status="queued",
        error=None,
        started_at=None,
        finished_at=None,
    )
    enqueue_dictation_fragment(fragment_id)
    return updated


@app.post("/api/dictation-fragments/{fragment_id}/cancel")
def cancel_dictation_fragment(fragment_id: str) -> dict:
    """Остановить распознавание фрагмента диктовки. Аудио остаётся на диске."""
    try:
        state = storage.cancel_queued_dictation_fragment(fragment_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Фрагмент не найден",
        ) from exc
    if state in {"completed", "failed", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="Этот фрагмент уже не распознаётся",
        )
    cancellation.request(fragment_id)
    return storage.get_dictation_fragment(fragment_id)


@app.post("/api/dictations/{message_id}/to-project")
def dictation_to_project(message_id: str, payload: DictationToProject) -> dict:
    """Сохранить диктовку в проект: аудио и готовая расшифровка.

    Диктовка остаётся в истории — это копия «на будущее», чтобы важное
    рассуждение не пропало при очистке истории.

    Каждый фрагмент становится отдельным файлом проекта: фрагменты и есть
    отдельные аудиозаписи, а объединённый результат проекта соберёт их в том же
    порядке. Аудио копируется, а не берётся ссылкой: смысл переноса в том, чтобы
    запись выжила при удалении диктовки.
    """
    try:
        message = storage.get_dictation_message(message_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Диктовка не найдена") from exc

    ready = [
        fragment
        for fragment in message["fragments"]
        if fragment["status"] == "completed" and fragment["transcript"].strip()
    ]
    if not ready:
        raise HTTPException(
            status_code=409,
            detail="В диктовке нет распознанных фрагментов",
        )

    project = storage.create_session(
        payload.name or None,
        payload.folder_id,
        storage.get_settings()["project_name_format"],
    )
    media_dir = storage.session_dir(project["id"]) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    created: list[dict] = []
    for number, fragment in enumerate(ready, start=1):
        source = Path(fragment["audio_path"])
        suffix = source.suffix or ".webm"
        target = storage.unique_child(
            media_dir,
            f"Диктовка · фрагмент {number}",
            suffix,
        )
        if source.is_file():
            shutil.copy2(source, target)
        job = storage.create_job(
            project["id"],
            f"Диктовка · фрагмент {number}{suffix}",
            target,
            {
                "language": fragment["language"],
                "quality": fragment["quality"],
                "timestamps": "segments",
                "formatting": "formatted",
                "diarization": False,
            },
        )
        duration = float(fragment["duration_seconds"] or 0)
        segments = [
            {
                # Диктовка распознаётся без меток слов, поэтому у фрагмента одна
                # реплика на всю длину. Это честнее выдуманных таймкодов внутри.
                "start": 0.0,
                "end": duration,
                "text": fragment["transcript"],
                "words": None,
            }
        ]
        _write_dictation_outputs(
            job,
            segments,
            {
                "language": fragment["detected_language"] or fragment["language"],
                "language_probability": 1.0,
                "duration": duration,
            },
        )
        storage.update_job(
            job["id"],
            status="completed",
            progress=1.0,
            detected_language=fragment["detected_language"]
            or fragment["language"],
            duration_seconds=duration,
            processing_seconds=fragment["processing_seconds"] or 0,
            finished_at=storage.utc_now(),
        )
        created.append(storage.get_job(job["id"]))

    storage.set_dictation_project(message_id, project["id"])

    # Правки текста живут в сообщении целиком и по фрагментам не раскладываются.
    # Если пользователь текст менял, честно говорим, что в проект ушли
    # расшифровки фрагментов, а не отредактированная версия.
    joined = "\n\n".join(fragment["transcript"].strip() for fragment in ready)
    return {
        "project": storage.get_session(project["id"]),
        "files": len(created),
        "text_edited": message["text"].strip() != joined.strip(),
    }


@app.delete("/api/dictation-fragments/{fragment_id}")
def remove_dictation_fragment(fragment_id: str) -> dict:
    """Убрать один фрагмент из диктовки, остановив его при необходимости.

    Раньше от неудачного фрагмента можно было избавиться только удалив всё
    сообщение, то есть потеряв и остальные записи. Если фрагмент ещё в очереди
    или считается прямо сейчас, сначала запрашивается остановка: держать
    видеокарту на записи, которая уже не нужна, незачем.
    """
    try:
        fragment = storage.get_dictation_fragment(fragment_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Фрагмент не найден"
        ) from exc

    if fragment["status"] in {"queued", "processing"}:
        try:
            storage.cancel_queued_dictation_fragment(fragment_id)
        except KeyError:
            pass
        cancellation.request(fragment_id)

    try:
        return storage.delete_dictation_fragment(fragment_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Фрагмент не найден"
        ) from exc


@app.post("/api/dictation-fragments/{fragment_id}/prioritize")
def prioritize_dictation_fragment(fragment_id: str) -> dict:
    try:
        fragment = storage.get_dictation_fragment(fragment_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Фрагмент не найден",
        ) from exc
    if fragment["status"] != "queued":
        raise HTTPException(
            status_code=409,
            detail="В приоритет можно переместить только ожидающий фрагмент",
        )
    _promote_queued_task("dictation", fragment_id)
    return queue_overview()


def _dictation_fragment_audio(fragment_id: str) -> tuple[dict, Path]:
    try:
        fragment = storage.get_dictation_fragment(fragment_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Фрагмент не найден",
        ) from exc
    path = Path(fragment["audio_path"])
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Исходное аудио не найдено",
        )
    return fragment, path


@app.get("/api/dictation-fragments/{fragment_id}/audio")
def dictation_fragment_audio(fragment_id: str):
    fragment, path = _dictation_fragment_audio(fragment_id)
    media_type = fragment["mime_type"] or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    return FileResponse(path, media_type=media_type)


@app.post("/api/dictation-fragments/{fragment_id}/reveal")
def reveal_dictation_fragment_audio(fragment_id: str) -> dict[str, bool]:
    """Открыть проводник с выделенным файлом записи.

    Именно с выделенным: сама папка ничего не говорит о том, какой из
    фрагментов нужен, а вытащить запись из диктовки иначе было негде.
    """
    _reveal_in_explorer(_dictation_fragment_audio(fragment_id)[1])
    return {"ok": True}


@app.get("/api/jobs")
def jobs(session_id: str | None = None) -> list[dict]:
    return _with_queue_positions(storage.list_jobs(session_id=session_id))


_WEBINAR_INTEGRATION_HEADER = "webinar-analyzer-local"
_WEBINAR_INTEGRATION_PROJECT = "Webinar Analyzer · локальная очередь"


def _check_webinar_integration_header(value: str | None) -> None:
    if value != _WEBINAR_INTEGRATION_HEADER:
        raise HTTPException(status_code=403, detail="Интеграция не подтверждена")


def _webinar_integration_session(
    external_id: str | None = None,
    external_name: str | None = None,
) -> dict:
    """Проект под конкретный разбор Анализа вебинаров.

    Раньше здесь возвращался один общий проект на все файлы этого сервиса, и он
    лежал на верхнем уровне рядом с обычными проектами. В общей ленте его нельзя
    было отличить от своих, а внутри росла свалка без разбора по разборам.
    Теперь проект создаётся на каждый проект внешнего сервиса и попадает в его
    папку.
    """
    integration = integrations.REGISTRY["webinar_analyzer"]
    return integrations.project(integration, external_id, external_name)


def _webinar_integration_jobs() -> list[dict]:
    result = []
    for item in storage.list_jobs(limit=10_000):
        options = item.get("options") or {}
        if options.get("integration_source") == "webinar_analyzer":
            result.append(item)
    return _with_queue_positions(result)


@app.get("/api/integrations/webinar-analyzer/jobs")
def webinar_analyzer_jobs(
    x_whisper_integration: str | None = Header(default=None),
) -> list[dict]:
    _check_webinar_integration_header(x_whisper_integration)
    return _webinar_integration_jobs()


@app.post("/api/integrations/webinar-analyzer/jobs", status_code=202)
def enqueue_webinar_analyzer_job(
    payload: WebinarAnalyzerReferenceJob,
    x_whisper_integration: str | None = Header(default=None),
) -> dict:
    _check_webinar_integration_header(x_whisper_integration)
    engine = _diarization_engine(payload.diarization, payload.diarization_engine)

    source = Path(payload.source_path).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Исходный файл не найден") from exc
    if not source.is_file():
        raise HTTPException(status_code=400, detail="Источник должен быть файлом")

    # Повтор одного и того же HTTP-запроса не создаёт дубликат в очереди.
    if payload.request_id:
        for existing in _webinar_integration_jobs():
            options = existing.get("options") or {}
            if (
                options.get("integration_request_id") == payload.request_id
                and str(Path(existing.get("stored_path") or "").resolve()) == str(source)
            ):
                return existing

    try:
        metadata = media.probe_media(source)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось открыть исходный файл: {exc}",
        ) from exc

    selected_stream = payload.audio_stream_index
    if selected_stream is None:
        selected_stream = int(metadata["audio_streams"][0]["index"])
    track = next(
        (
            stream
            for stream in metadata["audio_streams"]
            if int(stream["index"]) == int(selected_stream)
        ),
        None,
    )
    if track is None:
        raise HTTPException(
            status_code=400,
            detail=f"В файле нет аудиодорожки №{selected_stream}",
        )

    project = _webinar_integration_session(
        payload.external_project_id, payload.project_name
    )
    options = {
        "language": payload.language,
        "quality": payload.quality,
        "timestamps": payload.timestamps,
        "formatting": payload.formatting,
        "audio_stream_index": int(selected_stream),
        "audio_track": track,
        "diarization": payload.diarization,
        "integration_source": "webinar_analyzer",
        "integration_project_id": payload.external_project_id,
        "integration_project_name": payload.project_name,
        "integration_request_id": payload.request_id,
    }
    _with_engine(options, engine)
    job = storage.create_job(
        project["id"],
        source.name,
        source,
        options,
        source_path=source,
        storage_mode="reference",
    )
    enqueue_project_job(job["id"])
    return _with_queue_positions([storage.get_job(job["id"])])[0]


@app.post("/api/jobs")
async def enqueue_jobs(
    files: list[UploadFile] = File(...),
    session_id: str = Form(...),
    language: Literal["auto", "ru", "en"] = Form("ru"),
    quality: Literal["fast", "accurate"] = Form("accurate"),
    model: Literal["studio", "large-v3"] = Form("studio"),
    timestamps: Literal["none", "segments", "words"] = Form("segments"),
    formatting: Literal["formatted", "plain"] = Form("formatted"),
    diarization: bool = Form(False),
    diarization_engine: DiarizationEngine | None = Form(None),
    audio_stream_index: int | None = Form(None),
    integration_request_id: str | None = Form(None),
    x_whisper_integration: str | None = Header(default=None),
) -> list[dict]:
    engine = _diarization_engine(diarization, diarization_engine)
    try:
        storage.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc

    source = integrations.by_header(x_whisper_integration)
    # A bot may restart after the upload was accepted but before it received
    # the response. Replaying the same Telegram message must reuse the job.
    if source and integration_request_id:
        if len(files) != 1:
            raise HTTPException(status_code=400, detail="request id requires one file")
        for existing in storage.list_jobs(session_id):
            opts = existing.get("options") or {}
            if (opts.get("integration_source") == source.key and
                    opts.get("integration_request_id") == integration_request_id):
                for upload in files:
                    await upload.close()
                return [existing]
    created = []
    for upload in files:
        safe_name = Path(upload.filename or "audio").name
        suffix = Path(safe_name).suffix.lower()

        job_folder = storage.session_dir(session_id) / "inputs"
        job_folder.mkdir(parents=True, exist_ok=True)
        destination = job_folder / safe_name
        counter = 1
        while destination.exists():
            destination = job_folder / f"{Path(safe_name).stem}-{counter}{suffix}"
            counter += 1

        try:
            with destination.open("wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    target.write(chunk)
            metadata = media.probe_media(destination)
        except RuntimeError as exc:
            if destination.exists():
                destination.unlink()
            raise HTTPException(
                status_code=400,
                detail=f"Не удалось открыть «{safe_name}»: {exc}",
            ) from exc
        finally:
            await upload.close()

        selected_stream = audio_stream_index
        if selected_stream is None:
            selected_stream = int(metadata["audio_streams"][0]["index"])
        track = next(
            (
                stream
                for stream in metadata["audio_streams"]
                if stream["index"] == selected_stream
            ),
            None,
        )
        if track is None:
            destination.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"В «{safe_name}» нет аудиодорожки №{selected_stream}",
            )

        options = {
            "language": language,
            "quality": quality,
            "timestamps": timestamps,
            "formatting": formatting,
            "audio_stream_index": selected_stream,
            "audio_track": track,
            "diarization": diarization,
        }
        if model == "large-v3":
            options["model"] = model
        _with_engine(options, engine)
        # Файл мог прийти не из браузера, а от интеграции: отметка нужна,
        # чтобы в настройках было видно, кто и сколько сюда прислал.
        source = integrations.by_header(x_whisper_integration)
        if source is not None:
            options["integration_source"] = source.key
            if integration_request_id:
                options["integration_request_id"] = integration_request_id
        job = storage.create_job(session_id, safe_name, destination, options)
        created.append(job)
        enqueue_project_job(job["id"])
    return created


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    try:
        return _with_queue_positions([storage.get_job(job_id)])[0]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """Остановить распознавание файла из интерфейса.

    Для задачи в очереди остановка мгновенная. Для выполняющейся задачи
    запрос ставится в силу немедленно (дочерний FFmpeg/Pyannote убивается
    сразу), а сам Whisper завершится на ближайшем готовом сегменте.
    """
    try:
        job_before_cancel = storage.get_job(job_id)
        state = storage.cancel_queued_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    if state in {"completed", "failed", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="Это задание уже не выполняется",
        )
    previous_options = job_before_cancel["options"].get("_previous_options")
    if state == "queued" and isinstance(previous_options, dict):
        # Повторный запуск ещё не успел начаться: сразу возвращаем готовый
        # старый результат вместо состояния «остановлено».
        storage.set_job_options(job_id, previous_options)
        storage.update_job(
            job_id,
            status="completed",
            progress=1.0,
            error="Повторная обработка отменена — сохранён предыдущий результат",
            finished_at=storage.utc_now(),
        )
        cancellation.release(job_id)
    else:
        # Флаг ставим и для гонки, когда воркер забрал обычную задачу между
        # чтением статуса и обновлением.
        cancellation.request(job_id)
    return _with_queue_positions([storage.get_job(job_id)])[0]


@app.post("/api/jobs/{job_id}/prioritize")
def prioritize_job(job_id: str) -> dict:
    try:
        job_state = storage.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    if job_state["status"] != "queued":
        raise HTTPException(
            status_code=409,
            detail="В приоритет можно переместить только ожидающее задание",
        )
    _promote_queued_task(_job_task_kind(job_state), job_id)
    return queue_overview()


@app.post("/api/jobs/{job_id}/requeue")
def requeue_job(job_id: str) -> dict:
    """Вернуть остановленный или неудачный файл в конец очереди."""
    try:
        # Снимаем прошлый запрос на остановку, иначе задача остановилась бы
        # сразу же после повторной постановки.
        cancellation.release(job_id)
        job_state = storage.requeue_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    enqueue_project_job(job_id)
    return _with_queue_positions([job_state])[0]


@app.post("/api/jobs/{job_id}/diarize", status_code=202)
def diarize_completed_job(job_id: str, payload: JobDiarize | None = None) -> dict:
    """Разметить спикеров в готовой транскрибации без повторного Whisper.

    Годится и для повторной разметки другим движком: текст остаётся прежним,
    меняются только метки спикеров.
    """
    engine = _diarization_engine(True, payload.engine if payload else None)
    try:
        job_state = storage.get_job(job_id)
        json_path = job_state["outputs"].get("json")
        if not json_path or not Path(json_path).is_file():
            raise RuntimeError(
                "У готового файла нет сохранённых таймкодов для спикеров"
            )
        cancellation.release(job_id)
        job_state = storage.prepare_job_rerun(
            job_id,
            {"diarization": True, "diarization_engine": engine},
            "diarization",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    enqueue_diarization_job(job_id)
    return _with_queue_positions([job_state])[0]


@app.post("/api/jobs/{job_id}/rerun", status_code=202)
def rerun_completed_job(job_id: str, payload: JobRerun) -> dict:
    """Полностью повторить Whisper с выбранными параметрами."""
    engine = _diarization_engine(payload.diarization, payload.diarization_engine)
    try:
        cancellation.release(job_id)
        job_state = storage.prepare_job_rerun(
            job_id,
            _with_engine(
                {
                    "language": payload.language,
                    "quality": payload.quality,
                    **({"model": payload.model} if payload.model is not None else {}),
                    "diarization": payload.diarization,
                },
                engine,
            ),
            "transcribe",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    enqueue_project_job(job_id)
    return _with_queue_positions([job_state])[0]


@app.get("/api/jobs/{job_id}/text", response_class=PlainTextResponse)
def job_text(
    job_id: str,
    timestamps: Literal["none", "segments", "words"] | None = None,
    formatting: Literal["formatted", "plain"] | None = None,
) -> str:
    try:
        item = storage.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    if timestamps is not None or formatting is not None:
        return exporter.job_text(item, timestamps or item["options"].get("timestamps", "none"),
                                 formatting or item["options"].get("formatting", "formatted"))
    path_value = item["outputs"].get("txt")
    if not path_value or not Path(path_value).exists():
        raise HTTPException(status_code=404, detail="Текст ещё не готов")
    return Path(path_value).read_text(encoding="utf-8-sig")


@app.post("/api/jobs/{job_id}/open-source")
def open_job_source(job_id: str) -> dict[str, bool]:
    try:
        path = _source_path(storage.get_job(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Исходный файл больше не найден по этому пути"
        ) from exc
    os.startfile(str(path))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/reveal-source")
def reveal_job_source(job_id: str) -> dict[str, bool]:
    try:
        path = _source_path(storage.get_job(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Исходный файл больше не найден по этому пути"
        ) from exc
    _reveal_in_explorer(path)
    return {"ok": True}


def _extracted_audio(job_id: str) -> Path:
    try:
        job = storage.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    value = job.get("extracted_audio_path")
    if not value:
        raise HTTPException(
            status_code=404,
            detail="Для этого файла извлечённое аудио не сохранялось",
        )
    path = Path(value)
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Сохранённое аудио больше не найдено по этому пути",
        )
    return path


@app.post("/api/jobs/{job_id}/reveal-audio")
def reveal_job_audio(job_id: str) -> dict[str, bool]:
    _reveal_in_explorer(_extracted_audio(job_id))
    return {"ok": True}


@app.delete("/api/jobs/{job_id}/extracted-audio")
def remove_job_audio(job_id: str) -> dict[str, bool]:
    """Удалить сохранённое аудио, оставив расшифровку.

    Нужно, чтобы освободить место, не удаляя проект: аудио часовой записи
    весит около 115 МБ, а результат распознавания — килобайты.
    """
    _extracted_audio(job_id)
    return {"ok": storage.drop_extracted_audio(job_id)}


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(
    job_id: str,
    kind: str,
    timestamps: Literal["none", "segments", "words"] | None = None,
    formatting: Literal["formatted", "plain"] | None = None,
):
    if kind not in EXPORT_KINDS:
        raise HTTPException(status_code=404, detail="Неизвестный формат")
    try:
        item = storage.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    if kind in {"txt", "docx"} and (timestamps is not None or formatting is not None):
        path_value = str(exporter.create_job_text_export(
            item, kind, timestamps or item["options"].get("timestamps", "none"),
            formatting or item["options"].get("formatting", "formatted")))
    else:
        path_value = item["outputs"].get(kind)
    if not path_value and kind == "docx":
        try:
            path_value = str(exporter.create_job_docx(item))
        except RuntimeError:
            path_value = None
    if not path_value:
        raise HTTPException(status_code=404, detail="Файл ещё не готов")
    path = Path(path_value)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл результата не найден")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.get(
    "/api/projects/{project_id}/combined/text",
    response_class=PlainTextResponse,
)
def project_text(
    project_id: str,
    timestamps: Literal["none", "segments", "words"] = "none",
    formatting: Literal["formatted", "plain"] = "formatted",
    speakers: Literal["off", "on"] = "off",
    speaker_keys: str | None = None,
) -> str:
    try:
        return exporter.combined_text(
            project_id,
            timestamps,
            formatting,
            speakers,
            exporter.parse_speaker_keys(speaker_keys),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/combined/{kind}")
def project_download(
    project_id: str,
    kind: str,
    timestamps: Literal["none", "segments", "words"] = "none",
    formatting: Literal["formatted", "plain"] = "formatted",
    speakers: Literal["off", "on"] = "off",
    speaker_keys: str | None = None,
):
    if kind not in EXPORT_KINDS:
        raise HTTPException(status_code=404, detail="Неизвестный формат")
    try:
        path = exporter.create_project_export(
            project_id,
            kind,
            timestamps,
            formatting,
            speakers,
            exporter.parse_speaker_keys(speaker_keys),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.get("/api/projects/{project_id}/speakers")
def project_speakers(project_id: str) -> list[dict]:
    try:
        return exporter.project_speakers(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Проект не найден") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/speakers/{speaker_id}/sample")
def speaker_sample(job_id: str, speaker_id: str):
    """Образец голоса спикера — несколько его реплик одним MP3."""
    try:
        job_state = storage.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Задание не найдено") from exc
    try:
        path = speaker_samples.build_sample(job_state, speaker_id)
    except (FileNotFoundError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось подготовить прослушивание: {exc}",
        ) from exc
    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.patch(
    "/api/projects/{project_id}/speakers/{job_id}/{speaker_id}"
)
def rename_project_speaker(
    project_id: str,
    job_id: str,
    speaker_id: str,
    payload: SpeakerAliasPatch,
) -> dict:
    try:
        available = {
            item["key"]: item for item in exporter.project_speakers(project_id)
        }
        key = f"{job_id}:{speaker_id}"
        if key not in available:
            raise KeyError(key)
        storage.set_speaker_alias(
            project_id,
            job_id,
            speaker_id,
            payload.name,
        )
        renamed = next(
            item
            for item in exporter.project_speakers(project_id)
            if item["key"] == key
        )
        # Имя спикера попадает в текст расшифровки, поэтому готовые файлы
        # проекта устаревают в тот же момент.
        exporter.refresh_project_files(project_id)
        return renamed
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Спикер или проект не найден",
        ) from exc
