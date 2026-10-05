from __future__ import annotations

import tempfile
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


if __name__ == "__main__":
    unittest.main()
