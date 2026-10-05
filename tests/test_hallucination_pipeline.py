"""Сквозная проверка: мусор не доходит до файлов результата.

Модульные тесты в test_hallucinations.py проверяют сам фильтр. Здесь проверяется
проводка: что `transcribe_job` действительно его вызывает и что во всех пяти
форматах мусора нет, а запись о снятом — есть.

Модель подменяется заглушкой: настоящее распознавание тут не нужно и не
воспроизводимо, галлюцинация возникает только на конкретном шуме.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_hallucination_pipeline
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from backend import storage, transcriber


class _Word:
    def __init__(self, start: float, end: float, word: str) -> None:
        self.start = start
        self.end = end
        self.word = word
        self.probability = 0.9


class _Segment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.words = [_Word(start, end, text)]


class _Info:
    language = "ru"
    language_probability = 0.99
    duration = 12.0


class _StubModel:
    """Отдаёт настоящую фразу, приклеенную подпись и отдельный тег звука."""

    SEGMENTS = (
        (0.0, 4.0, " Добрый день, начинаем совещание."),
        (4.5, 8.0, " На этом всё. Субтитры сделал DimaTorzok"),
        (9.0, 10.0, "ВЕСЕЛАЯ МУЗЫКА"),
        (10.5, 12.0, " Продолжение следует..."),
    )

    def transcribe(self, *args, **kwargs):
        segments = (_Segment(*item) for item in self.SEGMENTS)
        return segments, _Info()


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-halluc-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "TEMP_DIR": storage.TEMP_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
            "_load_model": transcriber._load_model,
        }
        storage.DATA_DIR = self._root
        storage.DB_PATH = self._root / "test.db"
        storage.UPLOADS_DIR = self._root / "sessions"
        storage.TEMP_DIR = self._root / "temp"
        storage.DEFAULT_SETTINGS["managed_storage_root"] = str(
            storage.UPLOADS_DIR
        )
        storage.DEFAULT_SETTINGS["temporary_storage_root"] = str(
            storage.TEMP_DIR
        )
        storage.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        storage.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        storage.init_db()
        transcriber._load_model = lambda language: (_StubModel(), "cpu")

    def tearDown(self) -> None:
        storage.DB_PATH = self._saved["DB_PATH"]
        storage.DATA_DIR = self._saved["DATA_DIR"]
        storage.UPLOADS_DIR = self._saved["UPLOADS_DIR"]
        storage.TEMP_DIR = self._saved["TEMP_DIR"]
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        transcriber._load_model = self._saved["_load_model"]
        shutil.rmtree(self._root, ignore_errors=True)

    def _run_job(self) -> dict:
        session = storage.create_session("Проверка фильтра")
        media = storage.session_dir(session["id"]) / "media"
        media.mkdir(parents=True, exist_ok=True)
        audio = media / "sample.wav"
        audio.write_bytes(b"RIFF")  # заглушка не читает файл
        job = storage.create_job(
            session["id"],
            "sample.wav",
            audio,
            {
                "language": "ru",
                "quality": "accurate",
                "timestamps": "segments",
                "formatting": "formatted",
                "diarization": False,
            },
        )
        transcriber.transcribe_job(job["id"])
        return storage.get_job(job["id"])

    def test_outputs_are_clean_and_removal_is_recorded(self) -> None:
        job = self._run_job()
        self.assertEqual(job["status"], "completed", job.get("error"))

        for kind in ("txt", "srt", "vtt"):
            with self.subTest(kind=kind):
                text = Path(job["outputs"][kind]).read_text(encoding="utf-8")
                self.assertNotIn("DimaTorzok", text)
                self.assertNotIn("Субтитры", text)
                self.assertNotIn("МУЗЫКА", text)
                self.assertNotIn("Продолжение следует", text)
                self.assertIn("Добрый день", text)
                # Живая часть склеенного сегмента обязана уцелеть.
                self.assertIn("На этом всё", text)

        payload = json.loads(
            Path(job["outputs"]["json"]).read_text(encoding="utf-8")
        )
        texts = [item["text"] for item in payload["segments"]]
        self.assertEqual(len(payload["segments"]), 2)
        self.assertNotIn("DimaTorzok", " ".join(texts))

        removed = payload["removed_hallucinations"]
        self.assertEqual(len(removed), 3)
        reasons = " | ".join(item["reason"] for item in removed)
        self.assertIn("подпись", reasons)
        self.assertIn("тег звука", reasons)
        self.assertIn("шаблонная вставка", reasons)
        # В записи о снятом текст сохранён полностью — иначе проверить нечего.
        self.assertTrue(
            any("DimaTorzok" in item["text"] for item in removed)
        )

    def test_docx_is_clean(self) -> None:
        from docx import Document

        job = self._run_job()
        document = Document(job["outputs"]["docx"])
        text = "\n".join(p.text for p in document.paragraphs)
        self.assertNotIn("DimaTorzok", text)
        self.assertIn("Добрый день", text)

    def test_cleanup_script_rewrites_old_results(self) -> None:
        """scripts/clean_hallucinations.py --apply на грязном результате.

        Скрипт перезаписывает файлы на диске, поэтому его нельзя оставлять
        непроверенным: тест подкладывает старый грязный JSON и убеждается, что
        после прогона мусора нет ни в одном формате, а живая речь на месте.
        """
        from scripts import clean_hallucinations

        job = self._run_job()
        dirty = [
            {
                "start": 0.0,
                "end": 4.0,
                "text": " Добрый день, начинаем совещание.",
                "words": None,
            },
            {
                "start": 5.0,
                "end": 5.6,
                "text": " Субтитры делал DimaTorzok",
                "words": None,
            },
        ]
        json_path = Path(job["outputs"]["json"])
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["segments"] = dirty
        payload.pop("removed_hallucinations", None)
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Текстовые форматы тоже возвращаем в грязное состояние.
        Path(job["outputs"]["txt"]).write_text(
            "Добрый день, начинаем совещание. Субтитры делал DimaTorzok\n",
            encoding="utf-8",
        )

        argv = sys.argv
        sys.argv = ["clean_hallucinations", "--apply"]
        try:
            self.assertEqual(clean_hallucinations.main(), 0)
        finally:
            sys.argv = argv

        refreshed = storage.get_job(job["id"])
        for kind in ("txt", "srt", "vtt"):
            with self.subTest(kind=kind):
                text = Path(refreshed["outputs"][kind]).read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("DimaTorzok", text)
                self.assertIn("Добрый день", text)

        result = json.loads(
            Path(refreshed["outputs"]["json"]).read_text(encoding="utf-8")
        )
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(len(result["removed_hallucinations"]), 1)
        self.assertEqual(result["transcription_profile"], payload["transcription_profile"])

    def test_cleanup_script_report_only_changes_nothing(self) -> None:
        job = self._run_job()
        from scripts import clean_hallucinations

        json_path = Path(job["outputs"]["json"])
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["segments"] = [
            {
                "start": 0.0,
                "end": 1.0,
                "text": " Субтитры делал DimaTorzok",
                "words": None,
            }
        ]
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        before = json_path.read_text(encoding="utf-8")

        argv = sys.argv
        sys.argv = ["clean_hallucinations"]
        try:
            self.assertEqual(clean_hallucinations.main(), 0)
        finally:
            sys.argv = argv

        self.assertEqual(json_path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
