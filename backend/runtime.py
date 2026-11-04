"""Installation preference, separate from projects and portable source code."""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime.json"
TURBO_MODEL_FILES = ("model.bin", "config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json")
NEMOTRON_REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"


def missing_turbo_files(folder: Path) -> list[str]:
    """Files needed for a local, offline load of either pinned Turbo checkpoint."""
    return [name for name in TURBO_MODEL_FILES if not (folder / name).is_file()]


def preferred_device() -> str:
    value = os.environ.get("WHISPER_DEVICE")
    if value is None:
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["device"]
        except (OSError, ValueError, KeyError, TypeError):
            value = "auto"
    return value if value in ("auto", "cpu", "cuda") else "auto"
