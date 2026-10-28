from __future__ import annotations

import gc
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from faster_whisper import WhisperModel
from huggingface_hub import snapshot_download

from . import (
    cancellation,
    diarization,
    exporter,
    gpu_turns,
    hallucinations,
    longform,
    media,
    paragraphs,
    punctuation,
    runtime,
    remote_client,
    speech_profile,
    storage,
)
from .cancellation import CancelledJob
from .media import extract_audio, trim_bounds


ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
ENGLISH_MODEL_DIR = MODELS_DIR / "turbo"
RUSSIAN_MODEL_DIR = MODELS_DIR / "turbo-russian"
FULL_MODEL_DIR = MODELS_DIR / "large-v3"
RUSSIAN_REPO = "coriollon/whisper-large-v3-turbo-russian"
ENGLISH_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
MODEL_REVISIONS = {
    ENGLISH_REPO: "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    RUSSIAN_REPO: "1158957eaa77976d79e1d2e6083a55826931c37f",
}

_model_lock = threading.Lock()

# Модели держатся загруженными по языкам, а не по одной.
#
# Раньше смена языка выгружала модель и грузила другую. Замер на GTX 1060:
# русская поднимается за 1.84 с (на диске уже int8), английская — за 32.96 с,
# потому что на диске она во float16 и переквантуется в int8 при каждой
# загрузке. Смешанная очередь ru/en платила эти 33 секунды за каждое
# переключение. Кеш платит один раз за запуск сервиса.
#
# Обе модели в видеопамяти занимают 1734 МиБ из 6144 — помещаются с запасом.
# Но пик разметки спикеров 5360 МиБ, и вместе они бы не влезли, поэтому перед
# Pyannote вызывается release_models().
_models: dict[str, WhisperModel] = {}
_model_devices: dict[str, str] = {}


def punctuation_prompt(language: str) -> str:
    """Punctuation context used by Studio and dictation.

    Telegram's speech profile deliberately omits this context: quiet pauses
    in the user's recording echoed the instruction instead of the audio.
    """
    if language == "ru":
        return (
            "Русская устная речь. Оформляй результат как обычный русский "
            "текст: с заглавными буквами, запятыми, точками и вопросительными "
            "знаками."
        )
    if language == "en":
        return (
            "English speech. Format the result as normal English text with "
            "capital letters, commas, periods, and question marks."
        )
    return (
        "Format the transcription as normal readable text with punctuation "
        "and capital letters."
    )


def prepare_model_files(progress: Callable[[str], None] | None = None) -> None:
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    def download_with_resume(
        repo_id: str,
        local_dir: Path,
        patterns: list[str],
        marker: Path,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 61):
            try:
                snapshot_download(
                    repo_id=repo_id,
                    revision=MODEL_REVISIONS[repo_id],
                    allow_patterns=patterns,
                    local_dir=str(local_dir),
                    max_workers=1,
                )
                if marker.exists():
                    return
                raise RuntimeError(f"Не загружен обязательный файл {marker.name}")
            except Exception as exc:
                last_error = exc
                if progress:
                    progress(
                        f"Соединение прервалось, продолжаю загрузку "
                        f"({attempt}/60)…"
                    )
                time.sleep(min(8, 1 + attempt // 5))
        raise RuntimeError(f"Не удалось загрузить модель: {last_error}")

    if progress:
        progress("Загружается английская модель Turbo…")
    if not (ENGLISH_MODEL_DIR / "model.bin").exists():
        download_with_resume(
            ENGLISH_REPO,
            ENGLISH_MODEL_DIR,
            [
                "config.json",
                "preprocessor_config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.*",
            ],
            ENGLISH_MODEL_DIR / "model.bin",
        )

    if progress:
        progress("Загружается русская модель Turbo…")
    if not (RUSSIAN_MODEL_DIR / "model.bin").exists():
        download_root = MODELS_DIR / "_russian_download"
        download_with_resume(
            RUSSIAN_REPO,
            download_root,
            ["ct2_int8_float16/*"],
            download_root / "ct2_int8_float16" / "model.bin",
        )
        source = download_root / "ct2_int8_float16"
        RUSSIAN_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            target = RUSSIAN_MODEL_DIR / item.name
            if not target.exists():
                item.replace(target)


# Сколько потоков отдавать CTranslate2 на процессоре.
#
# Замер на этой машине (Xeon E3-1240 v2, 4 ядра + HT, turbo-russian, int8):
# 4 потока — 0.83x реального времени, 8 потоков — 0.76x. Гиперпоточность здесь
# не помогает, а мешает: два потока на ядро дерутся за один блок умножения.
# Поэтому целимся в физические ядра, а не в логические.
#
# Значение важно именно при переносе на другую машину: 4 было верно для
# четырёхъядерника и стало бы потерей половины мощности на восьмиядерном
# ноутбуке.
_cpu_threads_cache: int | None = None


def _physical_cores() -> int | None:
    """Число физических ядер или None, если узнать не удалось."""
    try:
        import subprocess

        probe = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor |"
                " Measure-Object -Property NumberOfCores -Sum).Sum",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    text = (probe.stdout or "").strip().splitlines()
    if not text:
        return None
    try:
        cores = int(text[-1].strip())
    except ValueError:
        return None
    return cores if cores > 0 else None


def cpu_threads() -> int:
    """Потоки для CPU-расчёта: явная настройка, физические ядра, оценка."""
    global _cpu_threads_cache
    if _cpu_threads_cache is not None:
        return _cpu_threads_cache

    override = (os.environ.get("WHISPER_CPU_THREADS") or "").strip()
    if override.isdigit() and int(override) > 0:
        _cpu_threads_cache = int(override)
        return _cpu_threads_cache

    cores = _physical_cores()
    if cores is None:
        # Запасной путь: у процессоров с SMT логических вдвое больше
        # физических, и это верно для подавляющего большинства машин, на
        # которых сервис вообще имеет смысл запускать.
        logical = os.cpu_count() or 4
        cores = max(1, logical // 2)
    _cpu_threads_cache = max(1, cores)
    return _cpu_threads_cache


def _load_model(language: str) -> tuple[WhisperModel, str]:
    remote = remote_client.selected()
    if remote:
        return remote_client.RemoteModel(language, remote), "remote"
    model_key = "large-v3" if language == "large-v3" else ("ru" if language == "ru" else "turbo")
    model_path = FULL_MODEL_DIR if model_key == "large-v3" else (RUSSIAN_MODEL_DIR if model_key == "ru" else ENGLISH_MODEL_DIR)

    with _model_lock:
        cached = _models.get(model_key)
        if cached is not None:
            return cached, _model_devices[model_key]

        if not (model_path / "model.bin").exists():
            if model_key == "large-v3":
                raise RuntimeError("Полная модель large-v3 не установлена. Установите её в models/large-v3 перед выбором этого режима.")
            prepare_model_files()

        preferred = runtime.preferred_device()
        model = None
        if preferred != "cpu":
            try:
                model = WhisperModel(
                    str(model_path),
                    device="cuda",
                    compute_type="float16" if model_key == "large-v3" else "int8_float32",
                    cpu_threads=cpu_threads(),
                    num_workers=1,
                )
                device = "cuda"
            except Exception:
                if preferred == "cuda":
                    raise
        if model is None:
            model = WhisperModel(
                str(model_path),
                device="cpu",
                compute_type="int8",
                cpu_threads=cpu_threads(),
                num_workers=1,
            )
            device = "cpu"

        _models[model_key] = model
        _model_devices[model_key] = device
        return model, device


def release_models() -> int:
    """Выгрузить модели Whisper и освободить видеопамять.

    Вызывается перед разметкой спикеров: Pyannote пикует на 5360 МиБ из 6144,
    и держать рядом с ним модели Whisper (889 МиБ русская, 805 английская)
    нельзя. Заодно это снимает риск, замеченный ещё в прошлом аудите: раньше
    модель оставалась в памяти на всё время разметки, и на многочасовой записи
    Pyannote мог не поместиться.

    Возвращает, сколько моделей выгружено. Задача, которая держит свою ссылку
    на модель (например незавершённый генератор), не пострадает: объект живёт,
    пока на него ссылаются, и уйдёт из памяти сам.
    """
    with _model_lock:
        released = len(_models)
        _models.clear()
        _model_devices.clear()
    gc.collect()
    return released


def loaded_model_keys() -> list[str]:
    """Какие модели сейчас в памяти — для диагностики и тестов."""
    with _model_lock:
        return sorted(_models)


def _plain_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _format_clock(seconds: float, decimal: str = ",") -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{decimal}{millis:03}"


def _write_outputs(
    job: dict[str, Any],
    segments: list[dict[str, Any]],
    info: dict[str, Any],
) -> None:
    session_path = storage.session_dir(job["session_id"])
    output_dir = session_path / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    # Файлы результата называются как сама запись и лежат в одной папке. Раньше
    # у каждого задания была своя подпапка с его идентификатором — в проводнике
    # это выглядело как десяток папок с шестнадцатеричными именами.
    stem = storage.job_file_base(job)
    formatting = job["options"]["formatting"]
    timestamp_mode = job["options"]["timestamps"]

    display_segments = []
    for segment in segments:
        item = dict(segment)
        item["words"] = [
            dict(word) for word in (segment.get("words") or [])
        ]
        if formatting == "plain":
            item["text"] = _plain_text(item["text"])
            for word in item.get("words") or []:
                word["word"] = _plain_text(word["word"])
        display_segments.append(item)

    # Текст собирается тем же кодом, что и просмотр: сегменты Whisper
    # склеиваются в абзацы, а не выкладываются лесенкой по пять слов. Разметка
    # спикеров включается сама, если она в результате есть, — иначе готовый
    # файл потерял бы то, ради чего эту разметку считали.
    speaker_mode = (
        "on"
        if any(paragraphs.segment_speaker(item) is not None for item in segments)
        else "off"
    )
    aliases = storage.list_speaker_aliases(job["session_id"])
    txt = exporter.render_segments(
        segments,
        timestamp_mode,
        formatting,
        speaker_mode,
        job["id"],
        aliases,
    )

    txt_path = output_dir / f"{stem}.txt"
    txt_path.write_text(
        txt + "\n",
        encoding="utf-8-sig" if job["options"].get("integration_source") == "telegram" else "utf-8",
    )
    storage.add_output(job["id"], "txt", txt_path)

    md_path = output_dir / f"{stem}.md"
    md_path.write_text(
        f"# {stem}\n\n"
        + exporter.render_segments(
            segments,
            timestamp_mode,
            formatting,
            speaker_mode,
            job["id"],
            aliases,
            markup="markdown",
        )
        + "\n",
        encoding="utf-8",
    )
    storage.add_output(job["id"], "md", md_path)

    docx_path = output_dir / f"{stem}.docx"
    exporter.write_docx(stem, txt, docx_path)
    storage.add_output(job["id"], "docx", docx_path)

    srt_blocks = []
    for index, item in enumerate(display_segments, start=1):
        srt_blocks.append(
            f"{index}\n{_format_clock(item['start'])} --> {_format_clock(item['end'])}\n"
            f"{item['text'].strip()}"
        )
    srt_path = output_dir / f"{stem}.srt"
    srt_path.write_text("\n\n".join(srt_blocks) + "\n", encoding="utf-8")
    storage.add_output(job["id"], "srt", srt_path)

    vtt_blocks = ["WEBVTT"]
    for item in display_segments:
        vtt_blocks.append(
            f"{_format_clock(item['start'], '.')} --> {_format_clock(item['end'], '.')}\n"
            f"{item['text'].strip()}"
        )
    vtt_path = output_dir / f"{stem}.vtt"
    vtt_path.write_text("\n\n".join(vtt_blocks) + "\n", encoding="utf-8")
    storage.add_output(job["id"], "vtt", vtt_path)

    json_path = output_dir / f"{stem}.json"
    json_path.write_text(
        json.dumps(
            {
                "source": job["original_name"],
                "options": job["options"],
                "language": info["language"],
                "language_probability": info["language_probability"],
                "duration_seconds": info["duration"],
                # На чём считал Whisper. Раньше значение вычислялось и
                # выбрасывалось: молчаливый откат на процессор — это
                # двадцатикратное замедление, и видно его не было нигде.
                "device": info.get("device"),
                "transcription_profile": info.get("transcription_profile"),
                "model": info.get("model", "studio"),
                "beam_size": info.get("beam_size"),
                "punctuation_model": info.get("punctuation_model"),
                # Снятые галлюцинации не выбрасываются, а лежат рядом с
                # результатом: всегда можно проверить, что именно убрали и
                # почему. В сам текст и в segments они уже не попадают.
                "removed_hallucinations": info.get("removed_hallucinations")
                or [],
                # JSON всегда хранит исходный текст Whisper. Благодаря этому
                # просмотр и экспорт можно переключать уже после распознавания.
                "segments": segments,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    storage.add_output(job["id"], "json", json_path)


def _clip_duration(source_path: Path, options: dict[str, Any]) -> float:
    """Длительность куска, который реально пойдёт в распознавание.

    Нужна заранее, до первого вызова модели: по ней планируются окна. Один вызов
    ffprobe дешёв даже на многочасовом файле, потому что читается контейнер, а не
    поток. Если длительность узнать не удалось, возвращается 0 — тогда файл идёт
    прежним путём, одним проходом.
    """
    try:
        probed = float(media.probe_media(source_path)["duration_seconds"] or 0)
    except (RuntimeError, KeyError, OSError, ValueError):
        return 0.0
    if probed > 0:
        return probed
    return 0.0


def _collect_segments(
    generator: Any,
    job_id: str,
    total_duration: float,
    time_shift: float,
    window_start: float,
    word_timestamps: bool,
) -> list[dict[str, Any]]:
    """Собрать сегменты одного прохода, сдвинув их на шкалу всего куска.

    time_shift — где начинается прочитанный клип на шкале куска (с учётом
    разгона перед окном). window_start — граница окна, по которой решается, чьи
    сегменты в разгоне.
    """
    collected: list[dict[str, Any]] = []
    last_update = 0.0
    total = max(float(total_duration), 0.001)
    for segment in generator:
        # Единственная точка, где можно прервать распознавание: сам вызов
        # model.transcribe отдаёт сегменты лениво, поэтому проверка здесь
        # останавливает работу на первом же готовом сегменте. По той же причине
        # здесь стоит и чекпоинт видеокарты: пока цикл ждёт, GPU не занят, и
        # диктовка получает своё за один сегмент вместо целого файла.
        gpu_turns.checkpoint()
        cancellation.check(job_id)
        start = float(segment.start) + time_shift
        end = float(segment.end) + time_shift
        if not longform.owns(start, end, window_start):
            continue
        words = None
        if word_timestamps and segment.words:
            words = [
                {
                    "start": word.start + time_shift,
                    "end": word.end + time_shift,
                    "word": word.word,
                    "probability": word.probability,
                }
                for word in segment.words
            ]
        collected.append(
            {"start": start, "end": end, "text": segment.text, "words": words}
        )
        current = min(0.98, max(0.02, end / total))
        if current - last_update >= 0.015:
            storage.update_job(job_id, progress=current)
            last_update = current
    return collected


def _trim_offset(options: dict[str, Any]) -> float:
    """Сколько секунд отрезано от начала: на столько сдвинуты все метки."""
    return max(0.0, float(options.get("trim_start_seconds") or 0))


def _shift_segments(
    segments: list[dict[str, Any]],
    offset: float,
) -> list[dict[str, Any]]:
    """Перенести метки фрагмента на шкалу исходного файла.

    Whisper считает время от начала обрезанного куска. Пользователь же ищет
    место в исходной записи, поэтому в результат уходят абсолютные метки.
    """
    if not offset:
        return segments
    for segment in segments:
        segment["start"] = float(segment.get("start") or 0) + offset
        segment["end"] = float(segment.get("end") or 0) + offset
        for word in segment.get("words") or []:
            if word.get("start") is not None:
                word["start"] = float(word["start"]) + offset
            if word.get("end") is not None:
                word["end"] = float(word["end"]) + offset
    return segments


def _diarization_engine(options: dict[str, Any]) -> str:
    """Движок разметки из опций файла.

    Файлы, поставленные до появления выбора, движка в опциях не имеют — их
    размечал Pyannote, и повторная разметка по ним остаётся на нём же.
    """
    engine = options.get("diarization_engine")
    return engine if engine in diarization.ENGINES else "pyannote"


def _clean_rerun_options(options: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in options.items()
        if key not in {"_rerun_mode", "_previous_options"}
    }


def _restore_previous_result(
    job_id: str,
    options: dict[str, Any],
    message: str,
) -> bool:
    previous = options.get("_previous_options")
    if not isinstance(previous, dict):
        return False
    storage.set_job_options(job_id, previous)
    storage.update_job(
        job_id,
        status="completed",
        progress=1.0,
        error=message,
        finished_at=storage.utc_now(),
    )
    return True


@remote_client.task_scope
def transcribe_job(job_id: str) -> None:
    job = storage.get_job(job_id)
    options = job["options"]
    language = options["language"]
    started = time.perf_counter()
    prepared_path: Path | None = None
    keep_audio = False
    storage.update_job(
        job_id,
        status="processing",
        progress=0.01,
        started_at=storage.utc_now(),
        error=None,
    )

    try:
        cancellation.check(job_id)
        source_path = Path(job["stored_path"])
        trim_offset = _trim_offset(options)
        audio_stream_index = options.get("audio_stream_index")
        if audio_stream_index is not None:
            # Извлечённое аудио — ровно то, что уходит в Whisper. По настройке
            # оно либо остаётся в проекте, либо живёт во временной папке и
            # удаляется в finally, как было раньше.
            keep_audio = storage.keep_extracted_audio()
            prepared_path = (
                storage.job_audio_path(job)
                if keep_audio
                else storage.temporary_dir() / f"{job_id}.wav"
            )
            extract_audio(
                source_path,
                int(audio_stream_index),
                prepared_path,
                job_id,
                start_seconds=trim_offset,
                end_seconds=options.get("trim_end_seconds"),
            )
            source_path = prepared_path
            # Путь записывается сразу после извлечения, а не после успешного
            # распознавания: если распознавание упадёт или его остановят, аудио
            # всё равно уже лежит на диске и должно быть видно в интерфейсе.
            storage.set_extracted_audio(job_id, prepared_path if keep_audio else None)
        elif trim_offset:
            # Обрезка возможна только после извлечения дорожки: без него
            # Whisper читает файл целиком, и смещение меток было бы ложью.
            trim_offset = 0.0

        cancellation.check(job_id)
        full_accuracy = options.get("model") == "large-v3" and options["quality"] == "accurate"
        beam_size = 10 if full_accuracy else (1 if options["quality"] == "fast" else 5)
        model_selector = "large-v3" if options.get("model") == "large-v3" else language
        # Храним метки слов всегда: пользователь может менять детализацию
        # просмотра и экспорта без повторного распознавания.
        word_timestamps = True
        forced_language = None if language == "auto" else language
        decode_options: dict[str, Any] = {
            "language": forced_language,
            "task": "transcribe",
            "beam_size": beam_size,
            "best_of": beam_size,
            # Telegram is a speech workflow. Detect speech there so pauses and
            # room noise are not turned into invented sentences. Keep the full
            # audio path for Studio recordings (including songs/quiet vocals).
            "vad_filter": options.get("integration_source") == "telegram",
            "word_timestamps": word_timestamps,
            # Telegram omits synthetic text context: repeating a punctuation
            # instruction in every window made the recording echo it.
            "condition_on_previous_text": False,
            # Повтор фразы может быть частью записи (например, припевом).
            # Зацикливание ограничивается сбросом контекста и проверкой
            # compression_ratio, без запрета настоящих повторов.
            "repetition_penalty": 1.0,
            "no_repeat_ngram_size": 0,
            "compression_ratio_threshold": 2.2,
            "log_prob_threshold": -0.8,
            "no_speech_threshold": 0.65,
        }
        if full_accuracy:
            decode_options.update(patience=2.0, temperature=0.0)
        if decode_options["vad_filter"]:
            decode_options["vad_parameters"] = {
                "threshold": 0.2 if full_accuracy else 0.35,
                "min_silence_duration_ms": 700 if full_accuracy else 500,
                "speech_pad_ms": 600 if full_accuracy else 400,
            }
        elif not full_accuracy:
            # Preserve Studio's existing punctuation behavior. The Telegram
            # speech profile has no hotwords or initial prompt.
            decode_options["hotwords"] = punctuation_prompt(language)

        clip_duration = _clip_duration(source_path, options)
        windows = longform.plan(clip_duration)
        signature = longform.signature(source_path, options)
        items: list[dict[str, Any]] = []
        device = "cpu"
        detected_language = forced_language or "unknown"
        language_probability = 1.0

        if not windows:
            # Файл короткий — прежний путь, одним проходом, без лишних вызовов
            # ffmpeg и без чекпоинтов: памяти на такой файл хватает с запасом.
            with gpu_turns.batch():
                model, device = _load_model(model_selector)
                if isinstance(model, remote_client.RemoteModel) and not full_accuracy:
                    model.progress = lambda value: storage.update_job(job_id, progress=0.02 + 0.95 * value)
                if full_accuracy:
                    generator, info = speech_profile.transcribe(model, source_path, decode_options, job_id)
                else:
                    generator, info = model.transcribe(str(source_path), **decode_options)
                detected_language = info.language
                language_probability = info.language_probability
                clip_duration = max(float(info.duration), clip_duration, 0.001)
                items = _collect_segments(
                    generator, job_id, clip_duration, 0.0, 0.0, word_timestamps
                )
                device = getattr(model, "remote_device", device)
        else:
            # Длинный файл: окна по очереди. Пик памяти держится на уровне одного
            # окна, а после каждого окна результат уходит на диск, поэтому сбой
            # питания стоит не больше одного окна работы.
            done_until, items = longform.load(job, signature)
            if done_until > 0:
                storage.update_job(
                    job_id,
                    error=None,
                    progress=min(0.98, max(0.02, done_until / clip_duration)),
                )
            for window_start, window_end in windows:
                if window_end <= done_until:
                    continue
                cancellation.check(job_id)
                lead = min(longform.WINDOW_OVERLAP, window_start)
                window_path = storage.temporary_dir() / (
                    f"{job_id}-w{int(window_start)}.wav"
                )
                try:
                    extract_audio(
                        source_path,
                        options.get("audio_stream_index")
                        if source_path != prepared_path
                        else None,
                        window_path,
                        job_id,
                        start_seconds=window_start - lead,
                        end_seconds=window_end,
                    )
                    with gpu_turns.batch():
                        model, device = _load_model(model_selector)
                        if isinstance(model, remote_client.RemoteModel) and not full_accuracy:
                            model.progress = lambda value: storage.update_job(
                                job_id, progress=min(0.98, max(0.02, (
                                    window_start - lead + value * (window_end - window_start + lead)
                                ) / clip_duration)))
                        if full_accuracy:
                            generator, info = speech_profile.transcribe(model, window_path, decode_options, job_id)
                        else:
                            generator, info = model.transcribe(str(window_path), **decode_options)
                        if window_start <= 0:
                            detected_language = info.language
                            language_probability = info.language_probability
                        items += _collect_segments(
                            generator,
                            job_id,
                            clip_duration,
                            window_start - lead,
                            window_start,
                            word_timestamps,
                        )
                        device = getattr(model, "remote_device", device)
                finally:
                    window_path.unlink(missing_ok=True)
                longform.save(job, signature, window_end, items)

        cancellation.check(job_id)
        items = _shift_segments(items, trim_offset)
        # Мусор снимается до разметки спикеров: иначе Pyannote тратит время на
        # фразы, которых в записи не было, и они получают своего «спикера».
        items, dropped = hallucinations.clean_segments(
            items, prompt=decode_options.get("hotwords", ""),
            is_silent=lambda segment: media.is_digital_silence(
                source_path, float(segment.get("start") or 0)-trim_offset,
                float(segment.get("end") or 0)-trim_offset,
                options.get("audio_stream_index") if source_path != prepared_path else None,
            ),
        )
        if full_accuracy and detected_language == "ru" and options["formatting"] == "formatted":
            storage.update_job(job_id, progress=0.98)
            items = punctuation.restore(items, job_id)
        if options.get("diarization"):
            storage.update_job(job_id, progress=0.985)
            with gpu_turns.batch():
                # Pyannote пикует на 5360 МиБ из 6144, поэтому модели Whisper
                # уходят из памяти на время разметки. Освобождение под слотом
                # батча: так диктовка не начнёт грузить модель обратно прямо
                # сейчас. Обратная загрузка стоит 1.84 с для русской модели.
                release_models()
                items = diarization.assign_speakers(
                    source_path,
                    items,
                    job_id,
                    time_offset=trim_offset,
                    engine=_diarization_engine(options),
                )

        # Язык и длительность берутся из собранных значений, а не из info
        # последнего окна: на длинном файле info существует по одному на окно, и
        # у последнего окна язык определялся бы по его последним минутам.
        metadata = {
            "language": detected_language,
            "language_probability": language_probability,
            "duration": clip_duration,
            "device": device,
            "transcription_profile": longform.profile(options),
            "model": options.get("model", "studio"),
            "beam_size": beam_size,
            "punctuation_model": punctuation.MODEL_REPO if full_accuracy and detected_language == "ru" and options["formatting"] == "formatted" else None,
            "removed_hallucinations": dropped,
        }
        clean_options = _clean_rerun_options(options)
        output_job = {**job, "options": clean_options}
        _write_outputs(output_job, items, metadata)
        storage.set_job_options(job_id, clean_options)
        if options.get("_rerun_mode"):
            storage.clear_speaker_aliases(job_id)
        elapsed = time.perf_counter() - started
        storage.update_job(
            job_id,
            device=device,
            status="completed",
            progress=1.0,
            detected_language=detected_language,
            duration_seconds=clip_duration,
            processing_seconds=elapsed,
            finished_at=storage.utc_now(),
        )
        # Файл дошёл до конца — продолжать больше нечего.
        longform.clear(job)
        # Сначала папка догоняет название проекта (пока файл считался, переезд
        # был запрещён), и только потом в неё пишутся готовые файлы.
        storage.sync_session_dir(job["session_id"])
        exporter.refresh_project_files(job["session_id"])
    except CancelledJob:
        # Остановка по просьбе пользователя: результат не пишем, но исходник и
        # уже готовые выводы прошлых прогонов не трогаем.
        if not _restore_previous_result(
            job_id,
            options,
            "Повторная обработка остановлена — сохранён предыдущий результат",
        ):
            storage.update_job(
                job_id,
                status="cancelled",
                progress=0,
                processing_seconds=time.perf_counter() - started,
                error=None,
                finished_at=storage.utc_now(),
            )
    except Exception as exc:
        if not _restore_previous_result(
            job_id,
            options,
            f"Повторная обработка не удалась: {exc}",
        ):
            storage.update_job(
                job_id,
                status="failed",
                progress=0,
                processing_seconds=time.perf_counter() - started,
                error=str(exc),
                finished_at=storage.utc_now(),
            )
    finally:
        # Сохранённое аудио — часть результата проекта, его удалять нельзя.
        if not keep_audio and prepared_path and prepared_path.exists():
            prepared_path.unlink()


@remote_client.task_scope
def diarize_existing_job(job_id: str) -> None:
    job = storage.get_job(job_id)
    options = job["options"]
    started = time.perf_counter()
    prepared_path: Path | None = None
    storage.update_job(
        job_id,
        status="processing",
        progress=0.02,
        started_at=storage.utc_now(),
        error=None,
    )
    try:
        cancellation.check(job_id)
        json_value = job["outputs"].get("json")
        if not json_value or not Path(json_value).is_file():
            raise RuntimeError(
                "В результате нет таймкодов для разметки спикеров"
            )
        payload = json.loads(Path(json_value).read_text(encoding="utf-8"))
        segments = payload.get("segments")
        if not isinstance(segments, list) or not segments:
            raise RuntimeError("В транскрибации нет фрагментов")
        # Разметка спикеров по старому результату — заодно и повод вычистить
        # мусор, записанный до появления фильтра.
        segments, dropped = hallucinations.clean_segments(segments)

        source_path = Path(job["stored_path"])
        trim_offset = _trim_offset(options)
        audio_stream_index = options.get("audio_stream_index")
        if audio_stream_index is not None:
            kept = job.get("extracted_audio_path")
            if kept and Path(kept).is_file():
                # Сохранённое аудио — это уже нужная дорожка, обрезанная по
                # выбранным границам. Повторное извлечение на многочасовом видео
                # стоит минуты ffmpeg и дало бы ровно тот же файл.
                source_path = Path(kept)
            else:
                prepared_path = (
                    storage.temporary_dir() / f"{job_id}-speakers.wav"
                )
                extract_audio(
                    source_path,
                    int(audio_stream_index),
                    prepared_path,
                    job_id,
                    start_seconds=trim_offset,
                    end_seconds=options.get("trim_end_seconds"),
                )
                source_path = prepared_path
        else:
            trim_offset = 0.0

        cancellation.check(job_id)
        storage.update_job(job_id, progress=0.1)
        with gpu_turns.batch():
            release_models()
            segments = diarization.assign_speakers(
                source_path,
                segments,
                job_id,
                time_offset=trim_offset,
                engine=_diarization_engine(options),
            )
        clean_options = {
            **_clean_rerun_options(options),
            "diarization": True,
        }
        output_job = {**job, "options": clean_options}
        metadata = {
            "language": payload.get("language") or job.get(
                "detected_language"
            ) or "unknown",
            "language_probability": payload.get(
                "language_probability"
            ) or 0,
            "duration": payload.get("duration_seconds")
            or job.get("duration_seconds")
            or 0,
            "transcription_profile": payload.get("transcription_profile"),
            "removed_hallucinations": (
                (payload.get("removed_hallucinations") or []) + dropped
            ),
        }
        _write_outputs(output_job, segments, metadata)
        storage.set_job_options(job_id, clean_options)
        storage.clear_speaker_aliases(job_id)
        storage.update_job(
            job_id,
            status="completed",
            progress=1.0,
            processing_seconds=time.perf_counter() - started,
            error=None,
            finished_at=storage.utc_now(),
        )
        storage.sync_session_dir(job["session_id"])
        exporter.refresh_project_files(job["session_id"])
    except CancelledJob:
        _restore_previous_result(
            job_id,
            options,
            "Разметка спикеров остановлена — сохранён предыдущий результат",
        )
    except Exception as exc:
        _restore_previous_result(
            job_id,
            options,
            f"Не удалось разметить спикеров: {exc}",
        )
    finally:
        if prepared_path and prepared_path.exists():
            prepared_path.unlink()
