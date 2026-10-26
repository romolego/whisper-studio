import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import punctuation


class PunctuationRestoreTests(unittest.TestCase):
    def test_inserts_commas_periods_and_capitals_without_rewriting_words(self):
        original = "привет как дела все хорошо спасибо"
        text = punctuation.apply_labels(original, [2, 0, 7, 0, 2, 4])
        self.assertEqual(text, "Привет, как дела? Все хорошо, спасибо.")
        self.assertEqual(punctuation.words(text), punctuation.words(original))

    def test_native_punctuation_is_preserved_and_not_doubled(self):
        text = "Всё хорошо, спасибо. Как дела?"
        restored = punctuation.apply_labels(text, [0, 4, 4, 0, 4])
        self.assertEqual(restored, text)

    def test_continuation_of_a_phrase_does_not_get_an_extra_capital(self):
        self.assertEqual(punctuation.apply_labels(" и подпись.", [0, 0], sentence_start=False), " и подпись.")

    def test_worker_cannot_rewrite_or_truncate_recognized_speech(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "model.safetensors").write_bytes(b"test")
            fake_python = root / ".venv_diarization_gpu/Scripts/python.exe"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_bytes(b"test")
            original = [{"text": "исходный текст", "start": 0, "end": 3, "words": []}]
            def worker(_job, command):
                Path(command[-1]).write_text(json.dumps(["другой текст"]), "utf-8")
                return SimpleNamespace(returncode=0, stderr="")
            with patch.object(punctuation, "MODEL_DIR", root), \
                    patch.object(punctuation, "ROOT", root), \
                    patch.object(punctuation.storage, "temporary_dir", return_value=root), \
                    patch.object(punctuation.cancellation, "run", side_effect=worker):
                with self.assertRaisesRegex(RuntimeError, "изменила"):
                    punctuation.restore(original, "test")
                self.assertEqual(original[0]["text"], "исходный текст")
                self.assertFalse(list(root.glob("*punctuation*.json")))


if __name__ == "__main__":
    unittest.main()
