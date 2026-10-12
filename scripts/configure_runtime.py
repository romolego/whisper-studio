"""Set the installation device; choose Nemotron only for a new, empty setup."""
from __future__ import annotations

import argparse
import json
from contextlib import closing

from backend import runtime, storage
from scripts.doctor import ensure_storage


def configure(device: str) -> None:
    with closing(storage.connect()) as db:
        initialized = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone() is not None
    ensure_storage()
    values = {}
    if not initialized:
        values["diarization_engine"] = "nemotron"
        values["diarization_device"] = device
    elif device != "auto":
        values["diarization_device"] = device
    if values:
        storage.update_settings(values)
    runtime.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = runtime.CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"device": device}) + "\n", encoding="utf-8")
    temporary.replace(runtime.CONFIG_PATH)
    print(f"Installation device: {device}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), required=True)
    configure(parser.parse_args().device)
