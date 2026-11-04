"""Рабочий поток обязан переживать любую ошибку задачи.

Аудит нашёл путь, на котором поток умирал молча: обработчик ошибок в цикле сам
обращался к базе и ловил только KeyError, поэтому «database is locked» вылетала
наружу, поток-демон завершался, и очередь вставала навсегда. Узнать об этом было
негде — `worker_alive` жил только в /api/health, который интерфейс не запрашивал.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_worker_resilience
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from backend import app as backend_app


class WorkerLoopTests(unittest.TestCase):
    def _run_loop(
        self,
        work_queue: queue.PriorityQueue,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=backend_app.worker_loop,
            args=(work_queue,),
            daemon=True,
        )
        thread.start()
        return thread

    def test_survives_task_error_and_failed_error_write(self) -> None:
        """Задача упала, и запись о провале тоже упала — поток обязан жить."""
        done = threading.Event()
        seen: list[str] = []

        def run_task(task_kind: str, task_id: str) -> None:
            seen.append(task_id)
            if task_id == "boom":
                raise RuntimeError("сбой распознавания")
            done.set()

        def broken_update(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        work_queue: queue.PriorityQueue = queue.PriorityQueue()
        with (
            patch.object(backend_app, "_run_task", side_effect=run_task),
            patch.object(backend_app, "_still_queued", return_value=True),
            patch.object(
                backend_app.storage, "update_job", side_effect=broken_update
            ),
        ):
            thread = self._run_loop(work_queue)
            work_queue.put((1, 1, "project", "boom"))
            work_queue.put((1, 2, "project", "next"))
            self.assertTrue(
                done.wait(5),
                "поток не дошёл до второй задачи — значит умер на первой",
            )
            self.assertTrue(thread.is_alive(), "поток умер")

        self.assertEqual(seen, ["boom", "next"])

    def test_survives_release_error(self) -> None:
        """Сбой в finally тоже не должен ронять цикл."""
        done = threading.Event()

        def run_task(task_kind: str, task_id: str) -> None:
            if task_id == "second":
                done.set()

        work_queue: queue.PriorityQueue = queue.PriorityQueue()
        with (
            patch.object(backend_app, "_run_task", side_effect=run_task),
            patch.object(backend_app, "_still_queued", return_value=True),
            patch.object(
                backend_app.cancellation,
                "release",
                side_effect=RuntimeError("сбой освобождения"),
            ),
        ):
            thread = self._run_loop(work_queue)
            work_queue.put((1, 1, "project", "first"))
            work_queue.put((1, 2, "project", "second"))
            self.assertTrue(done.wait(5))
            self.assertTrue(thread.is_alive())

    def test_missing_task_is_skipped_quietly(self) -> None:
        done = threading.Event()

        def still_queued(task_kind: str, task_id: str) -> bool:
            if task_id == "gone":
                raise KeyError(task_id)
            done.set()
            return False

        work_queue: queue.PriorityQueue = queue.PriorityQueue()
        with patch.object(
            backend_app, "_still_queued", side_effect=still_queued
        ):
            thread = self._run_loop(work_queue)
            work_queue.put((1, 1, "project", "gone"))
            work_queue.put((1, 2, "project", "present"))
            self.assertTrue(done.wait(5))
            self.assertTrue(thread.is_alive())


class WorkerSupervisorTests(unittest.TestCase):
    def test_dead_worker_is_replaced(self) -> None:
        """Сторож поднимает поток заново, если тот всё-таки умер."""
        saved = dict(backend_app.worker_threads)
        backend_app.worker_threads.clear()
        try:
            dead = threading.Thread(target=lambda: None)
            dead.start()
            dead.join()
            backend_app.worker_threads["whisper-worker"] = dead
            self.assertFalse(backend_app.workers_alive())

            backend_app._ensure_workers()

            self.assertTrue(backend_app.workers_alive())
            self.assertIsNot(
                backend_app.worker_threads["whisper-worker"], dead
            )
            self.assertEqual(
                sorted(backend_app.worker_threads),
                ["whisper-dictation", "whisper-worker"],
            )
        finally:
            backend_app.worker_threads.clear()
            backend_app.worker_threads.update(saved)

    def test_alive_workers_are_not_duplicated(self) -> None:
        saved = dict(backend_app.worker_threads)
        backend_app.worker_threads.clear()
        try:
            backend_app._ensure_workers()
            first = dict(backend_app.worker_threads)
            backend_app._ensure_workers()
            self.assertEqual(first, backend_app.worker_threads)
        finally:
            backend_app.worker_threads.clear()
            backend_app.worker_threads.update(saved)


class TemporaryPurgeTests(unittest.TestCase):
    def test_purge_only_removes_studio_scratch_in_custom_directory(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = "a" * 32 + ".wav"
            (root / name).write_bytes(b"user recording")
            (root / "waveforms").mkdir()
            cache = root / "waveforms" / "cached.json"
            cache.write_bytes(b"{}")

            with patch.object(backend_app.storage, "temporary_dir", return_value=root):
                scratch = backend_app.storage.processing_temp_dir()
                (scratch / name).write_bytes(b"interrupted extraction")
                (scratch / "notes.txt").write_bytes(b"user notes")
                self.assertEqual(backend_app._purge_temporary_files(), 1)

            self.assertFalse((scratch / name).exists())
            self.assertEqual((root / name).read_bytes(), b"user recording")
            self.assertEqual((scratch / "notes.txt").read_bytes(), b"user notes")
            self.assertEqual(cache.read_bytes(), b"{}")

    def test_purge_survives_a_missing_directory(self) -> None:
        from pathlib import Path

        with patch.object(
            backend_app.storage,
            "temporary_dir",
            return_value=Path("Z:/нет такого каталога"),
        ):
            self.assertEqual(backend_app._purge_temporary_files(), 0)


if __name__ == "__main__":
    unittest.main()
