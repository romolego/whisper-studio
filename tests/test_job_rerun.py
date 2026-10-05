from __future__ import annotations

import unittest
from unittest.mock import patch

from backend import app as backend_app


class JobRerunEndpointTests(unittest.TestCase):
    @patch.object(backend_app, "_with_queue_positions")
    @patch.object(backend_app, "enqueue_project_job")
    @patch.object(backend_app.storage, "prepare_job_rerun")
    @patch.object(backend_app.diarization_service, "engine_available")
    @patch.object(backend_app.diarization_service, "default_engine")
    def test_full_rerun_preserves_selected_options(
        self,
        default_engine,
        engine_available,
        prepare_job_rerun,
        enqueue_project_job,
        with_positions,
    ) -> None:
        default_engine.return_value = "pyannote"
        engine_available.return_value = True
        prepared = {"id": "job-1", "status": "queued"}
        prepare_job_rerun.return_value = prepared
        with_positions.return_value = [prepared]

        result = backend_app.rerun_completed_job(
            "job-1",
            backend_app.JobRerun(
                language="ru",
                quality="accurate",
                diarization=True,
            ),
        )

        self.assertEqual(result["status"], "queued")
        prepare_job_rerun.assert_called_once_with(
            "job-1",
            {
                "language": "ru",
                "quality": "accurate",
                "diarization": True,
                "diarization_engine": "pyannote",
            },
            "transcribe",
        )
        enqueue_project_job.assert_called_once_with("job-1")

    @patch.object(backend_app, "_with_queue_positions")
    @patch.object(backend_app, "enqueue_diarization_job")
    @patch.object(backend_app.storage, "prepare_job_rerun")
    @patch.object(backend_app.storage, "get_job")
    @patch.object(backend_app.diarization_service, "engine_available")
    @patch.object(backend_app.diarization_service, "default_engine")
    @patch.object(backend_app.Path, "is_file", return_value=True)
    def test_diarization_only_uses_existing_transcript(
        self,
        _is_file,
        default_engine,
        engine_available,
        get_job,
        prepare_job_rerun,
        enqueue_diarization_job,
        with_positions,
    ) -> None:
        default_engine.return_value = "pyannote"
        engine_available.return_value = True
        get_job.return_value = {
            "id": "job-2",
            "outputs": {"json": "existing.json"},
        }
        prepared = {"id": "job-2", "status": "queued"}
        prepare_job_rerun.return_value = prepared
        with_positions.return_value = [prepared]

        result = backend_app.diarize_completed_job("job-2")

        self.assertEqual(result["status"], "queued")
        prepare_job_rerun.assert_called_once_with(
            "job-2",
            {"diarization": True, "diarization_engine": "pyannote"},
            "diarization",
        )
        enqueue_diarization_job.assert_called_once_with("job-2")

    @patch.object(backend_app, "_with_queue_positions")
    @patch.object(backend_app, "enqueue_diarization_job")
    @patch.object(backend_app.storage, "prepare_job_rerun")
    @patch.object(backend_app.storage, "get_job")
    @patch.object(backend_app.diarization_service, "engine_available")
    @patch.object(backend_app.Path, "is_file", return_value=True)
    def test_rediarization_with_chosen_engine(
        self,
        _is_file,
        engine_available,
        get_job,
        prepare_job_rerun,
        enqueue_diarization_job,
        with_positions,
    ) -> None:
        """Готовый файл можно разметить заново другим движком."""
        engine_available.return_value = True
        get_job.return_value = {"id": "job-3", "outputs": {"json": "x.json"}}
        prepared = {"id": "job-3", "status": "queued"}
        prepare_job_rerun.return_value = prepared
        with_positions.return_value = [prepared]

        backend_app.diarize_completed_job(
            "job-3", backend_app.JobDiarize(engine="nemotron")
        )

        engine_available.assert_called_with("nemotron")
        prepare_job_rerun.assert_called_once_with(
            "job-3",
            {"diarization": True, "diarization_engine": "nemotron"},
            "diarization",
        )

    @patch.object(backend_app.storage, "prepare_job_rerun")
    @patch.object(backend_app.diarization_service, "engine_available")
    def test_unready_engine_is_refused_not_replaced(
        self,
        engine_available,
        prepare_job_rerun,
    ) -> None:
        """Выбранный, но не готовый движок — 409, а не тихая подмена другим."""
        engine_available.side_effect = lambda engine: engine == "pyannote"
        with self.assertRaises(backend_app.HTTPException) as caught:
            backend_app.diarize_completed_job(
                "job-4", backend_app.JobDiarize(engine="nemotron")
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Nemotron", caught.exception.detail)
        prepare_job_rerun.assert_not_called()


if __name__ == "__main__":
    unittest.main()
