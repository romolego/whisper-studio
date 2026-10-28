"""Приоритет диктовки над длинными файлами.

Главное, что проверяется: диктовка получает видеокарту на ближайшем чекпоинте
батча, а не после окончания всего файла. И что оба класса работы никогда не
считают одновременно — на 6 ГБ видеопамяти это не поместилось бы.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_gpu_turns
"""
from __future__ import annotations

import threading
import unittest

from backend import gpu_turns


class TurnOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(
            gpu_turns.state(),
            {
                "interactive_waiting": 0,
                "interactive_active": 0,
                "batch_active": 0,
            },
            "предыдущий тест не отпустил слот",
        )

    def test_dictation_cuts_in_at_next_checkpoint(self) -> None:
        """Батч из 50 сегментов, диктовка приходит на первом же."""
        order: list[str] = []
        lock = threading.Lock()
        dictation_started = threading.Event()
        batch_reached_first_segment = threading.Event()

        def batch_job() -> None:
            with gpu_turns.batch():
                for index in range(50):
                    gpu_turns.checkpoint()
                    with lock:
                        order.append(f"segment-{index}")
                    if index == 0:
                        batch_reached_first_segment.set()
                        # Даём диктовке время зарегистрировать заявку, иначе
                        # тест проверял бы просто скорость потоков.
                        dictation_started.wait(5)

        def dictation() -> None:
            batch_reached_first_segment.wait(5)
            dictation_started.set()
            with gpu_turns.interactive():
                with lock:
                    order.append("dictation")

        threads = [
            threading.Thread(target=batch_job),
            threading.Thread(target=dictation),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
            self.assertFalse(thread.is_alive(), "поток не завершился")

        self.assertIn("dictation", order)
        position = order.index("dictation")
        # Диктовка обязана вклиниться в начале, а не после 50-го сегмента.
        self.assertLessEqual(
            position,
            3,
            f"диктовка дождалась {position} сегментов: {order[:6]}",
        )
        self.assertEqual(len(order), 51)

    def test_never_overlap(self) -> None:
        """Батч и диктовка не считают одновременно — иначе OOM видеопамяти."""
        active: list[str] = []
        overlaps: list[tuple[str, ...]] = []
        lock = threading.Lock()

        def enter(kind: str) -> None:
            with lock:
                active.append(kind)
                if len(active) > 1:
                    overlaps.append(tuple(active))

        def leave(kind: str) -> None:
            with lock:
                active.remove(kind)

        def batch_job() -> None:
            for _ in range(20):
                with gpu_turns.batch():
                    enter("batch")
                    gpu_turns.checkpoint()
                    leave("batch")

        def dictation() -> None:
            for _ in range(20):
                with gpu_turns.interactive():
                    enter("dictation")
                    leave("dictation")

        threads = [
            threading.Thread(target=batch_job),
            threading.Thread(target=dictation),
            threading.Thread(target=dictation),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
            self.assertFalse(thread.is_alive())

        self.assertEqual(overlaps, [], f"одновременный доступ: {overlaps[:3]}")

    def test_counters_return_to_zero(self) -> None:
        with gpu_turns.batch():
            gpu_turns.checkpoint()
        with gpu_turns.interactive():
            pass
        self.assertEqual(
            gpu_turns.state(),
            {
                "interactive_waiting": 0,
                "interactive_active": 0,
                "batch_active": 0,
            },
        )

    def test_counters_survive_an_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with gpu_turns.batch():
                raise RuntimeError("сбой посреди задачи")
        with self.assertRaises(RuntimeError):
            with gpu_turns.interactive():
                raise RuntimeError("сбой посреди диктовки")
        self.assertEqual(gpu_turns.state()["batch_active"], 0)
        self.assertEqual(gpu_turns.state()["interactive_active"], 0)

    def test_checkpoint_outside_batch_is_a_noop(self) -> None:
        """Прямой вызов transcribe_job в тестах не должен ломать счётчики."""
        gpu_turns.checkpoint()
        gpu_turns.checkpoint()
        self.assertEqual(gpu_turns.state()["batch_active"], 0)

    def test_nested_batch_restores_the_marker(self) -> None:
        """Разметка спикеров берёт слот внутри задачи, уже бравшей слот."""
        with gpu_turns.batch():
            with gpu_turns.batch():
                gpu_turns.checkpoint()
            gpu_turns.checkpoint()
        self.assertEqual(gpu_turns.state()["batch_active"], 0)

    def test_local_and_remote_batches_are_serialized(self) -> None:
        entered = threading.Event()
        leave = threading.Event()
        second = threading.Event()

        def first_job():
            with gpu_turns.batch():
                entered.set()
                leave.wait(5)

        def second_job():
            entered.wait(5)
            with gpu_turns.batch():
                second.set()

        threads = [threading.Thread(target=first_job), threading.Thread(target=second_job)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse(second.wait(0.1))
        finally:
            leave.set()
            for thread in threads:
                thread.join(5)
        self.assertTrue(second.is_set())
        self.assertEqual(gpu_turns.state()["batch_active"], 0)

    def test_waiting_remote_batch_can_be_cancelled(self) -> None:
        requested = threading.Event()
        cancelled = threading.Event()

        def check():
            if requested.is_set():
                raise RuntimeError("cancelled")

        def waiting_job():
            try:
                with gpu_turns.batch(cancel_check=check):
                    self.fail("Cancelled batch acquired the GPU")
            except RuntimeError:
                cancelled.set()

        with gpu_turns.batch():
            thread = threading.Thread(target=waiting_job)
            thread.start()
            requested.set()
            self.assertTrue(cancelled.wait(3))
        thread.join(5)
        self.assertEqual(gpu_turns.state()["batch_active"], 0)


if __name__ == "__main__":
    unittest.main()
