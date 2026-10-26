"""Telegram omits synthetic prompts; Studio keeps its punctuation context."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import dictation, storage, transcriber


class _Info:
    language = "ru"
    language_probability = 0.99
    duration = 6.0


class _Segment:
    def __init__(self) -> None:
        self.start = 0.0
        self.end = 4.0
        self.text = " Проверка связи."
        self.words = []


class _SpyModel:
    """Запоминает, с какими параметрами позвали transcribe."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, *args, **kwargs):
        self.calls.append(kwargs)
        return iter([_Segment()]), _Info()


class PromptTextTests(unittest.TestCase):
    def test_language_specific_text(self) -> None:
        self.assertIn("русский", transcriber.punctuation_prompt("ru").lower())
        self.assertIn("english", transcriber.punctuation_prompt("en").lower())
        # Автоопределение тоже обязано получить инструкцию, а не пустоту.
        self.assertTrue(transcriber.punctuation_prompt("auto"))

    def test_dictation_uses_the_same_text(self) -> None:
        """Одна формулировка на два места: копии разошлись бы со временем."""
        for language in ("ru", "en", "auto"):
            with self.subTest(language=language):
                self.assertEqual(
                    dictation._dictation_initial_prompt(language),
                    transcriber.punctuation_prompt(language),
                )


class TranscribeCallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-punct-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "TEMP_DIR": storage.TEMP_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
            "_load_model": transcriber._load_model,
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
        self.model = _SpyModel()
        transcriber._load_model = lambda language: (self.model, "cpu")
        from unittest.mock import patch
        self.punctuation = patch.object(transcriber.punctuation, "restore", side_effect=lambda segments, _: segments)
        self.punctuation.start()
        self.speech = patch.object(transcriber.speech_profile, "transcribe", side_effect=lambda model, path, options, _: model.transcribe(path, **options))
        self.speech.start()

    def tearDown(self) -> None:
        self.punctuation.stop()
        self.speech.stop()
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR", "TEMP_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        transcriber._load_model = self._saved["_load_model"]
        shutil.rmtree(self._root, ignore_errors=True)

    def _run(self, language: str = "ru", formatting: str = "formatted", source: str = "", model: str = "studio") -> dict:
        session = storage.create_session("Проверка пунктуации")
        media_dir = storage.session_dir(session["id"]) / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        audio = media_dir / "clip.wav"
        audio.write_bytes(b"RIFF")
        job = storage.create_job(
            session["id"],
            "clip.wav",
            audio,
            {
                "language": language,
                "quality": "accurate",
                "model": model,
                "timestamps": "segments",
                "formatting": formatting,
                "diarization": False,
                "integration_source": source,
            },
        )
        transcriber.transcribe_job(job["id"])
        self.assertEqual(len(self.model.calls), 1)
        return self.model.calls[0]

    def test_telegram_does_not_receive_synthetic_text(self) -> None:
        call = self._run(source="telegram")
        self.assertFalse(call.get("hotwords"))
        self.assertFalse(call.get("initial_prompt"))

    def test_context_carryover_stays_off(self) -> None:
        """Инструкция не должна протаскиваться ценой переноса контекста.

        condition_on_previous_text=True тоже даёт пунктуацию, но на замере был
        на 23% медленнее и возвращает риск зацикливания, от которого код ушёл
        намеренно.
        """
        call = self._run()
        self.assertFalse(call["condition_on_previous_text"])

    def test_english_telegram_also_has_no_instruction(self) -> None:
        call = self._run(language="en", source="telegram")
        self.assertFalse(call.get("hotwords"))

    def test_plain_output_also_has_no_instruction(self) -> None:
        call = self._run(formatting="plain", source="telegram")
        self.assertFalse(call.get("hotwords"))

    def test_telegram_detects_speech_but_studio_keeps_full_audio(self) -> None:
        call = self._run(source="telegram")
        self.assertTrue(call["vad_filter"])
        self.assertLess(call["vad_parameters"]["threshold"], 0.5)
        self.assertGreaterEqual(call["vad_parameters"]["speech_pad_ms"], 400)

    def test_studio_keeps_existing_punctuation_context_and_full_audio(self) -> None:
        call = self._run()
        self.assertEqual(call["hotwords"], transcriber.punctuation_prompt("ru"))
        self.assertFalse(call["vad_filter"])

    def test_full_model_prioritizes_accuracy_without_echo_prone_hotwords(self) -> None:
        call = self._run(source="telegram", model="large-v3")
        self.assertEqual(call["beam_size"], 10)
        self.assertEqual(call["patience"], 2.0)
        self.assertFalse(call["condition_on_previous_text"], "Pauses must not inherit repeated text")
        self.assertTrue(call["vad_filter"])
        self.assertEqual(call["vad_parameters"]["threshold"], 0.2)
        self.assertEqual(call["temperature"], 0.0)
        self.assertFalse(call.get("hotwords"))
        self.assertFalse(call.get("initial_prompt"))


if __name__ == "__main__":
    unittest.main()
