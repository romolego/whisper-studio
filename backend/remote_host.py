"""A deliberately separate, authenticated compute-only listener.

Runs in the Studio process to share models and GPU arbitration. It never mounts
the Studio API, projects, settings, file downloads or shutdown routes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import logging
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import uvicorn

from . import cancellation, diarization, gpu_turns, remote_client, runtime, storage

HOST_CONFIG = Path(__file__).resolve().parents[1] / "data" / "remote-host.json"
UPLOAD_LIMIT = 2 * 1024**3
TASK_LEASE = 90
HOST_LEASE = 35
MAX_TASKS = 8
DECODE_KEYS = {
    "language", "task", "beam_size", "best_of", "patience", "temperature",
    "vad_filter", "vad_parameters", "word_timestamps", "condition_on_previous_text",
    "repetition_penalty", "no_repeat_ngram_size", "compression_ratio_threshold",
    "log_prob_threshold", "no_speech_threshold", "hotwords", "initial_prompt",
}


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["whisper", "diarization"]
    model: Literal["ru", "en", "auto", "turbo", "large-v3"] = "ru"
    engine: Literal["nemotron", "pyannote"] = "nemotron"
    options: dict = Field(default_factory=dict)

    def validate_options(self):
        if set(self.options) - DECODE_KEYS:
            raise ValueError("Unsupported decode options")
        for key in ("beam_size", "best_of"):
            value = self.options.get(key, 5)
            if type(value) is not int or not 1 <= value <= 10:
                raise ValueError("Unsupported decode options")


@dataclass
class Task:
    id: str
    folder: tempfile.TemporaryDirectory
    spec: TaskSpec
    status: str = "uploading"
    touched: float = field(default_factory=time.monotonic)
    progress: float = 0
    result: dict | None = None
    error: str | None = None
    discard: bool = False

    @property
    def path(self):
        return Path(self.folder.name) / "audio.wav"


def credentials() -> dict:
    try:
        return json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {"token": secrets.token_urlsafe(32), "id": uuid.uuid4().hex}
        HOST_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        HOST_CONFIG.write_text(json.dumps(value), encoding="utf-8")
        return value


def local_capabilities() -> dict:
    from . import transcriber
    import ctranslate2
    with remote_client.local_only():
        speakers = diarization.status()
    return {
        "protocol": 1,
        "host_id": credentials()["id"],
        "device_preference": runtime.preferred_device(),
        "cuda_available": ctranslate2.get_cuda_device_count() > 0,
        "models": {"ru": (transcriber.RUSSIAN_MODEL_DIR / "model.bin").is_file(),
                   "turbo": (transcriber.ENGLISH_MODEL_DIR / "model.bin").is_file(),
                   "large-v3": (transcriber.FULL_MODEL_DIR / "model.bin").is_file()},
        # Do not disclose local paths or tokens on the network.
        "engines": {key: {name: value[name] for name in ("available", "model_ready", "device", "cuda_available")}
                    for key, value in speakers["engines"].items()},
    }


class ComputeHost:
    def __init__(self, token: str):
        self.token = token
        self.lock = threading.RLock()
        self.tasks: dict[str, Task] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remote-compute")
        self.closed = False
        self.api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @self.api.middleware("http")
        async def authenticate(request: Request, call_next):
            if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + self.token):
                return JSONResponse({"detail": "Invalid key"}, status_code=401)
            # Browser origins are never a compute client; no CORS on this listener.
            if request.headers.get("origin"):
                return JSONResponse({"detail": "Browser access disabled"}, status_code=403)
            if self.closed:
                return JSONResponse({"detail": "Adapter stopped"}, status_code=503)
            return await call_next(request)

        self.api.add_api_route("/v1/status", local_capabilities, methods=["GET"])
        self.api.add_api_route("/v1/tasks", self.upload, methods=["POST"])
        self.api.add_api_route("/v1/tasks/{task_id}", self.state, methods=["GET"])
        self.api.add_api_route("/v1/tasks/{task_id}", self.remove, methods=["DELETE"])

    def expire(self):
        with self.lock:
            for task in list(self.tasks.values()):
                if time.monotonic() - task.touched > TASK_LEASE:
                    self._discard(task)

    def _discard(self, task):
        task.discard = True
        cancellation.request(task.id)
        if task.status in ("completed", "failed", "cancelled"):
            self.tasks.pop(task.id, None)
            cancellation.release(task.id)

    def close(self):
        with self.lock:
            self.closed = True
            for task in list(self.tasks.values()):
                self._discard(task)
        self.executor.shutdown(wait=False)

    async def upload(self, request: Request):
        raw = request.headers.get("x-whisper-spec", "")
        if len(raw) > 16000:
            raise HTTPException(422, "Options too large")
        try:
            spec = TaskSpec.model_validate_json(raw)
            spec.validate_options()
        except (ValidationError, ValueError):
            raise HTTPException(422, "Invalid compute options")
        with self.lock:
            self.expire()
            if self.closed or len(self.tasks) >= MAX_TASKS:
                raise HTTPException(429, "Remote queue full; retry later")
            # Separate from data/temp: normal Studio startup may purge that folder.
            root = HOST_CONFIG.parent / "remote-work"
            root.mkdir(parents=True, exist_ok=True)
            task = Task("remote-" + uuid.uuid4().hex,
                        tempfile.TemporaryDirectory(prefix="task-", dir=root), spec)
            self.tasks[task.id] = task
        try:
            size = 0
            with task.path.open("wb") as handle:
                async for chunk in request.stream():
                    cancellation.check(task.id)
                    size += len(chunk)
                    if size > UPLOAD_LIMIT:
                        raise HTTPException(413, "Prepared audio exceeds 2 GiB")
                    handle.write(chunk)
                    task.touched = time.monotonic()
            try:
                with wave.open(str(task.path), "rb") as handle:
                    if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) != (1, 2, 16000, "NONE"):
                        raise HTTPException(422, "Expected mono 16 kHz PCM WAV")
            except (wave.Error, EOFError):
                raise HTTPException(422, "Invalid WAV")
            task.status = "queued"
            self.executor.submit(self.run, task)
            return {"id": task.id}
        except BaseException:
            with self.lock:
                self.tasks.pop(task.id, None)
            task.folder.cleanup()
            cancellation.release(task.id)
            raise

    def state(self, task_id: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None or task.discard:
                raise HTTPException(404, "Unknown or expired task")
            task.touched = time.monotonic()
            return {"id": task.id, "status": task.status, "progress": task.progress,
                    "result": task.result, "error": task.error}

    def remove(self, task_id: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                self._discard(task)
        return {"ok": True}

    def run(self, task: Task):
        from . import transcriber
        from .app import MODEL_CACHE
        try:
            # The same process owns local and remote GPU work. No second CUDA cache.
            with MODEL_CACHE.activity(), remote_client.local_only(), gpu_turns.batch(cancel_check=lambda: cancellation.check(task.id)):
                cancellation.check(task.id)
                task.status = "processing"
                if task.spec.kind == "whisper":
                    model_key = "ru" if task.spec.model == "ru" else ("large-v3" if task.spec.model == "large-v3" else "turbo")
                    model_path = {"ru": transcriber.RUSSIAN_MODEL_DIR,
                                  "large-v3": transcriber.FULL_MODEL_DIR,
                                  "turbo": transcriber.ENGLISH_MODEL_DIR}[model_key]
                    if not (model_path / "model.bin").is_file():
                        raise RuntimeError("На удалённом компьютере не установлена выбранная модель")
                    model, device = transcriber._load_model(task.spec.model)
                    generator, info = model.transcribe(str(task.path), **task.spec.options)
                    segments = []
                    for segment in generator:
                        cancellation.check(task.id)
                        gpu_turns.checkpoint()
                        # Explicit wire format, independent of namedtuple serialization.
                        segments.append({
                            "start": segment.start, "end": segment.end, "text": segment.text,
                            "avg_logprob": getattr(segment, "avg_logprob", 0),
                            "compression_ratio": getattr(segment, "compression_ratio", 0),
                            "words": [{"start": w.start, "end": w.end, "word": w.word,
                                       "probability": w.probability} for w in segment.words or []],
                        })
                        task.progress = min(0.99, segment.end / max(info.duration, 0.001))
                    result = {"segments": segments, "device": device,
                              "info": {"language": info.language, "language_probability": info.language_probability,
                                       "duration": info.duration}}
                else:
                    if not diarization.engine_available(task.spec.engine):
                        raise RuntimeError("На удалённом компьютере не готов выбранный движок спикеров")
                    transcriber.release_models()
                    turns = (diarization._turns_nemotron(task.path, task.id) if task.spec.engine == "nemotron"
                             else diarization._turns_external(task.path, task.id) if diarization._external_ready()
                             else diarization._turns_local(task.path))
                    result = {"turns": turns}
                cancellation.check(task.id)
                with self.lock:
                    task.result = jsonable_encoder(result)
                    task.status = "completed"
                    task.progress = 1
        except cancellation.CancelledJob:
            task.status = "cancelled"
        except Exception:
            # Exception details can contain paths or worker credentials.
            logging.getLogger("whisper.remote").exception("Remote model failed for %s", task.id)
            task.error = "Ошибка удалённой модели. Проверьте модель и устройство в Whisper на основном компьютере."
            task.status = "failed"
        finally:
            task.folder.cleanup()
            with self.lock:
                if task.discard:
                    self.tasks.pop(task.id, None)
            cancellation.release(task.id)


class HostController:
    def __init__(self):
        self.lock = threading.RLock()
        self.server = None
        self.thread = None
        self.host = None
        self.lease = None
        self.touched = 0
        self.url = ""

    def start(self, address: str, port: int) -> dict:
        ip = ipaddress.ip_address(address)
        if (ip.version != 4 or ip.is_unspecified or ip.is_multicast or not (
                ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10"))):
            raise ValueError("Укажите адрес Tailscale или локальной сети, не 0.0.0.0")
        if remote_client.selected():
            raise ValueError("Сначала выберите локальные вычисления на основном компьютере")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("Адаптер уже запущен или завершает предыдущие задачи")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind((address, port))
                sock.listen(16)
                key = credentials()["token"]
                self.host = ComputeHost(key)
                self.server = uvicorn.Server(uvicorn.Config(self.host.api, host=address, port=port,
                                                           access_log=False, log_level="warning",
                                                           timeout_graceful_shutdown=5))
                self.lease = secrets.token_urlsafe(24)
                self.touched = time.monotonic()
                self.url = f"http://{address}:{port}"
                server, host = self.server, self.host

                def serve():
                    try:
                        server.run(sockets=[sock])
                    finally:
                        host.close()
                        sock.close()

                self.thread = threading.Thread(target=serve, daemon=True, name="remote-listener")
                self.thread.start()
                threading.Thread(target=self.watch, args=(server, host), daemon=True).start()
                return {"lease": self.lease, "url": self.url,
                        "code": remote_client.encode_code(self.url, key)}
            except BaseException:
                sock.close()
                raise

    def watch(self, server, host):
        while not server.should_exit:
            time.sleep(1)
            host.expire()
            if time.monotonic() - self.touched > HOST_LEASE:
                host.close()
                server.should_exit = True

    def heartbeat(self, lease: str):
        with self.lock:
            if not self.lease or not secrets.compare_digest(lease, self.lease):
                raise ValueError("Неверный сеанс адаптера")
            self.touched = time.monotonic()

    def stop(self, lease: str):
        with self.lock:
            self.heartbeat(lease)
            self.host.close()
            self.server.should_exit = True

    def status(self):
        with self.lock:
            return {"running": bool(self.server and self.server.started and not self.server.should_exit),
                    "url": self.url if self.server and not self.server.should_exit else ""}


controller = HostController()
