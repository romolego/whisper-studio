"""Installation preference, separate from projects and portable source code."""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime.json"


def preferred_device() -> str:
    value = os.environ.get("WHISPER_DEVICE")
    if value is None:
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["device"]
        except (OSError, ValueError, KeyError, TypeError):
            value = "auto"
    return value if value in ("auto", "cpu", "cuda") else "auto"
