"""A source-only restore has no recordings; skipped checks are not failures."""
import contextlib
import gc
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import doctor
from backend import storage


class SourceRestoreDiagnosticsTests(unittest.TestCase):
    def test_first_install_creates_defaults_and_existing_db_is_not_reinitialized(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with contextlib.ExitStack() as stack:
                stack.callback(gc.collect)
                for name, path in {
                    "DATA_DIR": root,
                    "DB_PATH": root / "whisper.sqlite3",
                    "TEMP_DIR": root / "temp",
                    "DICTATIONS_DIR": root / "dictations",
                    "COOKIES_DIR": root / "cookies",
                }.items():
                    stack.enter_context(patch.object(storage, name, path))
                init = stack.enter_context(patch.object(storage, "init_db", wraps=storage.init_db))
                doctor.ensure_storage()
                self.assertEqual(storage.get_settings()["diarization_device"], storage.DEFAULT_SETTINGS["diarization_device"])
                doctor.ensure_storage()
                self.assertEqual(init.call_count, 1)

    def test_missing_recording_skips_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(doctor, "SAMPLE", Path(folder) / "absent.wav"):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertIsNone(doctor.check_speed())
        self.assertIn("Замер пропущен", output.getvalue())

    def run_diagnostics(self, required, speed):
        with patch.object(doctor, "ensure_storage"):
            return self.run_initialized_diagnostics(required, speed)

    def run_initialized_diagnostics(self, required, speed):
        with patch.object(doctor, "check_components", return_value=(required, [])):
            with patch.object(doctor, "check_diarization"):
                with patch.object(doctor, "check_speed", return_value=speed):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        result = doctor.main()
        return result, output.getvalue()

    def test_no_recording_reports_components_without_claiming_inference(self):
        result, output = self.run_diagnostics([], None)
        self.assertEqual(result, 0)
        self.assertIn("распознавание и скорость ещё не проверены", output)
        self.assertNotIn("Сервис готов к работе", output)

    def test_missing_component_still_fails_when_inference_skipped(self):
        result, _ = self.run_diagnostics(["FFmpeg"], None)
        self.assertEqual(result, 1)

    def test_real_inference_failure_still_fails(self):
        result, output = self.run_diagnostics([], False)
        self.assertEqual(result, 1)
        self.assertIn("Распознавание не работает", output)

    def test_successful_inference_reports_service_ready(self):
        result, output = self.run_diagnostics([], True)
        self.assertEqual(result, 0)
        self.assertIn("Сервис готов к работе", output)


if __name__ == "__main__":
    unittest.main()
