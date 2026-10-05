from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import uuid
from array import array
from pathlib import Path
from typing import Any

from . import cancellation, storage


_preview_lock = threading.Lock()
_waveform_lock = threading.Lock()

# Огибающая строится по монодорожке 8 кГц: этого хватает и для шёпота, и для
# шипящих, а декодирование часа записи занимает секунды. Сначала копим пик на
# каждые 50 мс и лишь потом сжимаем до нужного числа столбиков — так итог не
# зависит от того, совпала ли длительность из ffprobe с реальной дорожкой.
_WAVEFORM_RATE = 8000
_WAVEFORM_SLOT_SAMPLES = 400
_WAVEFORM_BARS = 420

# Дорожка для прослушивания в редакторе границ. Моно 22 кГц хватает, чтобы на
# слух поймать начало и конец речи, а час записи весит около 28 МБ и отдаётся
# по диапазонам, поэтому перемотка мгновенная.
_PLAYBACK_RATE = 22050
_PLAYBACK_BITRATE = "64k"


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(
            f"{name} не найден. Переустановите FFmpeg или добавьте его в PATH."
        )
    return path


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        _tool("ffprobe"),
        "-v",
        "error",
        "-show_entries",
        (
            "format=format_name,duration,size:"
            "stream=index,codec_type,codec_name,channels,channel_layout,"
            "sample_rate,bit_rate:stream_tags=language,title"
        ),
        "-of",
        "json",
        str(path),
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
        detail = result.stderr.strip() or "FFmpeg не смог открыть файл"
        raise RuntimeError(detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFmpeg вернул некорректные данные о файле") from exc

    audio_streams = []
    video_streams = []
    for stream in payload.get("streams", []):
        tags = stream.get("tags") or {}
        item = {
            "index": int(stream["index"]),
            "codec": stream.get("codec_name") or "unknown",
            "channels": int(stream.get("channels") or 0),
            "channel_layout": stream.get("channel_layout") or "",
            "sample_rate": int(stream.get("sample_rate") or 0),
            "bit_rate": int(stream.get("bit_rate") or 0),
            "language": tags.get("language") or "",
            "title": tags.get("title") or "",
        }
        if stream.get("codec_type") == "audio":
            audio_streams.append(item)
        elif stream.get("codec_type") == "video":
            video_streams.append(item)

    if not audio_streams:
        raise RuntimeError("В файле не найдена аудиодорожка")

    format_info = payload.get("format") or {}
    return {
        "format_name": format_info.get("format_name") or path.suffix.lstrip("."),
        "duration_seconds": float(format_info.get("duration") or 0),
        "size_bytes": int(format_info.get("size") or path.stat().st_size),
        "audio_streams": audio_streams,
        "video_stream_count": len(video_streams),
    }


def extract_audio(
    source: Path,
    stream_index: int | None,
    target: Path,
    task_id: str | None = None,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> None:
    """Извлечь одну аудиодорожку в 16 кГц моно WAV.

    task_id позволяет прервать извлечение: на длинной записи этот этап идёт
    минутами, и без остановки дочернего FFmpeg кнопка «Остановить» ждала бы
    его окончания.

    start_seconds и end_seconds задают обрезку по краям. Отсчёт ведётся от
    начала исходного файла, а в готовом WAV фрагмент начинается с нуля —
    смещение обратно на исходную шкалу делает транскрайбер.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, float(start_seconds))
    command = [
        _tool("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    if start > 0:
        command += ["-ss", f"{start:.3f}"]
    command += ["-i", str(source)]
    # stream_index=None — «первая аудиодорожка». Нужно для нарезки окон, когда
    # пользователь дорожку не выбирал и Whisper читал файл напрямую.
    command += (
        ["-map", "0:a:0"] if stream_index is None else ["-map", f"0:{stream_index}"]
    )
    if end_seconds is not None:
        length = float(end_seconds) - start
        if length <= 0:
            raise RuntimeError("Конец обрезки должен быть позже начала")
        command += ["-t", f"{length:.3f}"]
    command += [
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        # WAV хранит размер в 32-битном поле, поэтому обычный wav ломается после
        # 4 ГБ — при 16 кГц моно это примерно 37 часов записи. rf64=auto
        # переключает контейнер на RF64 только когда предел действительно
        # достигнут, поэтому короткие файлы остаются обычным WAV.
        "-f",
        "wav",
        "-rf64",
        "auto",
        str(target),
    ]
    result = cancellation.run(task_id, command)
    if result.returncode != 0 or not target.exists():
        detail = result.stderr.strip() or "Не удалось извлечь аудиодорожку"
        raise RuntimeError(detail)


def trim_bounds(
    duration_seconds: float,
    start_seconds: float | None,
    end_seconds: float | None,
) -> tuple[float, float | None]:
    """Привести обрезку к безопасным границам файла.

    Возвращает (начало, конец); конец равен None, когда дорожка играет до
    самого конца — тогда FFmpeg не получает `-t` и не рискует обрезать хвост
    из-за неточной длительности в контейнере.
    """
    total = max(0.0, float(duration_seconds or 0))
    start = max(0.0, float(start_seconds or 0))
    if total:
        start = min(start, max(0.0, total - 1.0))

    end: float | None = None
    if end_seconds is not None:
        end = max(0.0, float(end_seconds))
        if total:
            end = min(end, total)
        # Хвост короче половины секунды считаем «до конца»: пользователь просто
        # не дотянул ручку до края.
        if total and end >= total - 0.5:
            end = None

    if end is not None and end - start < 1.0:
        raise RuntimeError("Выбранный фрагмент короче секунды")
    return start, end


def waveform_path(media_id: str, stream_index: int) -> Path:
    root = storage.temporary_dir() / "waveforms"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{media_id}-stream-{stream_index}.json"


def track_audio_path(media_id: str, stream_index: int) -> Path:
    root = storage.temporary_dir() / "waveforms"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{media_id}-stream-{stream_index}.mp3"


def _read_peak_slots(
    source: Path,
    stream_index: int,
    audio_target: Path | None = None,
) -> tuple[list[int], int]:
    """Пик амплитуды на каждые 50 мс дорожки и общее число сэмплов.

    audio_target просит вторым выходом записать ту же дорожку в MP3. Один
    проход декодирования отдаёт сразу и картинку, и звук для прослушивания:
    отдельный прогон стоил бы столько же времени, сколько первый.
    """
    command = [
        _tool("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(_WAVEFORM_RATE),
        "-f",
        "s16le",
        "-",
    ]
    if audio_target is not None:
        command += [
            "-map",
            f"0:{stream_index}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(_PLAYBACK_RATE),
            "-c:a",
            "libmp3lame",
            "-b:a",
            _PLAYBACK_BITRATE,
            "-y",
            "-f",
            "mp3",
            str(audio_target),
        ]
    slots: list[int] = []
    pending = array("h")
    total_samples = 0
    tail = b""
    # Ошибки уходят во временный файл, а не в канал: на повреждённой записи
    # FFmpeg может залить трубу сообщениями и встать, пока мы читаем звук.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(1 << 18):
                data = tail + chunk
                usable = len(data) - len(data) % 2
                tail = data[usable:]
                block = array("h")
                block.frombytes(data[:usable])
                total_samples += len(block)
                pending.extend(block)
                position = 0
                while len(pending) - position >= _WAVEFORM_SLOT_SAMPLES:
                    window = pending[position : position + _WAVEFORM_SLOT_SAMPLES]
                    slots.append(max(max(window), -min(window)))
                    position += _WAVEFORM_SLOT_SAMPLES
                pending = pending[position:]
            if pending:
                slots.append(max(max(pending), -min(pending)))
        finally:
            process.stdout.close()
            process.wait()
        errors.seek(0)
        detail = errors.read().decode("utf-8", "replace").strip()

    if process.returncode != 0:
        raise RuntimeError(detail or "FFmpeg не смог прочитать дорожку")
    if not slots:
        raise RuntimeError("В выбранной дорожке нет звука")
    return slots, total_samples


def _compress_slots(slots: list[int], bars: int) -> list[float]:
    count = min(bars, len(slots))
    peaks: list[float] = []
    for index in range(count):
        left = index * len(slots) // count
        right = max(left + 1, (index + 1) * len(slots) // count)
        peaks.append(float(max(slots[left:right])))
    loudest = max(peaks)
    if loudest <= 0:
        return [0.0] * count
    # Нормируем по самому громкому месту: тихие записи иначе выглядят
    # плоской линией, по которой невозможно поймать границы речи.
    return [round(value / loudest, 4) for value in peaks]


def _cached_waveform(cached: Path, audio: Path) -> dict[str, Any] | None:
    """Готовая пара «картинка + звук». Без любой половины считаем заново."""
    if not cached.exists() or not audio.exists() or audio.stat().st_size == 0:
        return None
    try:
        return json.loads(cached.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached.unlink(missing_ok=True)
        return None


def build_waveform(
    media_id: str,
    source: Path,
    stream_index: int,
) -> dict[str, Any]:
    """Огибающая громкости и дорожка для прослушивания в редакторе границ."""
    cached = waveform_path(media_id, stream_index)
    audio = track_audio_path(media_id, stream_index)
    ready = _cached_waveform(cached, audio)
    if ready is not None:
        return ready

    with _waveform_lock:
        ready = _cached_waveform(cached, audio)
        if ready is not None:
            return ready
        partial_audio = audio.parent / f".{audio.stem}-{uuid.uuid4().hex}.mp3"
        try:
            slots, total_samples = _read_peak_slots(
                source,
                stream_index,
                partial_audio,
            )
            payload = {
                "stream_index": stream_index,
                "duration_seconds": round(total_samples / _WAVEFORM_RATE, 3),
                "peaks": _compress_slots(slots, _WAVEFORM_BARS),
            }
            partial_json = cached.parent / f".{cached.stem}-{uuid.uuid4().hex}.json"
            partial_json.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            partial_audio.replace(audio)
            partial_json.replace(cached)
        finally:
            partial_audio.unlink(missing_ok=True)
    return payload


def track_preview_path(
    media_id: str,
    stream_index: int,
    start_seconds: float,
) -> Path:
    root = storage.temporary_dir() / "track-previews"
    root.mkdir(parents=True, exist_ok=True)
    start = max(0, round(start_seconds))
    return root / f"{media_id}-stream-{stream_index}-at-{start}.mp3"


def create_track_preview(
    media_id: str,
    source: Path,
    stream_index: int,
    start_seconds: float,
    duration_seconds: float,
) -> Path:
    """Создать браузерный MP3-фрагмент одной выбранной аудиодорожки."""
    target = track_preview_path(media_id, stream_index, start_seconds)
    if target.exists() and target.stat().st_size > 0:
        return target

    with _preview_lock:
        if target.exists() and target.stat().st_size > 0:
            return target
        partial = target.parent / f".{target.stem}-{uuid.uuid4().hex}.mp3"
        command = [
            _tool("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, float(start_seconds)):.3f}",
            "-i",
            str(source),
            "-map",
            f"0:{stream_index}",
            "-t",
            f"{max(1.0, float(duration_seconds)):.3f}",
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
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
                timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            partial.unlink(missing_ok=True)
            raise RuntimeError("Создание превью заняло слишком много времени") from exc
        if result.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            detail = result.stderr.strip() or "Не удалось создать превью дорожки"
            raise RuntimeError(detail)
        partial.replace(target)
    return target


def fragment_path(
    media_id: str,
    stream_index: int,
    start_seconds: float,
    end_seconds: float | None,
) -> Path:
    root = storage.temporary_dir() / "track-previews"
    root.mkdir(parents=True, exist_ok=True)
    finish = "end" if end_seconds is None else f"{round(end_seconds)}"
    return root / (
        f"{media_id}-stream-{stream_index}-cut-{round(start_seconds)}-{finish}.mp3"
    )


def create_fragment(
    media_id: str,
    source: Path,
    stream_index: int,
    start_seconds: float,
    end_seconds: float | None,
) -> Path:
    """Вырезать выбранный кусок дорожки в MP3 — тот же, что уйдёт в Whisper."""
    target = fragment_path(media_id, stream_index, start_seconds, end_seconds)
    if target.exists() and target.stat().st_size > 0:
        return target

    with _preview_lock:
        if target.exists() and target.stat().st_size > 0:
            return target
        partial = target.parent / f".{target.stem}-{uuid.uuid4().hex}.mp3"
        command = [
            _tool("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
        ]
        if start_seconds > 0:
            command += ["-ss", f"{start_seconds:.3f}"]
        command += ["-i", str(source), "-map", f"0:{stream_index}"]
        if end_seconds is not None:
            command += ["-t", f"{end_seconds - start_seconds:.3f}"]
        command += [
            "-vn",
            "-ar",
            "44100",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
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
                timeout=900,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            partial.unlink(missing_ok=True)
            raise RuntimeError("Вырезание заняло слишком много времени") from exc
        if result.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            detail = result.stderr.strip() or "Не удалось вырезать фрагмент"
            raise RuntimeError(detail)
        partial.replace(target)
    return target


def clear_track_previews(media_id: str) -> None:
    for folder, pattern in (
        ("track-previews", f"{media_id}-stream-*.mp3"),
        ("waveforms", f"{media_id}-stream-*.json"),
        ("waveforms", f"{media_id}-stream-*.mp3"),
    ):
        root = storage.temporary_dir() / folder
        if not root.exists():
            continue
        for path in root.glob(pattern):
            path.unlink(missing_ok=True)
