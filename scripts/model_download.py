"""Download pinned model weights with resumable ranges and SHA-256 verification."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DownloadSpec:
    repo: str
    revision: str
    size: int
    sha256: str
    directory: Path
    filename: str = "model.bin"


def download_weights(session_factory, *, spec: DownloadSpec, workers: int = 12) -> Path:
    """Small verified HTTP ranges survive interrupted large TLS transfers."""
    spec.directory.mkdir(parents=True, exist_ok=True)
    target = spec.directory / spec.filename
    if target.exists():
        verify(target, spec.size, spec.sha256)
        return target
    chunk_size = 4 * 1024 * 1024
    partial = spec.directory / (spec.filename + ".partial")
    state = spec.directory / (spec.filename + ".ranges.json")
    completed = set()
    if partial.exists() and partial.stat().st_size == spec.size and state.exists():
        saved = json.loads(state.read_text("utf-8"))
        if saved.get("revision") == spec.revision and saved.get("chunk_size") == chunk_size:
            completed = set(saved.get("completed", []))
    else:
        with partial.open("wb") as output:
            output.truncate(spec.size)
    with session_factory() as client:
        response = client.head(f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{spec.filename}",
                               allow_redirects=True, timeout=(10, 60))
        response.raise_for_status()
        signed_url = response.url
    count = (spec.size + chunk_size - 1) // chunk_size
    lock = threading.Lock()

    def fetch(index):
        start = index * chunk_size
        end = min(spec.size - 1, start + chunk_size - 1)
        for attempt in range(6):
            try:
                with session_factory() as client:
                    response = client.get(signed_url, headers={"Range": f"bytes={start}-{end}",
                                          "Accept-Encoding": "identity"}, timeout=(10, 60))
                    response.raise_for_status()
                    expected = f"bytes {start}-{end}/{spec.size}"
                    if response.headers.get("Content-Range") != expected or len(response.content) != end - start + 1:
                        raise RuntimeError("The download server returned an incorrect byte range")
                    data = response.content
                with lock:
                    with partial.open("r+b") as output:
                        output.seek(start); output.write(data); output.flush()
                    completed.add(index)
                    temporary = state.with_suffix(".tmp")
                    temporary.write_text(json.dumps({"revision": spec.revision, "chunk_size": chunk_size,
                                         "completed": sorted(completed)}), "utf-8")
                    temporary.replace(state)
                    if len(completed) % 32 == 0 or len(completed) == count:
                        print(f"{spec.filename}: {len(completed) * 100 // count}%", flush=True)
                return
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(min(attempt + 1, 3))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch, (index for index in range(count) if index not in completed)))
    # Publish only after the complete file passes its pinned content hash.
    verify(partial, spec.size, spec.sha256)
    partial.replace(target)
    state.unlink(missing_ok=True)
    return target


def verify(weights: Path, size: int, sha256: str) -> None:
    if weights.stat().st_size != size:
        raise RuntimeError("The model download is incomplete")
    digest = hashlib.sha256()
    with weights.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise RuntimeError("The model checksum does not match the pinned revision")
