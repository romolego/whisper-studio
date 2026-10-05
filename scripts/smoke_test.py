from __future__ import annotations

import gc
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUDA_BIN = ROOT / "vendor" / "cuda" / "bin"
if CUDA_BIN.exists():
    os.add_dll_directory(str(CUDA_BIN))
    os.environ["PATH"] = f"{CUDA_BIN}{os.pathsep}{os.environ['PATH']}"

from faster_whisper import WhisperModel


CASES = [
    ("ru", ROOT / "models" / "turbo-russian", ROOT / "data" / "test-ru.wav"),
    ("en", ROOT / "models" / "turbo", ROOT / "data" / "test-en.wav"),
]


for language, model_path, audio_path in CASES:
    model = WhisperModel(
        str(model_path),
        device="cuda",
        compute_type="int8_float32",
        cpu_threads=4,
    )
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=1,
        vad_filter=False,
    )
    text = " ".join(segment.text.strip() for segment in segments)
    print(f"{language}: {text}")
    print(
        f"{language}: detected={info.language}, "
        f"probability={info.language_probability:.3f}"
    )
    del model
    gc.collect()
