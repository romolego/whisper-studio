from __future__ import annotations

import queue
import unittest
from unittest.mock import patch

from backend import app as backend_app


def job(job_id: str, status: str, name: str) -> dict:
    return {
        "id": job_id,
        "session_id": "project-1",
        "original_name": name,
        "status": status,
        "options": {},
    }


class QueueControlTests(unittest.TestCase):
    @patch.object(backend_app.cancellation, "requested_ids", return_value=set())
    @patch.object(backend_app.storage, "list_dictation_messages")
    @patch.object(backend_app.storage, "list_sessions")
    @patch.object(backend_app.storage, "list_jobs")
    def test_overview_uses_real_priority_queue_order(
        self,
        list_jobs,
        list_sessions,
        list_dictation_messages,
        _requested_ids,
    ) -> None:
        list_jobs.return_value = [
            job("active", "processing", "Сейчас.mp3"),
            job("regular", "queued", "Обычный.mp3"),
            job("promoted", "queued", "Срочный.mp3"),
        ]
        list_sessions.return_value = [
            {"id": "project-1", "name": "Проверка очереди"},
        ]
        list_dictation_messages.return_value = [
            {
                "fragments": [
                    {
                        "id": "dictation",
                        "position": 2,
                        "status": "queued",
                    },
                ],
            },
        ]

        # Очередей теперь две, и диктовка идёт впереди даже поднятого вручную
        # файла: её обрабатывает отдельный поток, она не ждёт файлы вообще.
        # Раньше при одной очереди порядок был promoted, dictation, regular.
        original_batch = backend_app.batch_queue
        original_dictation = backend_app.dictation_queue
        backend_app.batch_queue = queue.PriorityQueue()
        backend_app.dictation_queue = queue.PriorityQueue()
        try:
            backend_app.batch_queue.put((1, 1, "project", "promoted"))
            backend_app.dictation_queue.put((0, 2, "dictation", "dictation"))
            backend_app.batch_queue.put((1, 3, "project", "regular"))
            backend_app.batch_queue.put((-1, 4, "project", "promoted"))

            with patch.object(backend_app, "_ensure_workers"):
                overview = backend_app.queue_overview()
        finally:
            backend_app.batch_queue = original_batch
            backend_app.dictation_queue = original_dictation

        self.assertEqual("active", overview["active"]["id"])
        self.assertEqual(
            ["dictation", "promoted", "regular"],
            [item["id"] for item in overview["waiting"]],
        )
        self.assertEqual([1, 2, 3], [item["position"] for item in overview["waiting"]])
        # Поднятый вручную файл по-прежнему опережает обычный внутри батча.
        self.assertTrue(overview["waiting"][1]["priority"])
        self.assertFalse(overview["waiting"][2]["priority"])
        self.assertEqual(4, overview["total"])

    def test_dictation_goes_to_its_own_queue(self) -> None:
        """Диктовка и файлы физически в разных очередях."""
        original_batch = backend_app.batch_queue
        original_dictation = backend_app.dictation_queue
        backend_app.batch_queue = queue.PriorityQueue()
        backend_app.dictation_queue = queue.PriorityQueue()
        try:
            backend_app.enqueue_project_job("job-1")
            backend_app.enqueue_diarization_job("job-2")
            backend_app.enqueue_dictation_fragment("fragment-1")

            self.assertEqual(backend_app.batch_queue.qsize(), 2)
            self.assertEqual(backend_app.dictation_queue.qsize(), 1)

            # Поднятие тоже попадает в свою очередь, а не в чужую.
            backend_app._promote_queued_task("dictation", "fragment-1")
            self.assertEqual(backend_app.dictation_queue.qsize(), 2)
            self.assertEqual(backend_app.batch_queue.qsize(), 2)
        finally:
            backend_app.batch_queue = original_batch
            backend_app.dictation_queue = original_dictation

    @patch.object(backend_app, "queue_overview", return_value={"waiting": []})
    @patch.object(backend_app, "_promote_queued_task")
    @patch.object(backend_app.storage, "get_job")
    def test_prioritize_job_promotes_only_queued_task(
        self,
        get_job,
        promote,
        _overview,
    ) -> None:
        get_job.return_value = job("job-1", "queued", "Файл.mp3")

        result = backend_app.prioritize_job("job-1")

        promote.assert_called_once_with("project", "job-1")
        self.assertEqual({"waiting": []}, result)


if __name__ == "__main__":
    unittest.main()
