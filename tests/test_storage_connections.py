import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import storage


class StorageConnectionTests(unittest.TestCase):
    def test_context_commits_and_releases_database_handle(self):
        work = Path(__file__).resolve().parents[1] / "run"
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as directory, patch.object(storage, "DATA_DIR", Path(directory)), patch.object(storage, "DB_PATH", Path(directory) / "test.db"):
            with storage.connect() as db:
                db.execute("CREATE TABLE items (value TEXT)")
                db.execute("INSERT INTO items VALUES ('committed')")
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
            with storage.connect() as db:
                self.assertEqual(db.execute("SELECT value FROM items").fetchone()[0], "committed")
            with self.assertRaises(ValueError), storage.connect() as db:
                db.execute("INSERT INTO items VALUES ('rolled back')")
                raise ValueError("rollback")
            with storage.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
