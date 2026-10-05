"""Длинные записи: окна, чекпоинты и продолжение после сбоя.

Замер, из которого всё это выросло: faster-whisper держит в памяти весь файл
сразу (звук float32, копия речи после VAD, мел-спектрограмма), пик на плотной
речи — 6818 МБ на 2 часа, то есть около 3.4 ГБ на час. Без окон 50 и 100 часов
не обработать ни на какой разумной машине. И до чекпоинтов сбой питания на
пятнадцатом часе суточной расшифровки означал полную потерю работы:
storage.init_db переводит `processing` в `queued` и обнуляет прогресс.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_longform
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend import longform, storage


class PlanTests(unittest.TestCase):
    """Разбиение на окна."""

    def test_short_file_is_not_split(self) -> None:
        """Ниже порога поведение прежнее — один проход, без вызовов ffmpeg."""
        for duration in (0, 60, 600, longform.WINDOW_THRESHOLD):
            with self.subTest(duration=duration):
                self.assertEqual(longform.plan(duration), [])

    def test_long_file_is_split_into_windows(self) -> None:
        windows = longform.plan(3 * longform.WINDOW_SECONDS)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], 0.0)
        self.assertEqual(windows[-1][1], 3 * longform.WINDOW_SECONDS)

    def test_windows_are_contiguous_and_cover_everything(self) -> None:
        """Ни дыр, ни перекрытий на шкале: иначе речь потерялась бы или удвоилась."""
        total = 100 * 3600.0  # сто часов, о которых и был вопрос
        windows = longform.plan(total)
        self.assertGreater(len(windows), 100)
        self.assertEqual(windows[0][0], 0.0)
        self.assertAlmostEqual(windows[-1][1], total)
        for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
            self.assertEqual(previous_end, next_start)

    def test_no_tiny_tail_window(self) -> None:
        """Хвост в 40 секунд не должен становиться отдельным окном."""
        windows = longform.plan(longform.WINDOW_SECONDS * 2 + 40)
        self.assertEqual(len(windows), 2)
        self.assertAlmostEqual(windows[-1][1], longform.WINDOW_SECONDS * 2 + 40)

    def test_hundred_hours_peak_stays_at_one_window(self) -> None:
        """Смысл окон: пик памяти не зависит от длины файла."""
        windows = longform.plan(100 * 3600.0)
        longest = max(end - start for start, end in windows)
        self.assertLessEqual(longest, longform.WINDOW_SECONDS * 1.25)


class OwnershipTests(unittest.TestCase):
    """Кому принадлежит сегмент на стыке окон."""

    def test_first_window_keeps_everything(self) -> None:
        self.assertTrue(longform.owns(0.0, 5.0, 0.0))
        self.assertTrue(longform.owns(100.0, 105.0, 0.0))

    def test_segment_from_the_lead_in_is_dropped(self) -> None:
        """Речь до границы уже записана предыдущим окном."""
        self.assertFalse(longform.owns(1790.0, 1795.0, 1800.0))

    def test_segment_after_the_border_is_kept(self) -> None:
        self.assertTrue(longform.owns(1801.0, 1806.0, 1800.0))

    def test_segment_across_the_border_goes_by_its_middle(self) -> None:
        # Середина до границы — сегмент принадлежит предыдущему окну.
        self.assertFalse(longform.owns(1794.0, 1802.0, 1800.0))
        # Середина после границы — этому.
        self.assertTrue(longform.owns(1798.0, 1810.0, 1800.0))


class CheckpointTests(unittest.TestCase):
    """Сохранение и продолжение."""

    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-longform-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
        }
        storage.DATA_DIR = self._root
        storage.DB_PATH = self._root / "test.db"
        storage.UPLOADS_DIR = self._root / "sessions"
        storage.DEFAULT_SETTINGS["managed_storage_root"] = str(storage.UPLOADS_DIR)
        storage.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        storage.init_db()
        self.session = storage.create_session("Длинный файл")
        self.job = {"id": "job-1", "session_id": self.session["id"]}
        self.audio = self._root / "long.wav"
        self.audio.write_bytes(b"x" * 1000)
        self.options = {"language": "ru", "quality": "accurate"}

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        shutil.rmtree(self._root, ignore_errors=True)

    def _segments(self, count: int) -> list[dict]:
        return [
            {"start": i * 10.0, "end": i * 10.0 + 9.0, "text": f"Фраза {i}."}
            for i in range(count)
        ]

    def test_nothing_saved_means_start_from_zero(self) -> None:
        signature = longform.signature(self.audio, self.options)
        self.assertEqual(longform.load(self.job, signature), (0.0, []))

    def test_saved_work_is_resumed(self) -> None:
        signature = longform.signature(self.audio, self.options)
        longform.save(self.job, signature, 3600.0, self._segments(5))

        done_until, segments = longform.load(self.job, signature)

        self.assertEqual(done_until, 3600.0)
        self.assertEqual(len(segments), 5)
        self.assertEqual(segments[0]["text"], "Фраза 0.")

    def test_changed_options_discard_the_checkpoint(self) -> None:
        """Другое качество — другой прогон, смешивать нельзя."""
        longform.save(
            self.job,
            longform.signature(self.audio, self.options),
            3600.0,
            self._segments(3),
        )
        other = longform.signature(
            self.audio, {**self.options, "quality": "fast"}
        )

        self.assertEqual(longform.load(self.job, other), (0.0, []))

    def test_old_vad_checkpoint_is_not_reused(self) -> None:
        current = longform.signature(self.audio, self.options)
        old = current.removeprefix(f"{longform.TRANSCRIPTION_PROFILE}|")
        longform.save(self.job, old, 3600.0, self._segments(3))
        self.assertEqual(longform.load(self.job, current), (0.0, []))

    def test_changed_source_discards_the_checkpoint(self) -> None:
        signature = longform.signature(self.audio, self.options)
        longform.save(self.job, signature, 3600.0, self._segments(3))
        self.audio.write_bytes(b"y" * 2000)

        fresh = longform.signature(self.audio, self.options)

        self.assertNotEqual(fresh, signature)
        self.assertEqual(longform.load(self.job, fresh), (0.0, []))

    def test_broken_checkpoint_is_ignored_not_fatal(self) -> None:
        """Обрыв питания посреди записи не должен ронять задачу."""
        signature = longform.signature(self.audio, self.options)
        longform.save(self.job, signature, 1800.0, self._segments(2))
        path = longform._path(self.job)
        path.write_text("{это не json", encoding="utf-8")

        self.assertEqual(longform.load(self.job, signature), (0.0, []))

    def test_write_is_atomic(self) -> None:
        """Пишем во временный файл и переименовываем — недописанного не остаётся."""
        signature = longform.signature(self.audio, self.options)
        longform.save(self.job, signature, 1800.0, self._segments(2))
        path = longform._path(self.job)
        self.assertTrue(path.is_file())
        self.assertFalse(path.with_suffix(".part").exists())
        # Файл всегда цельный JSON.
        json.loads(path.read_text(encoding="utf-8"))

    def test_later_window_replaces_the_earlier_state(self) -> None:
        signature = longform.signature(self.audio, self.options)
        longform.save(self.job, signature, 1800.0, self._segments(2))
        longform.save(self.job, signature, 3600.0, self._segments(4))

        done_until, segments = longform.load(self.job, signature)

        self.assertEqual(done_until, 3600.0)
        self.assertEqual(len(segments), 4)

    def test_clear_removes_the_checkpoint(self) -> None:
        signature = longform.signature(self.audio, self.options)
        longform.save(self.job, signature, 3600.0, self._segments(2))

        longform.clear(self.job)

        self.assertFalse(longform._path(self.job).exists())
        self.assertEqual(longform.load(self.job, signature), (0.0, []))

    def test_clear_is_safe_without_a_checkpoint(self) -> None:
        longform.clear(self.job)  # не должно бросать


if __name__ == "__main__":
    unittest.main()
