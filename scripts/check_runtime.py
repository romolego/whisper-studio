"""Check computation on the requested device without downloading model weights."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(device: str, diarization_python: str | None = None) -> None:
    # Keep handles alive while CTranslate2 imports and probes the DLLs.
    handles = []
    cuda = ROOT / "vendor" / "cuda" / "bin"
    if cuda.is_dir() and os.name == "nt":
        handles.append(os.add_dll_directory(str(cuda)))
        os.environ["PATH"] = str(cuda) + os.pathsep + os.environ.get("PATH", "")
    import ctranslate2

    supported = ctranslate2.get_supported_compute_types(device)
    if not supported:
        raise RuntimeError(f"CTranslate2 cannot use {device}")
    print(f"Whisper computation device: {device}; supported types: {sorted(supported)}", flush=True)
    if diarization_python:
        program = """
import json, torch
device = %r
if device == 'cuda':
    if not torch.cuda.is_available():
        raise RuntimeError('PyTorch CUDA is not available; check driver and wheel variant')
    major, minor = torch.cuda.get_device_capability()
    if f'sm_{major}{minor}' not in torch.cuda.get_arch_list():
        raise RuntimeError('GPU architecture is not supported by this PyTorch build')
sample = torch.ones(8, 8, device=device)
assert (sample @ sample).sum().item() == 512
print(json.dumps({'torch': torch.__version__, 'device': device, 'matrix_check': 'passed'}))
""" % device
        subprocess.run([diarization_python, "-c", program], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--diarization-python")
    args = parser.parse_args()
    check(args.device, args.diarization_python)
