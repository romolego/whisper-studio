"""A source-only restore has no recordings; skipped checks are not failures."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import doctor


class SourceRestoreDiagnosticsTests(unittest.TestCase):
    def test_missing_recording_skips_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(doctor, "SAMPLE", Path(folder) / "absent.wav"):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertIsNone(doctor.check_speed())
        self.assertIn("Замер пропущен", output.getvalue())

    def run_diagnostics(self, required, speed):
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
