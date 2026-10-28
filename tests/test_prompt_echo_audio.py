"""Audio evidence must distinguish digital silence from even very quiet speech."""
import tempfile
import unittest
import wave
from pathlib import Path

from backend import media


class DigitalSilenceTests(unittest.TestCase):
    def test_exact_silence_is_detected_but_a_quiet_signal_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'sample.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                audio.writeframes(b'\0\0'*16000 + b'\1\0'*16000)
            self.assertTrue(media.is_digital_silence(path, 0, 1))
            self.assertFalse(media.is_digital_silence(path, 1, 2))
            self.assertFalse(media.is_digital_silence(path, .5, 1.5))
            self.assertFalse(media.is_digital_silence(path, 2, 3))
            self.assertFalse(media.is_digital_silence(path, -1, 0))
