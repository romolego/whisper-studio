from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUDA_BIN = ROOT / "vendor" / "cuda" / "bin"
if CUDA_BIN.exists():
    os.add_dll_directory(str(CUDA_BIN))
    os.environ["PATH"] = f"{CUDA_BIN}{os.pathsep}{os.environ['PATH']}"

from backend.dictation import _dictation_initial_prompt
from backend.transcriber import _load_model

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


PROFILES = {
    "fast": {
        "beam_size": 1,
        "best_of": 1,
        "patience": 1.0,
        "condition_on_previous_text": False,
        "temperature": 0.0,
        "vad_parameters": {"min_silence_duration_ms": 450},
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.7,
    },
    "current_accurate": {
        "beam_size": 5,
        "best_of": 5,
        "patience": 1.0,
        "condition_on_previous_text": False,
        "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        "vad_parameters": {"min_silence_duration_ms": 450},
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.7,
    },
    "accurate_context": {
        "beam_size": 1,
        "best_of": 1,
        "patience": 1.0,
        "condition_on_previous_text": True,
        "temperature": 0.0,
        "vad_parameters": {"min_silence_duration_ms": 450},
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.7,
    },
    "context_greedy": {
        "beam_size": 1,
        "best_of": 1,
        "patience": 1.0,
        "condition_on_previous_text": True,
        "temperature": 0.0,
        "vad_parameters": {
            "threshold": 0.4,
            "min_speech_duration_ms": 80,
            "min_silence_duration_ms": 350,
            "speech_pad_ms": 500,
        },
        "compression_ratio_threshold": 2.5,
        "log_prob_threshold": -1.15,
        "no_speech_threshold": 0.6,
        "hallucination_silence_threshold": 2.0,
    },
    "beam3_context": {
        "beam_size": 3,
        "best_of": 3,
        "patience": 1.15,
        "condition_on_previous_text": True,
        "temperature": [0.0, 0.2, 0.4],
        "vad_parameters": {
            "threshold": 0.4,
            "min_speech_duration_ms": 80,
            "min_silence_duration_ms": 350,
            "speech_pad_ms": 500,
        },
        "compression_ratio_threshold": 2.5,
        "log_prob_threshold": -1.15,
        "no_speech_threshold": 0.6,
        "hallucination_silence_threshold": 2.0,
    },
    "balanced": {
        "beam_size": 5,
        "best_of": 5,
        "patience": 1.25,
        "condition_on_previous_text": True,
        "temperature": [0.0, 0.2, 0.4],
        "vad_parameters": {
            "threshold": 0.4,
            "min_speech_duration_ms": 80,
            "min_silence_duration_ms": 350,
            "speech_pad_ms": 500,
        },
        "compression_ratio_threshold": 2.5,
        "log_prob_threshold": -1.15,
        "no_speech_threshold": 0.6,
        "hallucination_silence_threshold": 2.0,
    },
    "heavy": {
        "beam_size": 8,
        "best_of": 8,
        "patience": 1.35,
        "condition_on_previous_text": True,
        "temperature": [0.0, 0.2, 0.4],
        "vad_parameters": {
            "threshold": 0.35,
            "min_speech_duration_ms": 60,
            "min_silence_duration_ms": 300,
            "speech_pad_ms": 550,
        },
        "compression_ratio_threshold": 2.6,
        "log_prob_threshold": -1.2,
        "no_speech_threshold": 0.55,
        "hallucination_silence_threshold": 2.0,
    },
}


def transcribe(model, path: Path, profile: dict, clip: str) -> dict:
    started = time.perf_counter()
    segments, info = model.transcribe(
        str(path),
        language="ru",
        task="transcribe",
        initial_prompt=_dictation_initial_prompt("ru"),
        vad_filter=True,
        word_timestamps=False,
        repetition_penalty=1.12,
        no_repeat_ngram_size=3,
        clip_timestamps=clip,
        **profile,
    )
    items = list(segments)
    elapsed = time.perf_counter() - started
    text = " ".join(item.text.strip() for item in items if item.text.strip())
    speech_seconds = sum(max(0.0, item.end - item.start) for item in items)
    weighted_logprob = (
        sum(item.avg_logprob * max(0.01, item.end - item.start) for item in items)
        / max(0.01, speech_seconds)
    )
    return {
        "seconds": round(elapsed, 3),
        "audio_seconds": round(float(info.duration), 3),
        "speed_x": round(float(info.duration) / max(elapsed, 0.001), 2),
        "avg_logprob": round(weighted_logprob, 4),
        "segments": len(items),
        "text": text,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", nargs="+", type=Path)
    parser.add_argument("--clip", default="0")
    parser.add_argument("--profiles", nargs="*", choices=PROFILES)
    args = parser.parse_args()

    selected_profiles = args.profiles or list(PROFILES)

    model, device = _load_model("ru")
    print(json.dumps({"device": device, "profiles": selected_profiles}, ensure_ascii=False))

    # Один короткий прогон разогревает CUDA; его время не входит в сравнение.
    warmup = args.audio[0]
    list(
        model.transcribe(
            str(warmup),
            language="ru",
            beam_size=1,
            best_of=1,
            vad_filter=True,
            clip_timestamps="0,3",
        )[0]
    )

    for path in args.audio:
        print(json.dumps({"audio": str(path)}, ensure_ascii=False))
        for name in selected_profiles:
            profile = PROFILES[name]
            result = transcribe(model, path, profile, args.clip)
            print(json.dumps({"profile": name, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
