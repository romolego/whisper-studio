"""Real HTTP round trips with isolated settings and fake inference, no user data."""
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import requests
import uvicorn
from fastapi import FastAPI

from backend import cancellation, diarization, gpu_turns, remote_api, remote_client, remote_host, storage, transcriber


def wav_bytes(seconds=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\0\0" * (16000 * seconds))
    return buffer.getvalue()


class HTTPServer:
    def __init__(self, app):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(16)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Test HTTP server did not start")
            time.sleep(0.01)
        return self

    def __exit__(self, *args):
        self.server.should_exit = True
        self.thread.join(10)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("Test HTTP server did not stop")


class RemoteComputeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "run", prefix="remote-test-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.patches = [
            patch.object(remote_client, "CONFIG_PATH", self.root / "client.json"),
            patch.object(remote_host, "HOST_CONFIG", self.root / "host.json"),
            patch.object(storage, "temporary_dir", return_value=self.root),
            patch.object(transcriber, "RUSSIAN_MODEL_DIR", self.root / "model"),
            patch.object(transcriber, "_models", {}),
            patch.object(transcriber, "_model_devices", {}),
            patch.object(transcriber.runtime, "preferred_device", return_value="cpu"),
            patch.object(transcriber, "cpu_threads", return_value=1),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        (self.root / "model").mkdir()
        (self.root / "model" / "model.bin").touch()
        self.audio = self.root / "source.wav"
        self.audio.write_bytes(wav_bytes())
        self.host = remote_host.ComputeHost("x" * 43)
        self.addCleanup(self.host.close)
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        self.options = []

        outer = self

        class Model:
            def transcribe(self, path, **options):
                with wave.open(str(path), "rb") as handle:
                    outer.assertEqual(handle.getframerate(), 16000)
                outer.options.append(options)
                segment = SimpleNamespace(start=0.1, end=0.9, text=" Test.", avg_logprob=-0.3,
                                          compression_ratio=1.0,
                                          words=[SimpleNamespace(start=0.1, end=0.9, word=" Test.", probability=0.9)])
                return iter([segment]), SimpleNamespace(language="en", language_probability=0.99, duration=1.0)

        item = patch.object(transcriber, "WhisperModel", return_value=Model())
        self.model_constructor = item.start()
        self.addCleanup(item.stop)

    def config(self, server):
        return {"mode": "remote", "url": server.url, "token": self.host.token}

    def test_defaults_and_saved_key_survive_local_switch(self):
        self.assertIsNone(remote_client.selected())
        self.assertFalse(remote_host.controller.status()["running"])
        value = {"mode": "remote", "url": "http://100.100.0.1:8766", "token": "a" * 43}
        remote_client.save_config(value)
        self.assertEqual(remote_client.selected(), value)
        remote_client.save_config({**value, "mode": "local"})
        self.assertIsNone(remote_client.selected())
        self.assertEqual(remote_client.read_config()["token"], value["token"])
        code = remote_client.encode_code(value["url"], value["token"])
        self.assertEqual(remote_client.decode_code(code), {key: value[key] for key in ("url", "token")})
        for invalid in ("bad", remote_client.encode_code("http://8.8.8.8:8766", "a" * 43),
                        remote_client.encode_code("http://0.0.0.0:8766", "a" * 43)):
            with self.assertRaises(ValueError):
                remote_client.decode_code(invalid)

    def test_authenticated_http_inference_and_word_timestamps(self):
        with HTTPServer(self.host.api) as server:
            config = self.config(server)
            model = remote_client.RemoteModel("en", config)
            generator, info = model.transcribe(self.audio, language="en", beam_size=3, word_timestamps=True)
            segments = list(generator)
            self.assertEqual(segments[0].words[0].word, " Test.")
            self.assertEqual(segments[0].avg_logprob, -0.3)
            self.assertEqual(info.language, "en")
            self.assertEqual(model.remote_device, "remote_cpu")
            self.assertEqual(self.options[0]["beam_size"], 3)
            self.assertEqual(self.host.tasks, {})
            self.assertEqual(list((self.root / "remote-work").iterdir()), [])

    def test_array_audio_for_full_speech_profile(self):
        import numpy as np
        with HTTPServer(self.host.api) as server:
            model = remote_client.RemoteModel("ru", self.config(server))
            generator, _ = model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=5)
            self.assertEqual(len(list(generator)), 1)

    def test_missing_key_wrong_key_and_browser_origin_are_rejected(self):
        with HTTPServer(self.host.api) as server:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                response = self.session.post(server.url + "/v1/tasks", headers=headers, data=b"ignored")
                self.assertEqual(response.status_code, 401)
            headers = {"Authorization": "Bearer " + self.host.token}
            self.assertEqual(self.session.get(server.url + "/api/settings", headers=headers).status_code, 404)
            self.assertEqual(self.session.get(server.url + "/docs", headers=headers).status_code, 404)
            self.assertEqual(self.session.get(server.url + "/v1/status", headers={**headers, "Origin": "http://evil.test"}).status_code, 403)
            for spec, body in (({"kind": "whisper", "options": {"beam_size": "bad"}}, wav_bytes()),
                               ({"kind": "whisper"}, b"not a WAV")):
                response = self.session.post(server.url + "/v1/tasks", data=body,
                    headers={**headers, "X-Whisper-Spec": json.dumps(spec)})
                self.assertEqual(response.status_code, 422)
            self.assertEqual(self.host.tasks, {})

    def test_local_controls_do_not_return_key_and_reject_foreign_origin(self):
        app = FastAPI()
        app.include_router(remote_api.router)
        remote_client.save_config({"mode": "local", "token": "secret", "url": "http://100.100.0.1:8766"})
        with HTTPServer(app) as server:
            response = self.session.get(server.url + "/api/remote/client")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("secret", response.text)
            self.assertEqual(self.session.post(server.url + "/api/remote/host/stop").status_code, 200)
            response = self.session.put(server.url + "/api/remote/client", json={"mode": "local"},
                                        headers={"Origin": "https://evil.test"})
            self.assertEqual(response.status_code, 403)

    def test_no_local_model_loaded_when_remote_unavailable(self):
        remote_client.save_config({"mode": "remote", "token": "x" * 43, "url": "http://127.0.0.1:1"})
        model, device = transcriber._load_model("ru")
        self.assertEqual(device, "remote")
        with self.assertRaisesRegex(RuntimeError, "недоступен"):
            model.transcribe(self.audio)
        self.model_constructor.assert_not_called()

    def test_remote_diarization_preserves_trim_offset_and_skips_local_worker(self):
        with HTTPServer(self.host.api) as server:
            remote_client.save_config(self.config(server))
            with patch.object(diarization, "engine_available", return_value=True), \
                 patch.object(diarization, "_turns_nemotron", return_value=[(0.0, 1.0, "speaker_0")]) as worker:
                result = diarization.assign_speakers(self.audio, [{"start": 10.1, "end": 10.9, "text": "Test"}],
                                                     time_offset=10, engine="nemotron")
                self.assertEqual(result[0]["speaker"], "speaker_0")
                self.assertEqual(worker.call_count, 1)

    def test_cancelled_remote_job_releases_audio_and_gpu(self):
        reached = threading.Event()
        resume = threading.Event()

        class SlowModel:
            def transcribe(self, path, **options):
                def generate():
                    reached.set()
                    resume.wait(5)
                    yield SimpleNamespace(start=0, end=1, text="Test", words=[])
                return generate(), SimpleNamespace(duration=1, language="en", language_probability=1)

        with patch.object(transcriber, "WhisperModel", return_value=SlowModel()), HTTPServer(self.host.api) as server:
            headers = {"Authorization": "Bearer " + self.host.token, "X-Whisper-Spec": '{"kind":"whisper"}'}
            response = self.session.post(server.url + "/v1/tasks", data=wav_bytes(), headers=headers)
            task_id = response.json()["id"]
            self.assertTrue(reached.wait(5))
            self.session.delete(server.url + "/v1/tasks/" + task_id, headers=headers)
            resume.set()
            deadline = time.monotonic() + 5
            while self.host.tasks and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.host.tasks, {})
            self.assertEqual(gpu_turns.state()["batch_active"], 0)
            self.assertEqual(list((self.root / "remote-work").iterdir()), [])

    def test_expired_host_lease_closes_listener(self):
        controller = remote_host.HostController()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        value = controller.start("127.0.0.1", port)
        self.assertEqual(remote_client.decode_code(value["code"])["token"], remote_host.credentials()["token"])
        controller.touched = time.monotonic() - remote_host.HOST_LEASE - 1
        controller.thread.join(5)
        self.assertFalse(controller.thread.is_alive())
        self.assertFalse(controller.status()["running"])

    def test_job_scope_freezes_target_until_job_finishes(self):
        remote_client.save_config({"mode": "remote", "url": "http://100.100.0.1:8766", "token": "a" * 43})

        @remote_client.task_scope
        def task(task_id):
            remote_client.save_config({"mode": "local"})
            self.assertIsNotNone(remote_client.selected())
            with remote_client.local_only():
                self.assertIsNone(remote_client.selected())
            self.assertIsNotNone(remote_client.selected())

        task("test")
        self.assertIsNone(remote_client.selected())


if __name__ == "__main__":
    unittest.main()
