"""Разметка спикеров моделью NVIDIA Nemotron 3 Diarization.

Запускается отдельным процессом в среде диаризации, как и worker Pyannote:
у обеих моделей свой torch, а сервису он не нужен. На вход — WAV, на выход —
JSON со списком реплик того же вида, что отдаёт Pyannote.

С ключом --download только скачивает модель в кеш Hugging Face. Токен не
нужен: модель открытая, без принятия условий.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

DOWNLOAD_MODE = sys.argv[1:] == ["--download"]
# Константы huggingface_hub читаются при импорте, поэтому режим сети
# выбирается до импорта transformers.
if not DOWNLOAD_MODE:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
warnings.filterwarnings("ignore", category=FutureWarning)

import numpy as np
import soundfile as sf
import torch
from transformers import AutoModelForAudioFrameClassification, AutoProcessor

from worker_device import pick_device


MODEL_ID = "nvidia/Nemotron-3-Diarization"
MODEL_REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"
# В репозитории модели лежат ещё .nemo для NeMo, GGUF для C++ и демо-ролики —
# ещё 300 МБ, которые этому worker не нужны.
MODEL_FILES = ["config.json", "processor_config.json", "model.safetensors"]
SAMPLE_RATE = 16000

# Спектрограмма считается кусками, а не всей записью разом. Целиком двухчасовая
# запись разворачивается в STFT на несколько гигабайт оперативной памяти, а
# шестичасовая — на полтора десятка. Кусок в десять минут стоит сотню
# мегабайт, и склеенный результат совпадает с проходом по всей записи кадр в
# кадр (см. _features).
PIECE_SECONDS = 600

# Порог вероятности и сглаживание реплик. Модель выдаёт вероятность каждого из
# восьми спикеров на каждые 10 мс; 0.5 — порог из карточки модели. Паузы короче
# GAP_FILL_SECONDS внутри речи одного человека склеиваются: иначе слово,
# попавшее на вдох между двумя кусками, оставалось бы без спикера. Всплески
# короче BLIP_SECONDS — это щелчки и чужие «угу» на полкадра, а не реплики.
THRESHOLD = 0.5
GAP_FILL_SECONDS = 0.25
BLIP_SECONDS = 0.08


def _decode_with_ffmpeg(path: Path) -> np.ndarray:
    """Запасной путь для форматов, которые soundfile не читает (m4a, mp4…)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg не найден, а soundfile этот файл не читает")
    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "FFmpeg не смог прочитать аудио")
    return np.frombuffer(result.stdout, dtype=np.float32).copy()


def load_audio(path: Path) -> np.ndarray:
    """Моно 16 кГц float32 — то, на чём обучена модель."""
    try:
        data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception:
        return _decode_with_ffmpeg(path)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    if sample_rate != SAMPLE_RATE:
        import torchaudio.functional as audio_functional

        mono = audio_functional.resample(
            torch.from_numpy(np.ascontiguousarray(mono)),
            int(sample_rate),
            SAMPLE_RATE,
        ).numpy()
    return np.ascontiguousarray(mono, dtype=np.float32)


def _span(audio: np.ndarray, start: int, stop: int) -> np.ndarray:
    """audio[start:stop], где всё за краями записи — тишина."""
    left = max(0, -start)
    right = max(0, stop - len(audio))
    piece = audio[max(0, start) : min(len(audio), stop)]
    if left or right:
        piece = np.pad(piece, (left, right))
    return piece


def _features(feature_extractor, audio: np.ndarray) -> torch.Tensor:
    """Лог-мел спектрограмма всей записи, посчитанная кусками.

    Проход по всей записи с center=True дополняет её n_fft/2 нулями с каждой
    стороны, и кадр i занимает отсчёты [hop*i - n_fft/2, hop*i + n_fft/2).
    Кусок с center=False, начатый ровно с этого места, даёт те же кадры — это
    же свойство модель использует в потоковом режиме. Предыскажение внутри
    куска теряет связь с предыдущим отсчётом только для первого отсчёта окна,
    а у окна Ханна там нулевой вес, поэтому склейка совпадает с цельным
    проходом.
    """
    hop = int(feature_extractor.hop_length)
    n_fft = int(feature_extractor.n_fft)
    half = n_fft // 2
    total = len(audio) // hop
    if total < 1:
        raise RuntimeError("Запись слишком короткая для разметки спикеров")
    step = max(1, PIECE_SECONDS * SAMPLE_RATE // hop)
    pieces = []
    for first in range(0, total, step):
        last = min(first + step, total)
        chunk = _span(audio, hop * first - half, hop * (last - 1) + n_fft - half)
        output = feature_extractor(
            chunk,
            sampling_rate=SAMPLE_RATE,
            center=False,
            return_tensors="pt",
        )
        pieces.append(output["input_features"][:, : last - first])
    return torch.cat(pieces, dim=1)


def _smooth(
    segments: list[tuple[float, float, int]],
) -> list[tuple[float, float, int]]:
    by_speaker: dict[int, list[list[float]]] = {}
    for start, end, speaker in sorted(segments):
        runs = by_speaker.setdefault(speaker, [])
        if runs and start - runs[-1][1] <= GAP_FILL_SECONDS:
            runs[-1][1] = max(runs[-1][1], end)
        else:
            runs.append([start, end])
    result = [
        (start, end, speaker)
        for speaker, runs in by_speaker.items()
        for start, end in runs
        if end - start >= BLIP_SECONDS
    ]
    result.sort(key=lambda item: (item[0], item[2]))
    return result


def diarize(audio: np.ndarray, device: torch.device) -> list[dict]:
    processor = AutoProcessor.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    model = AutoModelForAudioFrameClassification.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    model.to(device)
    model.eval()

    features = _features(processor.feature_extractor, audio)
    frame_seconds = (
        processor.feature_extractor.hop_length
        / processor.feature_extractor.sampling_rate
    )
    del audio
    with torch.inference_mode():
        logits = model(input_features=features.to(device)).logits
    probabilities = logits[0].float().sigmoid().cpu()
    active = (probabilities > THRESHOLD).to(torch.int8)
    # +1 там, где спикер начал говорить, -1 — где замолчал.
    edge = torch.zeros(1, active.shape[1], dtype=torch.int8)
    changes = torch.cat([edge, active, edge]).diff(dim=0)
    raw: list[tuple[float, float, int]] = []
    for speaker in range(changes.shape[1]):
        starts = (changes[:, speaker] == 1).nonzero()[:, 0].tolist()
        ends = (changes[:, speaker] == -1).nonzero()[:, 0].tolist()
        raw.extend(
            (start * frame_seconds, end * frame_seconds, speaker)
            for start, end in zip(starts, ends)
        )
    print(
        f"nemotron: кадров {active.shape[0]}, реплик до сглаживания {len(raw)}",
        file=sys.stderr,
    )
    # Метки в том же виде, что у Pyannote: SPEAKER_00, SPEAKER_01… Номер
    # спикера — порядок первого появления голоса в записи.
    return [
        {
            "start": round(start, 3),
            "end": round(end, 3),
            "speaker": f"SPEAKER_{speaker:02d}",
        }
        for start, end, speaker in _smooth(raw)
    ]


def download() -> int:
    from huggingface_hub import snapshot_download

    print(snapshot_download(MODEL_ID, revision=MODEL_REVISION, allow_patterns=MODEL_FILES))
    return 0


def main() -> int:
    if DOWNLOAD_MODE:
        return download()
    if len(sys.argv) != 3:
        raise RuntimeError("Ожидаются пути к WAV и JSON")
    audio_path = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()
    device = pick_device()
    print(f"nemotron device: {device}", file=sys.stderr)
    turns = diarize(load_audio(audio_path), device)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(turns, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
