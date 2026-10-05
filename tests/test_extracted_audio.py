"""Сохранение извлечённого аудио: настройка, пути, повторная разметка.

Проверяется главное обещание: при включённой настройке аудио остаётся в
проекте и видно по пути, при выключенной — удаляется, как было раньше.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_extracted_audio
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import storage, transcriber


class _Word:
    def __init__(self, start: float, end: float, word: str) -> None:
        self.start = start
        self.end = end
        self.word = word
        self.probability = 0.9


class _Segment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.words = [_Word(start, end, text)]


class _Info:
    language = "ru"
    language_probability = 0.99
    duration = 8.0


class _StubModel:
    def transcribe(self, *args, **kwargs):
        return iter([_Segment(0.0, 4.0, " Тестовая фраза.")]), _Info()


class ExtractedAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-audio-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "TEMP_DIR": storage.TEMP_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
            "_load_model": transcriber._load_model,
            "extract_audio": transcriber.extract_audio,
            # Эти две подменяет только тест повторной разметки, но восстанавливать
            # обязаны все: без этого подмена утекала в test_model_cache и
            # test_media_trim, которые идут следом по алфавиту.
            "release_models": transcriber.release_models,
            "assign_speakers": transcriber.diarization.assign_speakers,
        }
        storage.DATA_DIR = self._root
        storage.DB_PATH = self._root / "test.db"
        storage.UPLOADS_DIR = self._root / "sessions"
        storage.TEMP_DIR = self._root / "temp"
        storage.DEFAULT_SETTINGS["managed_storage_root"] = str(storage.UPLOADS_DIR)
        storage.DEFAULT_SETTINGS["temporary_storage_root"] = str(storage.TEMP_DIR)
        storage.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        storage.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        storage.init_db()
        transcriber._load_model = lambda language: (_StubModel(), "cpu")
        self.extract_calls: list[Path] = []

        def fake_extract(source, stream_index, target, task_id, **kwargs):
            # Настоящий ffmpeg тут не нужен: проверяется размещение файла,
            # а не его содержимое.
            self.extract_calls.append(Path(target))
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(b"RIFF-fake-wav")

        transcriber.extract_audio = fake_extract

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR", "TEMP_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        transcriber._load_model = self._saved["_load_model"]
        transcriber.extract_audio = self._saved["extract_audio"]
        transcriber.release_models = self._saved["release_models"]
        transcriber.diarization.assign_speakers = self._saved["assign_speakers"]
        shutil.rmtree(self._root, ignore_errors=True)

    def _job(self) -> dict:
        session = storage.create_session("Проверка аудио")
        media_dir = storage.session_dir(session["id"]) / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        video = media_dir / "clip.mp4"
        video.write_bytes(b"fake-video")
        return storage.create_job(
            session["id"],
            "clip.mp4",
            video,
            {
                "language": "ru",
                "quality": "accurate",
                "timestamps": "segments",
                "formatting": "formatted",
                "diarization": False,
                "audio_stream_index": 1,
            },
        )

    def test_disabled_by_default_audio_is_deleted(self) -> None:
        self.assertEqual(storage.get_settings()["keep_extracted_audio"], "off")
        job = self._job()
        transcriber.transcribe_job(job["id"])
        result = storage.get_job(job["id"])
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertIsNone(result["extracted_audio_path"])
        # Файл лежал во временной папке и удалён после задания.
        self.assertEqual(len(self.extract_calls), 1)
        self.assertFalse(self.extract_calls[0].exists())
        self.assertEqual(
            self.extract_calls[0].parent.resolve(),
            storage.temporary_dir().resolve(),
        )

    def test_enabled_audio_stays_in_project(self) -> None:
        storage.update_settings({"keep_extracted_audio": "on"})
        job = self._job()
        transcriber.transcribe_job(job["id"])
        result = storage.get_job(job["id"])
        self.assertEqual(result["status"], "completed", result.get("error"))
        kept = Path(result["extracted_audio_path"])
        self.assertTrue(kept.is_file(), "аудио должно остаться на диске")
        # Внутри проекта, а не во временной папке: удаляется вместе с проектом.
        self.assertIn(
            storage.session_dir(job["session_id"]).resolve(),
            kept.resolve().parents,
        )

    def test_rediarization_reuses_kept_audio(self) -> None:
        """Повторная разметка не извлекает дорожку заново."""
        storage.update_settings({"keep_extracted_audio": "on"})
        job = self._job()
        transcriber.transcribe_job(job["id"])
        self.assertEqual(len(self.extract_calls), 1)

        transcriber.diarization.assign_speakers = (
            lambda path, segments, task_id=None, time_offset=0.0, **_: segments
        )
        transcriber.release_models = lambda: 0
        transcriber.diarize_existing_job(job["id"])
        self.assertEqual(
            len(self.extract_calls),
            1,
            "дорожка извлечена повторно, хотя сохранённое аудио было на месте",
        )

    def test_audio_recorded_even_when_recognition_fails(self) -> None:
        """Аудио уже на диске — путь обязан быть виден и после сбоя."""
        storage.update_settings({"keep_extracted_audio": "on"})

        def broken(language):
            raise RuntimeError("модель не загрузилась")

        transcriber._load_model = broken
        job = self._job()
        transcriber.transcribe_job(job["id"])
        result = storage.get_job(job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(Path(result["extracted_audio_path"]).is_file())

    def test_drop_extracted_audio(self) -> None:
        storage.update_settings({"keep_extracted_audio": "on"})
        job = self._job()
        transcriber.transcribe_job(job["id"])
        kept = Path(storage.get_job(job["id"])["extracted_audio_path"])
        self.assertTrue(storage.drop_extracted_audio(job["id"]))
        self.assertFalse(kept.exists())
        self.assertIsNone(storage.get_job(job["id"])["extracted_audio_path"])
        # Повторный вызов уже нечего удалять и не должен падать.
        self.assertFalse(storage.drop_extracted_audio(job["id"]))

    def test_drop_refuses_path_outside_project(self) -> None:
        """Защита от произвольного пути в базе."""
        outside = self._root / "чужой.wav"
        outside.write_bytes(b"data")
        job = self._job()
        storage.set_extracted_audio(job["id"], outside)
        self.assertFalse(storage.drop_extracted_audio(job["id"]))
        self.assertTrue(outside.exists(), "файл вне проекта удалять нельзя")

    def test_project_deletion_removes_kept_audio(self) -> None:
        storage.update_settings({"keep_extracted_audio": "on"})
        job = self._job()
        transcriber.transcribe_job(job["id"])
        kept = Path(storage.get_job(job["id"])["extracted_audio_path"])
        self.assertTrue(kept.is_file())
        storage.delete_session(job["session_id"])
        self.assertFalse(kept.exists())


class ProjectNameFormatTests(unittest.TestCase):
    def test_all_formats_produce_distinct_names(self) -> None:
        names = {
            value: storage._make_project_name("Лекция", value)
            for value in storage.SETTING_VALUES["project_name_format"]
        }
        self.assertEqual(len(set(names.values())), len(names), names)
        for value, name in names.items():
            with self.subTest(value=value):
                self.assertIn("Лекция", name)


if __name__ == "__main__":
    unittest.main()
