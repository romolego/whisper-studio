"""Длинные записи: обработка окнами и сохранение промежуточного результата.

Две проблемы, которые это решает. Обе измерены, а не предположены.

**Память.** faster-whisper загружает файл в память целиком: сначала весь звук во
float32, потом копию только речи после VAD, потом мел-спектрограмму — тоже сразу
по всему файлу. Замер на плотной речи (реальная запись, зацикленная до двух
часов) дал пик 6818 МБ на 2 часа, то есть **около 3.4 ГБ на час аудио**, причём
скачок даёт именно расчёт мел-спектрограммы. На 32 ГБ это упирается в потолок
где-то на 8–9 часах плотной речи; ни 50, ни 100 часов таким способом не
обработать. Окно фиксированной длины держит пик постоянным независимо от общей
длительности файла.

**Потеря работы.** При старте сервис переводит все задачи из `processing` в
`queued` (storage.init_db) — то есть после сбоя питания файл начинался заново. На
суточной расшифровке это означало потерю всей проделанной работы. Теперь после
каждого окна результат дописывается на диск, и повторный запуск продолжает с
первого недоделанного окна.

Короткие файлы идут прежним путём — одним проходом, без лишних вызовов ffmpeg.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import storage


# Окно 30 минут: при измеренных 3.4 ГБ на час пик держится около 1.7 ГБ, что
# помещается даже на машине с 8 ГБ памяти вместе с моделью.
WINDOW_SECONDS = 1800.0

# Разгон перед окном. Модель получает акустический контекст до своей границы,
# поэтому слово на стыке распознаётся целиком, а не рвётся пополам. Владелец
# сегмента определяется по его середине, так что этот разгон ничего не дублирует.
WINDOW_OVERLAP = 12.0

# Ниже этого порога файл обрабатывается как раньше — одним проходом. Смысл: не
# платить лишними вызовами ffmpeg и не менять поведение там, где памяти хватает с
# запасом. 45 минут плотной речи — это около 2.5 ГБ пика.
WINDOW_THRESHOLD = 2700.0

# Служебные файлы прогресса: по одному на задание, скрытой папкой в outputs.
CHECKPOINT_DIR_NAME = ".checkpoints"

# Старые окна с VAD уже потеряли часть звука. После смены профиля их нельзя
# подмешивать к новым: незавершённое задание должно распознаваться заново.
TRANSCRIPTION_PROFILE = "full-audio-v1"


def plan(duration_seconds: float) -> list[tuple[float, float]]:
    """Окна [начало, конец) на шкале обрабатываемого куска.

    Пустой список означает «обрабатывать целиком», как раньше.
    """
    total = float(duration_seconds or 0)
    if total <= 0 or total <= WINDOW_THRESHOLD:
        return []
    windows: list[tuple[float, float]] = []
    start = 0.0
    while start < total:
        end = min(total, start + WINDOW_SECONDS)
        windows.append((start, end))
        # Последний огрызок короче четверти окна прицепляем к предыдущему: иначе
        # на файле в 30 минут и 40 секунд появилось бы окно на 40 секунд.
        if total - end < WINDOW_SECONDS / 4 and end < total:
            windows[-1] = (start, total)
            break
        start = end
    return windows


def signature(source: Path, options: dict[str, Any]) -> str:
    """Отпечаток исходника и настроек распознавания.

    Нужен, чтобы не продолжить старый чекпоинт после смены файла, языка,
    качества, дорожки или границ обрезки: результат тогда был бы смесью двух
    разных прогонов.
    """
    try:
        stat = source.stat()
        file_part = f"{source.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        file_part = source.name
    relevant = (
        options.get("language"),
        options.get("quality"),
        options.get("audio_stream_index"),
        options.get("trim_start_seconds"),
        options.get("trim_end_seconds"),
    )
    return f"{TRANSCRIPTION_PROFILE}|{file_part}|{relevant}"


def _path(job: dict[str, Any]) -> Path:
    # Чекпоинт — служебный файл, а не результат: он лежит в скрытой папке и
    # исчезает, когда файл дочитан до конца. Рядом с расшифровками, которые
    # человек открывает руками, ему делать нечего.
    return (
        storage.session_dir(job["session_id"])
        / "outputs"
        / CHECKPOINT_DIR_NAME
        / f"{job['id']}.json"
    )


def load(job: dict[str, Any], expected: str) -> tuple[float, list[dict[str, Any]]]:
    """Сколько уже сделано: (начало первого недоделанного окна, сегменты).

    При несовпадении отпечатка или битом файле возвращает начало с нуля —
    продолжать сомнительный чекпоинт хуже, чем посчитать заново.
    """
    path = _path(job)
    if not path.is_file():
        return 0.0, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0, []
    if payload.get("signature") != expected:
        return 0.0, []
    segments = payload.get("segments")
    if not isinstance(segments, list):
        return 0.0, []
    try:
        done_until = float(payload.get("done_until") or 0)
    except (TypeError, ValueError):
        return 0.0, []
    return max(0.0, done_until), segments


def save(
    job: dict[str, Any],
    expected: str,
    done_until: float,
    segments: list[dict[str, Any]],
) -> None:
    """Дописать результат после очередного окна.

    Запись идёт во временный файл и переименованием: обрыв питания посередине
    записи не должен превратить чекпоинт в мусор и обнулить всю работу.
    """
    path = _path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    temporary.write_text(
        json.dumps(
            {
                "signature": expected,
                "done_until": done_until,
                "segments": segments,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def clear(job: dict[str, Any]) -> None:
    """Убрать чекпоинт: файл дошёл до конца, продолжать больше нечего."""
    path = _path(job)
    try:
        path.unlink(missing_ok=True)
        path.with_suffix(".part").unlink(missing_ok=True)
    except OSError:
        pass


def owns(segment_start: float, segment_end: float, window_start: float) -> bool:
    """Принадлежит ли сегмент этому окну.

    Владелец определяется серединой сегмента. Из-за разгона перед окном модель
    видит и речь предыдущего окна; её сегменты имеют середину раньше границы и
    отбрасываются, потому что предыдущее окно их уже записало.
    """
    if window_start <= 0:
        return True
    middle = (float(segment_start) + float(segment_end)) / 2
    return middle >= window_start
