from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
warnings.filterwarnings(
    "ignore",
    message=".*torchcodec is not installed correctly.*",
)

import soundfile as sf
import torch
from pyannote.audio import Pipeline

from worker_device import pick_device


MODEL_ID = "pyannote/speaker-diarization-community-1"


def load_audio(path: Path) -> dict[str, Any]:
    data, sample_rate = sf.read(
        str(path),
        dtype="float32",
        always_2d=True,
    )
    mono = data.mean(axis=1)
    waveform = torch.from_numpy(mono).float().unsqueeze(0)
    return {"waveform": waveform, "sample_rate": int(sample_rate)}


def annotation_from(output: Any) -> Any:
    if hasattr(output, "itertracks"):
        return output
    for name in (
        "exclusive_speaker_diarization",
        "speaker_diarization",
        "diarization",
        "annotation",
    ):
        value = getattr(output, name, None)
        if hasattr(value, "itertracks"):
            return value
    raise RuntimeError("Pyannote вернул неизвестный формат результата")


def main() -> int:
    if len(sys.argv) != 3:
        raise RuntimeError("Ожидаются пути к WAV и JSON")
    audio_path = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()
    pipeline = Pipeline.from_pretrained(
        MODEL_ID,
        token="hf_offline_cache_only",
    )
    if pipeline is None:
        raise RuntimeError("Не удалось загрузить локальную модель Pyannote")
    device = pick_device()
    print(f"pyannote device: {device}", file=sys.stderr)
    pipeline.to(device)
    annotation = annotation_from(pipeline(load_audio(audio_path)))
    turns = [
        {
            "start": float(turn.start),
            "end": float(turn.end),
            "speaker": str(speaker),
        }
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(turns, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
