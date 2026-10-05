"""Пакетное удаление и перенос проектов.

Главное требование: отказ по одному проекту не отменяет остальные. При выделении
десятка проектов один занятый скачиванием не должен блокировать операцию целиком,
но и молча пропасть он тоже не должен — каждый пропуск возвращается с причиной.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_projects_bulk
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as backend_app
from backend import storage


class ProjectsBulkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-bulk-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "TEMP_DIR": storage.TEMP_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
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

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR", "TEMP_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        shutil.rmtree(self._root, ignore_errors=True)

    def _projects(self, count: int) -> list[str]:
        return [
            storage.create_session(f"Проект {index}")["id"]
            for index in range(count)
        ]

    # --- перенос ---

    def test_moves_all_into_folder(self) -> None:
        ids = self._projects(3)
        folder = storage.create_folder("Архив")

        result = backend_app.bulk_move_projects(
            backend_app.ProjectsBulkMove(ids=ids, folder_id=folder["id"])
        )

        self.assertEqual(result["moved"], 3)
        self.assertEqual(result["skipped"], [])
        for project_id in ids:
            self.assertEqual(
                storage.get_session(project_id)["folder_id"], folder["id"]
            )

    def test_moves_out_of_folder(self) -> None:
        folder = storage.create_folder("Архив")
        ids = self._projects(2)
        backend_app.bulk_move_projects(
            backend_app.ProjectsBulkMove(ids=ids, folder_id=folder["id"])
        )

        result = backend_app.bulk_move_projects(
            backend_app.ProjectsBulkMove(ids=ids, folder_id=None)
        )

        self.assertEqual(result["moved"], 2)
        for project_id in ids:
            self.assertIsNone(storage.get_session(project_id)["folder_id"])

    def test_unknown_project_is_skipped_with_reason(self) -> None:
        ids = self._projects(2)
        folder = storage.create_folder("Архив")

        result = backend_app.bulk_move_projects(
            backend_app.ProjectsBulkMove(
                ids=[*ids, "нет-такого"], folder_id=folder["id"]
            )
        )

        self.assertEqual(result["moved"], 2, "остальные должны переехать")
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["id"], "нет-такого")
        self.assertTrue(result["skipped"][0]["reason"])

    # --- удаление ---

    def test_deletes_all(self) -> None:
        ids = self._projects(3)

        result = backend_app.bulk_delete_projects(
            backend_app.ProjectsBulkDelete(ids=ids)
        )

        self.assertEqual(result["deleted"], 3)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(storage.list_sessions(), [])

    def test_deletes_project_folder_on_disk(self) -> None:
        ids = self._projects(1)
        folder = storage.session_dir(ids[0])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "media").mkdir(exist_ok=True)

        backend_app.bulk_delete_projects(
            backend_app.ProjectsBulkDelete(ids=ids)
        )

        self.assertFalse(folder.exists())

    def test_active_download_is_skipped_others_deleted(self) -> None:
        """Занятый скачиванием проект не должен блокировать остальные."""
        ids = self._projects(3)
        with patch.object(
            backend_app.video_downloader,
            "active_project_ids",
            return_value={ids[1]},
        ):
            result = backend_app.bulk_delete_projects(
                backend_app.ProjectsBulkDelete(ids=ids)
            )

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["id"], ids[1])
        self.assertIn("скачивание", result["skipped"][0]["reason"])
        # Пропущенный обязан остаться живым.
        self.assertTrue(storage.get_session(ids[1]))

    def test_unknown_project_does_not_abort_deletion(self) -> None:
        ids = self._projects(2)

        result = backend_app.bulk_delete_projects(
            backend_app.ProjectsBulkDelete(ids=["нет-такого", *ids])
        )

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(len(result["skipped"]), 1)

    def test_busy_project_reports_its_own_reason(self) -> None:
        """Причину отказа даёт хранилище — её нельзя терять по дороге."""
        ids = self._projects(2)
        original = storage.delete_session

        def fake(project_id: str) -> None:
            if project_id == ids[0]:
                raise RuntimeError("Дождитесь окончания распознавания")
            original(project_id)

        with patch.object(storage, "delete_session", side_effect=fake):
            result = backend_app.bulk_delete_projects(
                backend_app.ProjectsBulkDelete(ids=ids)
            )

        self.assertEqual(result["deleted"], 1)
        self.assertIn("распознавания", result["skipped"][0]["reason"])

    def test_empty_list_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            backend_app.ProjectsBulkDelete(ids=[])

    def test_absurdly_long_list_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            backend_app.ProjectsBulkDelete(ids=[f"id{n}" for n in range(501)])


if __name__ == "__main__":
    unittest.main()
