from __future__ import annotations

import tempfile
import sqlite3
import unittest
from pathlib import Path

from backend import storage


class DictationContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_connect = storage.connect
        self.connections = []
        self.original_paths = (
            storage.DATA_DIR,
            storage.UPLOADS_DIR,
            storage.TEMP_DIR,
            storage.DICTATIONS_DIR,
            storage.DB_PATH,
        )
        root = Path(self.temp_dir.name)
        storage.DATA_DIR = root / "data"
        storage.UPLOADS_DIR = storage.DATA_DIR / "sessions"
        storage.TEMP_DIR = storage.DATA_DIR / "temp"
        storage.DICTATIONS_DIR = storage.DATA_DIR / "dictations"
        storage.DB_PATH = storage.DATA_DIR / "whisper.db"

        def tracked_connect():
            connection = self.original_connect()
            self.connections.append(connection)
            return connection

        storage.connect = tracked_connect
        storage.init_db()

    def tearDown(self) -> None:
        for connection in self.connections:
            connection.close()
        storage.connect = self.original_connect
        (
            storage.DATA_DIR,
            storage.UPLOADS_DIR,
            storage.TEMP_DIR,
            storage.DICTATIONS_DIR,
            storage.DB_PATH,
        ) = self.original_paths
        self.temp_dir.cleanup()

    def test_continuation_is_joined_before_message_is_confirmed(self) -> None:
        message = storage.create_dictation_message()

        first = storage.create_dictation_fragment(
            "fragment-1",
            message["id"],
            storage.dictation_message_dir(message["id"]) / "fragment-1.webm",
            "audio/webm",
            12.0,
            "ru",
            "accurate",
        )
        storage.complete_dictation_fragment(
            first["id"],
            "Первая часть сообщения.",
            "ru",
            1.0,
        )

        continuation = storage.create_dictation_fragment(
            "fragment-2",
            message["id"],
            storage.dictation_message_dir(message["id"]) / "fragment-2.webm",
            "audio/webm",
            8.0,
            "ru",
            "accurate",
        )
        storage.complete_dictation_fragment(
            continuation["id"],
            "Продолжение сообщения.",
            "ru",
            1.0,
        )

        combined = storage.get_dictation_message(message["id"])
        self.assertEqual([1, 2], [item["position"] for item in combined["fragments"]])
        self.assertEqual(
            "Первая часть сообщения.\n\nПродолжение сообщения.",
            combined["text"],
        )

        confirmed = storage.update_dictation_message(
            message["id"],
            text=combined["text"],
            status="confirmed",
        )
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual(2, len(confirmed["fragments"]))
        self.assertEqual(combined["text"], confirmed["text"])

    def test_cut_text_is_restored_only_on_confirmation_and_audio_is_unchanged(self) -> None:
        message = storage.create_dictation_message()
        paths = []
        for index, transcript in enumerate(["Первый фрагмент.", "Второй фрагмент."]):
            path = storage.dictation_message_dir(message["id"]) / f"{index}.wav"
            path.write_bytes(b"saved audio")
            paths.append(path)
            fragment = storage.create_dictation_fragment(
                f"audio-{index}", message["id"], path, "audio/wav", 1, "ru", "fast"
            )
            storage.complete_dictation_fragment(fragment["id"], transcript, "ru", 1)
        cut = storage.update_dictation_message(message["id"], text="")
        self.assertEqual("", cut["text"])
        confirmed = storage.update_dictation_message(message["id"], text="", status="confirmed")
        self.assertEqual("Первый фрагмент.\n\nВторой фрагмент.", confirmed["text"])
        self.assertEqual(2, len(confirmed["fragments"]))
        self.assertEqual([b"saved audio"] * 2, [path.read_bytes() for path in paths])
        self.assertEqual(confirmed, storage.get_dictation_message(message["id"]))

    def test_confirmation_preserves_manual_text_instead_of_source_transcript(self) -> None:
        message = storage.create_dictation_message()
        path = storage.dictation_message_dir(message["id"]) / "audio.wav"
        fragment = storage.create_dictation_fragment("audio", message["id"], path, "audio/wav", 1, "ru", "fast")
        storage.complete_dictation_fragment(fragment["id"], "Исходная речь.", "ru", 1)
        result = storage.update_dictation_message(message["id"], text="Моя правка", status="confirmed")
        self.assertEqual("Моя правка", result["text"])

    def test_insertion_preference_is_persistent_validated_and_defaults_to_end(self) -> None:
        self.assertEqual("end", storage.get_settings()["dictation_insert_mode"])
        storage.update_settings({"dictation_insert_mode": "cursor"})
        storage.init_db()
        self.assertEqual("cursor", storage.get_settings()["dictation_insert_mode"])
        with self.assertRaises(ValueError):
            storage.update_settings({"dictation_insert_mode": "anywhere"})

    def test_confirmation_does_not_restore_a_deleted_fragment(self) -> None:
        message = storage.create_dictation_message()
        for fid, transcript in [("keep", "Осталось."), ("remove", "Удалено.")]:
            path = storage.dictation_message_dir(message["id"]) / f"{fid}.wav"
            path.write_bytes(b"audio")
            storage.create_dictation_fragment(fid, message["id"], path, "audio/wav", 1, "ru", "fast")
            storage.complete_dictation_fragment(fid, transcript, "ru", 1)
        storage.delete_dictation_fragment("remove")
        storage.update_dictation_message(message["id"], text=" ")
        result = storage.update_dictation_message(message["id"], status="confirmed")
        self.assertEqual("Осталось.", result["text"])
        self.assertEqual(["keep"], [item["id"] for item in result["fragments"]])

    def test_failed_successor_insert_rolls_back_confirmation_and_retry_restores_cut_text(self) -> None:
        mid = storage.create_dictation_message()["id"]
        path = storage.dictation_message_dir(mid) / "audio.wav"
        path.write_bytes(b"fixture audio")
        storage.create_dictation_fragment("audio", mid, path, "audio/wav", 1, "ru", "fast")
        storage.complete_dictation_fragment("audio", "Сохранённая речь.", "ru", 1)
        storage.update_dictation_message(mid, text="", draft_only=True)
        with storage.connect() as db:
            db.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON dictation_messages
                          BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            storage.update_dictation_message(mid, text="", status="confirmed", start_next_draft=True)
        before_retry = storage.get_dictation_message(mid)
        self.assertEqual((before_retry["status"], before_retry["text"]), ("draft", ""))
        with storage.connect() as db:
            db.execute("DROP TRIGGER fail_insert")
        result = storage.update_dictation_message(mid, text="", status="confirmed", start_next_draft=True)
        self.assertEqual(result["text"], "Сохранённая речь.")
        self.assertEqual(result["next_draft"]["status"], "draft")
        self.assertEqual(path.read_bytes(), b"fixture audio")

    def test_lost_ack_retry_preserves_history_edits_and_reuses_its_own_next_draft(self) -> None:
        mid = storage.create_dictation_message()["id"]
        result = storage.update_dictation_message(mid, text="Первое.", status="confirmed", start_next_draft=True)
        successor_id = result["next_draft"]["id"]
        storage.update_dictation_message(successor_id, text="Следующая запись.", draft_only=True)
        storage.create_dictation_message()  # Чужой черновик из другой вкладки.
        storage.update_dictation_message(mid, text="Исправлено в истории.")
        with self.assertRaises(storage.DictationAlreadyConfirmed):
            storage.update_dictation_message(mid, text="", draft_only=True)
        retry = storage.update_dictation_message(mid, text="Первое.", status="confirmed", start_next_draft=True)
        self.assertEqual(retry["text"], "Исправлено в истории.")
        self.assertEqual(retry["confirmed_at"], result["confirmed_at"])
        self.assertEqual(retry["next_draft"]["id"], successor_id)
        self.assertEqual(retry["next_draft"]["text"], "Следующая запись.")
        self.assertEqual(storage.count_dictation_messages(), 3)


if __name__ == "__main__":
    unittest.main()
