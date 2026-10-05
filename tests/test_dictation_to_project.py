"""Сохранение диктовки в проект.

Смысл: важное рассуждение, надиктованное на пятнадцать минут, должно переживать
очистку истории диктовок. Поэтому аудио копируется, а не берётся ссылкой, и сама
диктовка из истории не исчезает.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_dictation_to_project
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from backend import app as backend_app
from backend import storage


class DictationToProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-to-project-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "TEMP_DIR": storage.TEMP_DIR,
            "DICTATIONS_DIR": storage.DICTATIONS_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
        }
        storage.DATA_DIR = self._root
        storage.DB_PATH = self._root / "test.db"
        storage.UPLOADS_DIR = self._root / "sessions"
        storage.TEMP_DIR = self._root / "temp"
        storage.DICTATIONS_DIR = self._root / "dictations"
        storage.DEFAULT_SETTINGS["managed_storage_root"] = str(storage.UPLOADS_DIR)
        storage.DEFAULT_SETTINGS["temporary_storage_root"] = str(storage.TEMP_DIR)
        for folder in (storage.UPLOADS_DIR, storage.TEMP_DIR, storage.DICTATIONS_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        storage.init_db()

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR", "TEMP_DIR", "DICTATIONS_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        shutil.rmtree(self._root, ignore_errors=True)

    def _dictation(self, *texts: str, statuses=None) -> dict:
        message = storage.create_dictation_message()
        folder = storage.dictation_message_dir(message["id"])
        folder.mkdir(parents=True, exist_ok=True)
        for index, text in enumerate(texts):
            audio = folder / f"{index}.webm"
            audio.write_bytes(b"audio-bytes-" + str(index).encode())
            fragment = storage.create_dictation_fragment(
                uuid.uuid4().hex,
                message["id"],
                audio,
                "audio/webm",
                12.5,
                "ru",
                "accurate",
            )
            state = (statuses or {}).get(index, "completed")
            if state == "completed":
                storage.complete_dictation_fragment(
                    fragment["id"], text, "ru", 2.0
                )
            else:
                storage.update_dictation_fragment(fragment["id"], status=state)
        return storage.get_dictation_message(message["id"])

    def test_creates_project_with_one_file_per_fragment(self) -> None:
        message = self._dictation("Первая мысль.", "Вторая мысль.")

        result = backend_app.dictation_to_project(
            message["id"], backend_app.DictationToProject(name="Важное")
        )

        self.assertEqual(result["files"], 2)
        # Название проходит через шаблон из настроек, как и у всех проектов,
        # поэтому проверяется вхождение, а не полное совпадение.
        self.assertIn("Важное", result["project"]["name"])
        jobs = storage.list_jobs(result["project"]["id"])
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(job["status"] == "completed" for job in jobs))

    def test_all_five_formats_are_written(self) -> None:
        message = self._dictation("Одна мысль.")
        result = backend_app.dictation_to_project(
            message["id"], backend_app.DictationToProject(name="Проект")
        )
        job = storage.list_jobs(result["project"]["id"])[0]
        for kind in ("txt", "docx", "srt", "vtt", "json"):
            with self.subTest(kind=kind):
                path = job["outputs"].get(kind)
                self.assertTrue(path, f"нет {kind}")
                self.assertTrue(Path(path).is_file())
        payload = json.loads(
            Path(job["outputs"]["json"]).read_text(encoding="utf-8")
        )
        self.assertEqual(payload["segments"][0]["text"], "Одна мысль.")
        self.assertAlmostEqual(payload["segments"][0]["end"], 12.5)

    def test_audio_is_copied_not_referenced(self) -> None:
        """Диктовку можно удалить — в проекте запись обязана остаться."""
        message = self._dictation("Мысль.")
        result = backend_app.dictation_to_project(
            message["id"], backend_app.DictationToProject()
        )
        job = storage.list_jobs(result["project"]["id"])[0]
        stored = Path(job["stored_path"])
        self.assertTrue(stored.is_file())

        storage.delete_dictation_message(message["id"])

        self.assertTrue(stored.is_file(), "аудио в проекте не должно исчезать")

    def test_dictation_stays_in_history_and_is_marked(self) -> None:
        message = self._dictation("Мысль.")
        result = backend_app.dictation_to_project(
            message["id"], backend_app.DictationToProject()
        )
        again = storage.get_dictation_message(message["id"])
        self.assertEqual(again["project_id"], result["project"]["id"])
        self.assertEqual(len(again["fragments"]), 1)

    def test_only_completed_fragments_travel(self) -> None:
        message = self._dictation(
            "Готовая.", "", statuses={1: "failed"}
        )
        result = backend_app.dictation_to_project(
            message["id"], backend_app.DictationToProject()
        )
        self.assertEqual(result["files"], 1)

    def test_refuses_when_nothing_is_recognised(self) -> None:
        message = self._dictation("", statuses={0: "failed"})
        with self.assertRaises(Exception) as caught:
            backend_app.dictation_to_project(
                message["id"], backend_app.DictationToProject()
            )
        self.assertIn("нет распознанных", str(caught.exception))

    def test_manual_edits_are_reported_not_hidden(self) -> None:
        """Правки текста по фрагментам не раскладываются — надо сказать прямо."""
        message = self._dictation("Первая.", "Вторая.")
        storage.update_dictation_message(
            message["id"], text="Полностью переписал руками."
        )
        result = backend_app.dictation_to_project(
            message["id"], backend_app.DictationToProject()
        )
        self.assertTrue(result["text_edited"])

    def test_untouched_text_is_not_reported_as_edited(self) -> None:
        message = self._dictation("Первая.", "Вторая.")
        result = backend_app.dictation_to_project(
            message["id"], backend_app.DictationToProject()
        )
        self.assertFalse(result["text_edited"])

    def test_unknown_dictation_is_404(self) -> None:
        with self.assertRaises(Exception) as caught:
            backend_app.dictation_to_project(
                "нет-такой", backend_app.DictationToProject()
            )
        self.assertIn("не найдена", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
