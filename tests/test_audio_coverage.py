"""Песня, тихая речь и настоящие повторы не должны исчезать до вывода.

Модель имитирует реальный дефект: VAD отдаёт только начало песни, а запрет
повторных n-грамм подавляет припев. Проверяется весь путь до файлов результата.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import longform, storage, transcriber


class AudioCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="whisper-coverage-"))
        self.patches = [
            patch.object(storage, "DATA_DIR", self.root),
            patch.object(storage, "DB_PATH", self.root / "test.db"),
            patch.object(storage, "UPLOADS_DIR", self.root / "sessions"),
            patch.object(storage, "TEMP_DIR", self.root / "temp"),
            patch.dict(storage.DEFAULT_SETTINGS, {
                "managed_storage_root": str(self.root / "sessions"),
                "temporary_storage_root": str(self.root / "temp"),
            }),
            patch.object(transcriber, "_load_model", return_value=(self, "cpu")),
            patch.object(transcriber, "_clip_duration", return_value=320.0),
        ]
        for item in self.patches:
            item.start()
        storage.init_db()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def transcribe(self, _path, **options):
        texts = [(1, 24, "Первый куплет."), (109, 145, "Второй куплет."),
                 (192, 237, "Третий куплет."), (240, 264, "Настоящий припев."),
                 (270, 302, "Настоящий припев.")]
        if options["vad_filter"]:
            texts = texts[:1]
        if options["no_repeat_ngram_size"]:
            texts = texts[:-1]
        texts.append((314, 315, "Русская устная речь."))
        segments = [SimpleNamespace(
            start=start, end=end, text=text,
            words=[SimpleNamespace(start=start, end=end, word=word,
                                   probability=0.05 if start == 314 else 0.9)
                   for word in text.split()],
        ) for start, end, text in texts]
        info = SimpleNamespace(language="ru", language_probability=1.0, duration=320.0)
        return iter(segments), info

    def test_complete_lyrics_and_repeated_chorus_reach_every_output(self) -> None:
        session = storage.create_session("Проверка песни")
        source = storage.session_dir(session["id"]) / "song.mp3"
        source.write_bytes(b"test")
        job = storage.create_job(session["id"], source.name, source, {
            "language": "ru", "quality": "accurate", "timestamps": "none",
            "formatting": "formatted", "diarization": False,
        })
        transcriber.transcribe_job(job["id"])
        result = storage.get_job(job["id"])
        self.assertEqual(result["status"], "completed", result.get("error"))
        payload = json.loads(Path(result["outputs"]["json"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["duration_seconds"], 320.0)
        self.assertEqual(payload["transcription_profile"], longform.TRANSCRIPTION_PROFILE)
        self.assertEqual(len(payload["segments"]), 5)
        self.assertEqual(len(payload["removed_hallucinations"]), 1)
        self.assertGreater(payload["segments"][-1]["end"], 300)
        for kind in ("txt", "md", "srt", "vtt"):
            text = Path(result["outputs"][kind]).read_text(encoding="utf-8")
            self.assertIn("Первый куплет", text)
            self.assertIn("Второй куплет", text)
            self.assertIn("Третий куплет", text)
            self.assertEqual(text.count("Настоящий припев"), 2)
            self.assertNotIn("Русская устная речь", text)
