"""Restore Whisper CUDA DLLs from official NVIDIA Windows wheels on PyPI."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "cu126": ("nvidia-cublas-cu12==12.4.5.8", "nvidia-cudnn-cu12==9.5.1.17"),
    "cu128": ("nvidia-cublas-cu12==12.8.4.1", "nvidia-cudnn-cu12==9.10.2.21"),
}
REQUIRED = {"cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll"}


def extract_dlls(wheels: list[Path], destination: Path) -> set[str]:
    names = set()
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if path.parent.as_posix() not in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
                    continue
                if path.suffix.lower() != ".dll":
                    continue
                if path.name in names:
                    raise RuntimeError(f"Duplicate DLL in NVIDIA wheels: {path.name}")
                with archive.open(member) as source, (destination / path.name).open("wb") as target:
                    shutil.copyfileobj(source, target)
                names.add(path.name)
    missing = REQUIRED - names
    if missing:
        raise RuntimeError(f"NVIDIA wheels are missing DLLs: {sorted(missing)}")
    return names


def install(variant: str, *, force: bool = False) -> None:
    destination = ROOT / "vendor" / "cuda" / "bin"
    manifest = destination.parent / "runtime.json"
    try:
        previous = json.loads(manifest.read_text(encoding="utf-8"))["variant"]
    except (OSError, ValueError, KeyError, TypeError):
        previous = "cu126"  # Existing working installation predates this helper.
    if not force and previous == variant and all((destination / name).is_file() for name in REQUIRED):
        print("Whisper CUDA libraries are already present.", flush=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="install-", dir=destination.parent) as folder:
        stage = Path(folder)
        downloads = stage / "wheels"
        downloads.mkdir()
        subprocess.run([
            sys.executable, "-m", "pip", "--timeout", "120", "--retries", "5", "download", "--index-url", "https://pypi.org/simple",
            "--only-binary=:all:", "--no-deps", "--dest", str(downloads), *PACKAGES[variant],
        ], check=True)
        extracted = stage / "dlls"
        extracted.mkdir()
        names = extract_dlls(list(downloads.glob("*.whl")), extracted)
        destination.mkdir(exist_ok=True)
        for name in names:
            (extracted / name).replace(destination / name)
        manifest.write_text(json.dumps({"variant": variant, "packages": PACKAGES[variant]}) + "\n", encoding="utf-8")
    print(f"Whisper CUDA libraries installed: {variant}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=PACKAGES, default="cu126")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    install(args.variant, force=args.force)
