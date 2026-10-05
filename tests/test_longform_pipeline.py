"""Сквозная проверка окон: реальный длинный файл, реальный сбой, продолжение.

Модель подменяется заглушкой — проверяется проводка, а не качество распознавания.
Аудио настоящее: окна режет настоящий ffmpeg, и именно это надо проверить.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_longform_pipeline
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend import longform, storage, transcriber


def _make_audio(target: Path, seconds: int) -> None:
    """Тон с паузами: VAD не должен выбросить всё целиком."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"sine=frequency=200:duration={seconds}",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(target),
        ],
        check=True,
    )


class _Word:
    def __init__(self, start: float, end: float, word: str) -> None:
        self.start, self.end, self.word, self.probability = start, end, word, 0.9


class _Segment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start, self.end, self.text = start, end, text
        self.words = [_Word(start, end, text)]


class _Info:
    language = "ru"
    language_probability = 0.99

    def __init__(self, duration: float) -> None:
        self.duration = duration


class _WindowModel:
    """Отдаёт по два сегмента на каждое переданное окно.

    Помнит длительности полученных клипов: по ним видно, что модель получала
    куски, а не файл целиком.
    """

    def __init__(self) -> None:
        self.clips: list[float] = []
        self.options: list[dict] = []
        self.fail_after: int | None = None

    def transcribe(self, path, **kwargs):
        import wave

        self.options.append(kwargs)
        with wave.open(str(path), "rb") as handle:
            duration = handle.getnframes() / handle.getframerate()
        self.clips.append(round(duration, 1))
        if self.fail_after is not None and len(self.clips) > self.fail_after:
            raise RuntimeError("свет выключили")
        index = len(self.clips)
        segments = iter(
            [
                _Segment(1.0, 5.0, f" Окно {index}, начало."),
                _Segment(duration - 6.0, duration - 2.0, f" Окно {index}, конец."),
            ]
        )
        return segments, _Info(duration)


class LongformPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-longpipe-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "TEMP_DIR": storage.TEMP_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
            "_load_model": transcriber._load_model,
            "WINDOW_SECONDS": longform.WINDOW_SECONDS,
            "WINDOW_THRESHOLD": longform.WINDOW_THRESHOLD,
            "WINDOW_OVERLAP": longform.WINDOW_OVERLAP,
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

        # Окна уменьшены, чтобы тест шёл на 60-секундном файле, а не на часовом.
        longform.WINDOW_SECONDS = 20.0
        longform.WINDOW_THRESHOLD = 25.0
        longform.WINDOW_OVERLAP = 2.0

        self.model = _WindowModel()
        transcriber._load_model = lambda language: (self.model, "cpu")

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR", "TEMP_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        transcriber._load_model = self._saved["_load_model"]
        longform.WINDOW_SECONDS = self._saved["WINDOW_SECONDS"]
        longform.WINDOW_THRESHOLD = self._saved["WINDOW_THRESHOLD"]
        longform.WINDOW_OVERLAP = self._saved["WINDOW_OVERLAP"]
        shutil.rmtree(self._root, ignore_errors=True)

    def _job(self, seconds: int = 60) -> dict:
        session = storage.create_session("Длинная запись")
        media_dir = storage.session_dir(session["id"]) / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        audio = media_dir / "long.wav"
        _make_audio(audio, seconds)
        return storage.create_job(
            session["id"],
            "long.wav",
            audio,
            {
                "language": "ru",
                "quality": "accurate",
                "timestamps": "segments",
                "formatting": "formatted",
                "diarization": False,
            },
        )

    def test_long_file_is_processed_in_windows(self) -> None:
        job = self._job(60)

        transcriber.transcribe_job(job["id"])

        state = storage.get_job(job["id"])
        self.assertEqual(state["status"], "completed", state.get("error"))
        # Три окна по 20 секунд, а не один клип на 60.
        self.assertEqual(len(self.model.clips), 3)
        self.assertTrue(
            all(clip <= 22.5 for clip in self.model.clips),
            f"модель получила слишком длинный кусок: {self.model.clips}",
        )
        self.assertTrue(all(not opts["vad_filter"] for opts in self.model.options))

    def test_timestamps_stay_on_the_full_timeline(self) -> None:
        job = self._job(60)
        transcriber.transcribe_job(job["id"])

        payload = json.loads(
            Path(storage.get_job(job["id"])["outputs"]["json"]).read_text(
                encoding="utf-8"
            )
        )
        starts = [segment["start"] for segment in payload["segments"]]
        self.assertEqual(starts, sorted(starts), "метки должны идти по возрастанию")
        # Последний сегмент обязан быть ближе к концу записи, а не к концу окна.
        self.assertGreater(payload["segments"][-1]["end"], 50.0)
        # И метки слов сдвинуты вместе с сегментами.
        words = payload["segments"][-1]["words"]
        self.assertGreater(words[0]["start"], 50.0)

    def test_crash_keeps_finished_windows(self) -> None:
        """Главная защита: сбой не обнуляет уже сделанное."""
        job = self._job(60)
        self.model.fail_after = 2  # третье окно падает

        transcriber.transcribe_job(job["id"])

        state = storage.get_job(job["id"])
        self.assertEqual(state["status"], "failed")
        done_until, segments = longform.load(
            storage.get_job(job["id"]),
            longform.signature(Path(job["stored_path"]), job["options"]),
        )
        self.assertGreater(done_until, 0.0, "чекпоинт не сохранился")
        self.assertEqual(len(segments), 4, "два окна по два сегмента")

    def test_restart_resumes_instead_of_starting_over(self) -> None:
        job = self._job(60)
        self.model.fail_after = 2
        transcriber.transcribe_job(job["id"])
        self.assertEqual(len(self.model.clips), 3)

        # Повторный запуск, как после перезапуска сервиса.
        self.model.clips.clear()
        self.model.fail_after = None
        transcriber.transcribe_job(job["id"])

        state = storage.get_job(job["id"])
        self.assertEqual(state["status"], "completed", state.get("error"))
        # Осталось одно недоделанное окно — первые два не переигрывались.
        self.assertEqual(
            len(self.model.clips), 1, f"переиграны лишние окна: {self.model.clips}"
        )
        payload = json.loads(
            Path(state["outputs"]["json"]).read_text(encoding="utf-8")
        )
        self.assertEqual(len(payload["segments"]), 6, "должны собраться все три окна")

    def test_checkpoint_is_removed_after_success(self) -> None:
        job = self._job(60)
        transcriber.transcribe_job(job["id"])
        self.assertFalse(
            longform._path(storage.get_job(job["id"])).exists(),
            "доделанный файл не должен оставлять чекпоинт",
        )

    def test_short_file_keeps_the_single_pass(self) -> None:
        """Ниже порога — прежний путь: один вызов модели по всему файлу."""
        job = self._job(10)
        transcriber.transcribe_job(job["id"])

        self.assertEqual(storage.get_job(job["id"])["status"], "completed")
        self.assertEqual(len(self.model.clips), 1)
        self.assertAlmostEqual(self.model.clips[0], 10.0, places=0)
        self.assertFalse(longform._path(storage.get_job(job["id"])).exists())

    def test_temp_windows_do_not_pile_up(self) -> None:
        """Окна удаляются сразу: иначе суточный файл забил бы диск."""
        job = self._job(60)
        transcriber.transcribe_job(job["id"])
        leftovers = list(storage.TEMP_DIR.glob("*.wav"))
        self.assertEqual(leftovers, [], f"остались временные окна: {leftovers}")


if __name__ == "__main__":
    unittest.main()
