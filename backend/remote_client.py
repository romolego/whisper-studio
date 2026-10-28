"""Opt-in remote model calls. Projects, media preparation and exports stay local."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import ipaddress
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import wave

import requests

from . import cancellation, gpu_turns, media, storage

CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "remote-client.json"
_lock = threading.RLock()
_context: ContextVar[tuple | None] = ContextVar("remote_task", default=None)
_status_cache: tuple = (None, 0, None)


def read_config() -> dict:
    with _lock:
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"mode": "local"}
        if value.get("mode") not in ("local", "remote"):
            raise RuntimeError("Повреждены настройки удалённых вычислений")
        return value


def save_config(value: dict) -> None:
    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(CONFIG_PATH)


def decode_code(code: str) -> dict:
    try:
        if len(code) > 4096 or not code.startswith("ws1."):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(code[4:] + "=" * (-len(code[4:]) % 4)))
        url, token = value["url"], value["token"]
        parsed = urlsplit(url)
        address = ipaddress.ip_address(parsed.hostname or "")
        # HTTP is carried inside the encrypted Tailscale network, or a LAN.
        # Public internet transport must use HTTPS and a separate reverse proxy.
        if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or not parsed.port or not isinstance(token, str) or len(token) < 32
                or address.is_unspecified or address.is_multicast
                or (parsed.scheme == "http" and not (
                    address.is_private or address in ipaddress.ip_network("100.64.0.0/10")))):
            raise ValueError()
        return {"url": url.rstrip("/"), "token": token}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("Неверный код подключения Whisper Studio") from exc


def encode_code(url: str, token: str) -> str:
    payload = json.dumps({"url": url, "token": token}, separators=(",", ":")).encode()
    return "ws1." + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _request(config: dict, method: str, path: str, **kwargs):
    # Never forward credentials through environment proxies or redirects.
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, config["url"] + path,
                                       headers={"Authorization": "Bearer " + config["token"],
                                                **kwargs.pop("headers", {})},
                                       timeout=kwargs.pop("timeout", (8, 30)),
                                       allow_redirects=False, **kwargs)
        if response.status_code == 401:
            raise RuntimeError("Удалённый компьютер отклонил ключ подключения")
        if response.status_code >= 300:
            raise RuntimeError(f"Удалённый адаптер вернул HTTP {response.status_code}")
        return response.json()
    except requests.RequestException as exc:
        raise RuntimeError("Удалённый компьютер недоступен. Проверьте адаптер и соединение Tailscale.") from exc


def probe(config: dict) -> dict:
    result = _request(config, "GET", "/v1/status")
    if result.get("protocol") != 1:
        raise RuntimeError("Несовместимая версия удалённого адаптера")
    return result


def speaker_status() -> dict:
    global _status_cache
    config = selected()
    cache_key = (config["url"], config["token"])
    with _lock:
        if _status_cache[0] == cache_key and time.monotonic() - _status_cache[1] < 5:
            return _status_cache[2]
    try:
        engines = probe(config)["engines"]
    except RuntimeError:
        engines = {}
    values = {key: {"available": False, "model_ready": False, "package_installed": False,
                    "device": "cpu", "cuda_available": False, "runtime": "Удалённый компьютер",
                    **engines.get(key, {})} for key in ("pyannote", "nemotron")}
    for value in values.values():
        value["package_installed"] = value["available"]
    result = {**values["pyannote"], "engines": values,
              "any_available": any(value["available"] for value in values.values()),
              "default_engine": storage.get_settings()["diarization_engine"],
              "configured_device": "auto", "remote": True}
    with _lock:
        _status_cache = (cache_key, time.monotonic(), result)
    return result


def selected() -> dict | None:
    context = _context.get()
    if context is not None:
        return context[1]
    config = read_config()
    return config if config["mode"] == "remote" else None


@contextmanager
def local_only():
    token = _context.set((None, None))
    try:
        yield
    finally:
        _context.reset(token)


def task_scope(function):
    @wraps(function)
    def run(task_id, *args, **kwargs):
        token = _context.set((task_id, selected()))
        try:
            return function(task_id, *args, **kwargs)
        finally:
            _context.reset(token)
    return run


def _namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


def _check(task_id):
    if task_id:
        cancellation.check(task_id)


def compute(config: dict, audio, spec: dict, task_id: str | None = None, progress=None) -> dict:
    """Send only mono 16 kHz PCM, poll with a lease, always cancel/release the task."""
    task_id = task_id or (_context.get() or (None,))[0]
    _check(task_id)
    remote_id = None
    with tempfile.TemporaryDirectory(prefix="remote-send-", dir=storage.temporary_dir()) as folder:
        target = Path(folder) / "audio.wav"
        if isinstance(audio, (str, Path)):
            source = Path(audio)
            try:
                with wave.open(str(source), "rb") as handle:
                    prepared = (handle.getnchannels() == 1 and handle.getframerate() == 16000
                                and handle.getsampwidth() == 2 and handle.getcomptype() == "NONE")
            except (wave.Error, EOFError):
                prepared = False
            if prepared:
                target = source
            else:
                media.extract_audio(source, None, target, task_id)
        else:
            import numpy as np
            samples = (np.clip(audio, -1, 1) * 32767).astype("<i2")
            with wave.open(str(target), "wb") as handle:
                handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                handle.writeframes(samples.tobytes())
        try:
            _check(task_id)
            with target.open("rb") as handle:
                class CheckedUpload:
                    def read(self, size=-1):
                        _check(task_id)
                        return handle.read(size)

                    def __getattr__(self, name):
                        return getattr(handle, name)

                # requests streams a file object without holding the upload in memory.
                created = _request(config, "POST", "/v1/tasks", data=CheckedUpload(),
                                   headers={"Content-Type": "audio/wav", "X-Whisper-Spec": json.dumps(spec)},
                                   timeout=(8, 120))
            remote_id = created["id"]
            reported = -1.0
            while True:
                _check(task_id)
                gpu_turns.checkpoint()
                state = _request(config, "GET", f"/v1/tasks/{remote_id}")
                value = float(state.get("progress", 0))
                if progress and value - reported >= 0.015:
                    progress(value)
                    reported = value
                if state["status"] == "completed":
                    return state["result"]
                if state["status"] in ("failed", "cancelled"):
                    raise RuntimeError(state.get("error") or "Удалённое вычисление остановлено")
                time.sleep(0.5)
        finally:
            if remote_id:
                try:
                    _request(config, "DELETE", f"/v1/tasks/{remote_id}", timeout=(3, 5))
                except RuntimeError:
                    # The server lease cancels orphaned work even if the network died.
                    pass


class RemoteModel:
    def __init__(self, selector: str, config: dict):
        self.selector, self.config = selector, config
        self.remote_device = "remote"
        self.progress = None

    def transcribe(self, audio, **options):
        result = compute(self.config, audio, {"kind": "whisper", "model": self.selector,
                                             "options": options}, progress=self.progress)
        self.remote_device = "remote_" + result["device"]
        return iter(_namespace(result["segments"])), _namespace(result["info"])
