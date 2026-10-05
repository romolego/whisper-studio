"""Образец голоса спикера: несколько его реплик, склеенных в один MP3.

По тексту спикера не всегда понятно, кто это: «СПИКЕР 3» с репликой «да, давайте
так» может оказаться кем угодно. На слух голос узнаётся за пару секунд, поэтому
у карточки спикера есть кнопка прослушивания.

Образец собирается из самых длинных реплик этого спикера: в длинной реплике
голос звучит чисто, без чужих «угу» по краям. Несколько кусков из разных мест
записи лучше одного длинного — если разметка где-то ошиблась, это слышно сразу.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from . import storage
from .media import _tool

# Паузы между словами одного спикера короче этого считаются одной репликой.
RUN_GAP_SECONDS = 1.0
MAX_CLIPS = 4
MAX_CLIP_SECONDS = 8.0
MAX_TOTAL_SECONDS = 30.0
# Реплики короче этого берутся, только если длиннее у спикера ничего нет:
# в полсекунды голос не узнать.
MIN_CLIP_SECONDS = 1.5
LEAD_SECONDS = 0.15
TAIL_SECONDS = 0.3
SILENCE_SECONDS = 0.45
SAMPLE_RATE = 22050

_lock = threading.Lock()


def _speaker_runs(segments: list[dict[str, Any]]) -> list[tuple[float, float, str]]:
    """Непрерывные куски речи каждого спикера по меткам слов."""
    runs: list[list[Any]] = []
    for segment in segments:
        words = segment.get("words") or []
        if words:
            items = [
                (
                    float(word.get("start") or 0),
                    float(word.get("end") or word.get("start") or 0),
                    word.get("speaker") or segment.get("speaker"),
                )
                for word in words
            ]
        else:
            items = [
                (
                    float(segment.get("start") or 0),
                    float(segment.get("end") or segment.get("start") or 0),
                    segment.get("speaker"),
                )
            ]
        for start, end, speaker in items:
            if speaker is None:
                continue
            speaker = str(speaker)
            if (
                runs
                and runs[-1][2] == speaker
                and start - runs[-1][1] <= RUN_GAP_SECONDS
            ):
                runs[-1][1] = max(runs[-1][1], end)
            else:
                runs.append([start, end, speaker])
    return [(start, end, speaker) for start, end, speaker in runs]


def pick_clips(
    segments: list[dict[str, Any]],
    speaker_id: str,
) -> list[tuple[float, float]]:
    """Отрезки для образца на шкале исходной записи, по порядку времени."""
    own = [
        (start, end)
        for start, end, speaker in _speaker_runs(segments)
        if speaker == speaker_id and end > start
    ]
    if not own:
        return []
    own.sort(key=lambda item: item[1] - item[0], reverse=True)
    long_enough = [item for item in own if item[1] - item[0] >= MIN_CLIP_SECONDS]
    candidates = long_enough or own
    chosen: list[tuple[float, float]] = []
    total = 0.0
    for start, end in candidates:
        if len(chosen) >= MAX_CLIPS or total >= MAX_TOTAL_SECONDS:
            break
        length = min(end - start, MAX_CLIP_SECONDS, MAX_TOTAL_SECONDS - total)
        if length <= 0:
            break
        chosen.append((start, start + length))
        total += length
    chosen.sort()
    return chosen


def _audio_source(job: dict[str, Any]) -> tuple[Path, str, float]:
    """Откуда резать звук: файл, дорожка для -map и сдвиг шкалы.

    Сначала сохранённое аудио — ровно то, что слышала разметка, моно 16 кГц,
    перематывается мгновенно. Оно начинается с границы обрезки, поэтому метки
    сдвигаются на неё. Если аудио не сохранялось — исходник и выбранная в нём
    дорожка, там метки совпадают с файлом.
    """
    options = job.get("options") or {}
    kept = job.get("extracted_audio_path")
    if kept and Path(kept).is_file():
        offset = max(0.0, float(options.get("trim_start_seconds") or 0))
        return Path(kept), "a:0", offset
    value = job.get("source_path") or job.get("stored_path")
    if not value or not Path(value).is_file():
        raise FileNotFoundError(
            "Звук для прослушивания не найден: исходный файл перемещён или удалён"
        )
    stream_index = options.get("audio_stream_index")
    stream = "a:0" if stream_index is None else str(int(stream_index))
    return Path(value), stream, 0.0


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:60] or "speaker"


def build_sample(job: dict[str, Any], speaker_id: str) -> Path:
    json_value = (job.get("outputs") or {}).get("json")
    if not json_value or not Path(json_value).is_file():
        raise FileNotFoundError("У файла нет сохранённой разметки")
    payload = json.loads(Path(json_value).read_text(encoding="utf-8"))
    clips = pick_clips(payload.get("segments") or [], speaker_id)
    if not clips:
        raise LookupError("У этого спикера нет реплик с таймкодами")

    source, stream, offset = _audio_source(job)
    root = storage.temporary_dir() / "speaker-samples"
    root.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha1(
        json.dumps(
            [str(source), source.stat().st_mtime, stream, offset, clips]
        ).encode("utf-8")
    ).hexdigest()[:12]
    prefix = f"{job['id']}-{_safe(speaker_id)}-"
    target = root / f"{prefix}{signature}.mp3"
    if target.exists() and target.stat().st_size > 0:
        return target

    with _lock:
        if target.exists() and target.stat().st_size > 0:
            return target
        command = [_tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        filters: list[str] = []
        labels: list[str] = []
        for number, (start, end) in enumerate(clips):
            begin = max(0.0, start - offset - LEAD_SECONDS)
            length = (end - offset + TAIL_SECONDS) - begin
            command += ["-ss", f"{begin:.3f}", "-t", f"{length:.3f}", "-i", str(source)]
            fade = min(0.08, length / 4)
            filters.append(
                f"[{number}:{stream}]aformat=sample_rates={SAMPLE_RATE}"
                ":channel_layouts=mono,"
                f"afade=t=in:d={fade:.3f},"
                f"afade=t=out:st={max(0.0, length - fade):.3f}:d={fade:.3f}"
                f"[c{number}]"
            )
            if labels:
                filters.append(
                    f"anullsrc=r={SAMPLE_RATE}:cl=mono,"
                    f"atrim=duration={SILENCE_SECONDS}[s{number}]"
                )
                labels.append(f"[s{number}]")
            labels.append(f"[c{number}]")
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
        partial = root / f".{prefix}{uuid.uuid4().hex}.mp3"
        command += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "96k",
            "-f",
            "mp3",
            str(partial),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            partial.unlink(missing_ok=True)
            raise RuntimeError("Нарезка образца голоса заняла слишком долго") from exc
        if result.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise RuntimeError(
                result.stderr.strip()[-400:] or "Не удалось вырезать образец голоса"
            )
        # Образцы от прошлой разметки этого же спикера больше не нужны. Файл,
        # который прямо сейчас отдаётся в браузер, Windows удалить не даст —
        # он уйдёт в следующий раз.
        for stale in root.glob(f"{prefix}*.mp3"):
            try:
                stale.unlink()
            except OSError:
                pass
        partial.replace(target)
    return target
