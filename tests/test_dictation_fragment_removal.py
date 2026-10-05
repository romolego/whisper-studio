"""Удаление одного фрагмента диктовки.

Раньше избавиться от неудачной записи можно было только удалив всё сообщение —
вместе с остальными четырнадцатью. Здесь проверяется, что уходит ровно один
фрагмент, его текст исчезает из сообщения, а ручные правки остального текста
при этом выживают.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_dictation_fragment_removal
"""
from __future__ import annotations

import shutil
import tempfile
import uuid
import unittest
from pathlib import Path

from backend import storage


class FragmentRemovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-fragments-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "DICTATIONS_DIR": storage.DICTATIONS_DIR,
        }
        storage.DATA_DIR = self._root
        storage.DB_PATH = self._root / "test.db"
        storage.DICTATIONS_DIR = self._root / "dictations"
        storage.DICTATIONS_DIR.mkdir(parents=True, exist_ok=True)
        storage.init_db()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(storage, key, value)
        shutil.rmtree(self._root, ignore_errors=True)

    def _message_with(self, *texts: str) -> tuple[dict, list[dict]]:
        message = storage.create_dictation_message()
        fragments = []
        for index, text in enumerate(texts):
            folder = storage.dictation_message_dir(message["id"])
            folder.mkdir(parents=True, exist_ok=True)
            audio = folder / f"{index}.webm"
            audio.write_bytes(b"audio-bytes")
            fragment = storage.create_dictation_fragment(
                uuid.uuid4().hex,
                message["id"],
                audio,
                "audio/webm",
                5.0,
                "ru",
                "accurate",
            )
            storage.complete_dictation_fragment(fragment["id"], text, "ru", 1.0)
            fragments.append(storage.get_dictation_fragment(fragment["id"]))
        return storage.get_dictation_message(message["id"]), fragments

    def test_removes_only_that_fragment(self) -> None:
        message, fragments = self._message_with("Первый.", "Второй.", "Третий.")
        self.assertEqual(len(message["fragments"]), 3)

        updated = storage.delete_dictation_fragment(fragments[1]["id"])

        self.assertEqual(len(updated["fragments"]), 2)
        ids = [item["id"] for item in updated["fragments"]]
        self.assertEqual(ids, [fragments[0]["id"], fragments[2]["id"]])

    def test_text_loses_the_removed_fragment_only(self) -> None:
        message, fragments = self._message_with("Первый.", "Второй.", "Третий.")
        self.assertIn("Второй.", message["text"])

        updated = storage.delete_dictation_fragment(fragments[1]["id"])

        self.assertNotIn("Второй.", updated["text"])
        self.assertIn("Первый.", updated["text"])
        self.assertIn("Третий.", updated["text"])
        # Разделители не должны превратиться в дыру из пустых строк.
        self.assertNotIn("\n\n\n", updated["text"])

    def test_manual_edits_survive(self) -> None:
        """Текст правится руками — пересобирать его заново нельзя."""
        message, fragments = self._message_with("Первый.", "Второй.", "Третий.")
        storage.update_dictation_message(
            message["id"],
            text="Первый — с моей правкой.\n\nВторой.\n\nТретий, тоже правленый.",
        )

        updated = storage.delete_dictation_fragment(fragments[1]["id"])

        self.assertIn("с моей правкой", updated["text"])
        self.assertIn("тоже правленый", updated["text"])
        self.assertNotIn("Второй.", updated["text"])

    def test_unmatched_transcript_leaves_text_alone(self) -> None:
        """Если правки съели вхождение — лучше не тронуть текст вообще."""
        message, fragments = self._message_with("Первый.", "Второй.")
        storage.update_dictation_message(
            message["id"], text="Полностью переписанный текст."
        )

        updated = storage.delete_dictation_fragment(fragments[1]["id"])

        self.assertEqual(updated["text"], "Полностью переписанный текст.")
        self.assertEqual(len(updated["fragments"]), 1)

    def test_audio_file_is_deleted(self) -> None:
        _message, fragments = self._message_with("Первый.")
        audio = Path(fragments[0]["audio_path"])
        self.assertTrue(audio.is_file())

        storage.delete_dictation_fragment(fragments[0]["id"])

        self.assertFalse(audio.exists())

    def test_audio_outside_dictations_is_not_touched(self) -> None:
        """Защита от произвольного пути в базе."""
        _message, fragments = self._message_with("Первый.")
        outside = self._root / "чужой.webm"
        outside.write_bytes(b"data")
        with storage.connect() as db:
            db.execute(
                "UPDATE dictation_fragments SET audio_path = ? WHERE id = ?",
                (str(outside), fragments[0]["id"]),
            )

        storage.delete_dictation_fragment(fragments[0]["id"])

        self.assertTrue(outside.exists(), "файл вне папки диктовок удалять нельзя")

    def test_unknown_fragment_raises(self) -> None:
        with self.assertRaises(KeyError):
            storage.delete_dictation_fragment("нет-такого")

    def test_removing_the_last_fragment_keeps_the_message(self) -> None:
        message, fragments = self._message_with("Единственный.")

        updated = storage.delete_dictation_fragment(fragments[0]["id"])

        self.assertEqual(updated["id"], message["id"])
        self.assertEqual(updated["fragments"], [])
        self.assertEqual(updated["text"], "")


class TextSurgeryTests(unittest.TestCase):
    """Точность вырезания — отдельно от базы."""

    def test_removes_middle(self) -> None:
        result = storage._text_without_fragment("А\n\nБ\n\nВ", "Б")
        self.assertEqual(result, "А\n\nВ")

    def test_removes_first(self) -> None:
        result = storage._text_without_fragment("А\n\nБ", "А")
        self.assertEqual(result, "Б")

    def test_removes_last(self) -> None:
        result = storage._text_without_fragment("А\n\nБ", "Б")
        self.assertEqual(result, "А")

    def test_keeps_text_when_not_found(self) -> None:
        result = storage._text_without_fragment("А\n\nБ", "Я")
        self.assertEqual(result, "А\n\nБ")

    def test_empty_transcript_changes_nothing(self) -> None:
        self.assertEqual(storage._text_without_fragment("А", ""), "А")

    def test_removes_only_first_occurrence(self) -> None:
        """Повторяющаяся реплика: уходит одна копия, а не все."""
        result = storage._text_without_fragment("Да.\n\nДа.\n\nНет.", "Да.")
        self.assertEqual(result, "Да.\n\nНет.")


if __name__ == "__main__":
    unittest.main()
