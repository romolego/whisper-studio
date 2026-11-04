import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from backend import app, storage
from tests.test_project_names import _StorageCase


class BatchImportTests(_StorageCase):
    def setUp(self):
        super().setUp()
        self.project = storage.create_session("Batch")["id"]
        self.metadata = dict(size_bytes=5, duration_seconds=1, format_name="wav", audio_streams=[], video_stream_count=0)

    def test_failed_upload_preserves_existing_files_and_retry_imports_once(self):
        folder = storage.session_dir(self.project) / "media"
        folder.mkdir(exist_ok=True)
        original = folder / "one.wav"
        original.write_bytes(b"original")

        def upload():
            parts = [UploadFile(io.BytesIO(b"audio"), filename=name) for name in ("one.wav", "two.wav")]
            return app.upload_media(parts, self.project)

        with patch.object(app.media, "probe_media", side_effect=[self.metadata, RuntimeError("bad file")]):
            with self.assertRaises(HTTPException):
                upload()
        self.assertEqual(storage.list_media(self.project), [])
        self.assertEqual(list(folder.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b"original")
        with patch.object(app.media, "probe_media", return_value=self.metadata):
            self.assertEqual(len(upload()), 2)
        self.assertEqual(len(storage.list_media(self.project)), 2)

    def test_reference_batch_rolls_back_when_second_database_insert_fails(self):
        paths = [self._root / name for name in ("one.wav", "two.wav")]
        for path in paths:
            path.write_bytes(b"owner")
        with storage.connect() as db:
            db.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON media_uploads
                          WHEN NEW.original_name = 'two.wav'
                          BEGIN SELECT RAISE(ABORT, 'second insert failed'); END""")
        with patch.object(app, "_run_picker", return_value=json.dumps([str(p) for p in paths])), \
                patch.object(app.media, "probe_media", return_value=self.metadata):
            with self.assertRaises(sqlite3.IntegrityError):
                app.pick_local_files(app.LocalFilesPick(session_id=self.project))
        self.assertEqual(storage.list_media(self.project), [])
        self.assertEqual([p.read_bytes() for p in paths], [b"owner", b"owner"])

    def test_rename_during_import_keeps_registered_copy_accessible(self):
        rename_started = threading.Event()
        sync = storage.sync_session_dir

        def rename_directory(project_id):
            rename_started.set()
            return sync(project_id)

        with ThreadPoolExecutor(max_workers=1) as pool:
            def probe(_path):
                pending = pool.submit(storage.rename_session, self.project, "Renamed")
                self.assertTrue(rename_started.wait(5))
                # Без защиты каталога переезд успевает до записи пути в базу.
                try:
                    pending.result(timeout=0.3)
                except TimeoutError:
                    pass
                return self.metadata

            with patch.object(storage, "sync_session_dir", side_effect=rename_directory), \
                    patch.object(app.media, "probe_media", side_effect=probe):
                part = UploadFile(io.BytesIO(b"audio"), filename="sample.wav")
                app.upload_media([part], self.project)
        item, = storage.list_media(self.project)
        self.assertEqual((storage.session_dir(self.project) / "media" / "sample.wav").read_bytes(), b"audio")
        self.assertEqual(item["stored_path"], str(storage.session_dir(self.project) / "media" / "sample.wav"))
