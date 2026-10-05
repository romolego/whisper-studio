"""Раздел «Модели и зависимости»: инвентарь и загрузка моделей.

Смысл раздела — чтобы после переустановки Windows или на новой машине было
видно, что нужно сервису, что из этого стоит, где лежит и откуда берётся.
Поэтому тесты проверяют не только «поле есть», но и обещания: отсутствие
необязательного компонента не делает сервис неготовым, а у каждого компонента
сказано, что именно сломается без него.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_components
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gc

from backend import components, storage
from scripts import doctor


class InventoryShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.addCleanup(gc.collect)
        root = Path(workspace.name)
        for name, path in {
            "DATA_DIR": root,
            "DB_PATH": root / "whisper.sqlite3",
            "TEMP_DIR": root / "temp",
            "DICTATIONS_DIR": root / "dictations",
            "COOKIES_DIR": root / "cookies",
        }.items():
            patcher = patch.object(storage, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        doctor.ensure_storage()
        components.invalidate()
        self.inventory = components.inventory(refresh=True)

    def test_every_component_answers_the_four_questions(self) -> None:
        """Нужен ли, есть ли, где лежит, откуда берётся."""
        for item in self.inventory["items"]:
            with self.subTest(component=item["key"]):
                self.assertIsInstance(item["required"], bool)
                self.assertIsInstance(item["present"], bool)
                self.assertTrue(item["title"])
                self.assertIn(item["group"], {"model", "runtime", "python"})
                # Источник обязателен: без него непонятно, где брать компонент
                # при переустановке. Исключение — драйвер и библиотеки, где
                # ссылка есть, но пути может не быть.
                self.assertTrue(item["source"], item["key"])
                self.assertTrue(item["impact"], item["key"])

    def test_present_components_have_no_install_action(self) -> None:
        for item in self.inventory["items"]:
            if item["present"]:
                with self.subTest(component=item["key"]):
                    self.assertEqual(item["install"], "none")

    def test_models_are_installable_by_the_service(self) -> None:
        keys = {item["key"]: item for item in self.inventory["items"]}
        for key in ("model_ru", "model_en", "model_pyannote"):
            self.assertIn(key, keys)
        # Whisper сервис умеет скачать сам; для программ показывается команда.
        for key in ("python", "ffmpeg", "node", "pwsh"):
            self.assertTrue(keys[key]["command"], key)

    def test_optional_components_do_not_break_readiness(self) -> None:
        """Без Pyannote сервис работает — он не должен считаться неготовым."""
        keys = {item["key"]: item for item in self.inventory["items"]}
        self.assertFalse(keys["model_pyannote"]["required"])
        self.assertFalse(keys["nvidia"]["required"])
        self.assertFalse(keys["cuda_libs"]["required"])
        # Английская модель тоже необязательна: без неё русские файлы работают.
        self.assertFalse(keys["model_en"]["required"])
        # А русская обязательна — на ней всё и держится.
        self.assertTrue(keys["model_ru"]["required"])

    def test_ready_ignores_optional_gaps(self) -> None:
        with patch.object(components, "_cache", None):
            fake = [
                components._component(
                    "a", "Обязательный", "runtime", True, impact="x", source="y"
                ),
                components._component(
                    "b",
                    "Необязательный",
                    "model",
                    False,
                    required=False,
                    impact="x",
                    source="y",
                ),
            ]
            with patch.object(
                components, "_package_components", return_value=fake
            ), patch.object(
                components, "_runtime_components", return_value=[]
            ), patch.object(
                components, "_pyannote_component", side_effect=lambda: fake[1]
            ), patch.object(
                components, "WHISPER_MODELS", ()
            ):
                result = components.inventory(refresh=True)
        self.assertTrue(result["ready"])
        self.assertEqual(result["missing_optional"], ["b", "b"])
        self.assertEqual(result["missing_required"], [])

    def test_cache_avoids_repeated_probing(self) -> None:
        calls = []

        def spy() -> list:
            calls.append(1)
            return []

        with patch.object(components, "_runtime_components", side_effect=spy):
            components.inventory(refresh=True)
            components.inventory()
            components.inventory()
        self.assertEqual(len(calls), 1, "кеш не сработал")


class MissingModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="whisper-components-"))
        self.saved_models = components.MODELS_DIR
        self.saved_whisper = components.WHISPER_MODELS
        components.MODELS_DIR = self.root
        components.WHISPER_MODELS = (
            (
                "model_ru",
                "Whisper для русского",
                self.root / "turbo-russian",
                "coriollon/whisper-large-v3-turbo-russian",
                "нет русского",
            ),
            (
                "model_en",
                "Whisper для английского",
                self.root / "turbo",
                "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                "нет английского",
            ),
        )

    def tearDown(self) -> None:
        components.MODELS_DIR = self.saved_models
        components.WHISPER_MODELS = self.saved_whisper
        shutil.rmtree(self.root, ignore_errors=True)

    def test_both_missing(self) -> None:
        self.assertEqual(
            sorted(components.missing_models()), ["model_en", "model_ru"]
        )

    def test_one_present(self) -> None:
        folder = self.root / "turbo-russian"
        folder.mkdir(parents=True)
        (folder / "model.bin").write_bytes(b"x" * 10)
        self.assertEqual(components.missing_models(), ["model_en"])

    def test_component_reports_source_and_path(self) -> None:
        item = components._whisper_component(*components.WHISPER_MODELS[0])
        self.assertFalse(item["present"])
        self.assertIn("huggingface.co", item["source"])
        self.assertEqual(item["path"], str(self.root / "turbo-russian"))
        self.assertEqual(item["install"], "service")


class DownloadStateTests(unittest.TestCase):
    def tearDown(self) -> None:
        components._download_state.clear()
        components._download_state.update({"status": "idle", "targets": []})

    def test_nothing_to_download(self) -> None:
        with patch.object(components, "missing_models", return_value=[]):
            state = components.start_model_download()
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["targets"], [])

    def test_percent_is_computed(self) -> None:
        components._download_state.update(
            {
                "status": "running",
                "targets": [],
                "downloaded_bytes": 500,
                "total_bytes": 2000,
            }
        )
        self.assertEqual(components.download_status()["percent"], 25.0)

    def test_percent_is_none_without_total(self) -> None:
        """Размер с Hugging Face мог не приехать — процент честно отсутствует."""
        components._download_state.update(
            {
                "status": "running",
                "targets": [],
                "downloaded_bytes": 500,
                "total_bytes": 0,
            }
        )
        self.assertIsNone(components.download_status()["percent"])

    def test_second_start_does_not_duplicate_a_running_download(self) -> None:
        components._download_state.update(
            {"status": "running", "targets": [], "message": "идёт"}
        )
        with patch.object(components, "missing_models") as missing:
            state = components.start_model_download()
        missing.assert_not_called()
        self.assertEqual(state["status"], "running")

    def test_fallback_total_when_hub_is_unreachable(self) -> None:
        """Без ответа Hugging Face прогресс всё равно должен быть осмысленным."""
        with patch.object(components, "_repo_total_bytes", return_value=0), patch.object(
            components, "missing_models", return_value=["model_ru"]
        ), patch.object(components.threading, "Thread"):
            state = components.start_model_download()
        self.assertEqual(state["status"], "running")
        self.assertEqual(
            state["total_bytes"], components._FALLBACK_TOTALS["model_ru"]
        )


if __name__ == "__main__":
    unittest.main()


class DownloadProgressTests(unittest.TestCase):
    """Прогресс, скорость и остаток на живых потоках.

    Это самая хрупкая часть: два потока, счёт байтов по диску и сглаживание
    скорости. Настоящая загрузка с Hugging Face в тестах недопустима, поэтому
    prepare_model_files подменяется заглушкой, которая пишет байты порциями —
    остальная механика работает настоящая.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="whisper-progress-"))
        self.saved_models = components.MODELS_DIR
        components.MODELS_DIR = self.root
        components._download_state.clear()
        components._download_state.update({"status": "idle", "targets": []})

    def tearDown(self) -> None:
        with components._download_lock:
            components._download_state["status"] = "cancelled"
        components.MODELS_DIR = self.saved_models
        shutil.rmtree(self.root, ignore_errors=True)

    def test_progress_follows_bytes_on_disk(self) -> None:
        import time as clock

        chunk = 400_000

        def fake_prepare(progress=None):
            folder = self.root / "turbo"
            folder.mkdir(parents=True, exist_ok=True)
            written = b""
            for step in range(4):
                written += b"x" * chunk
                (folder / "model.bin").write_bytes(written)
                if progress:
                    progress(f"Шаг {step + 1}")
                clock.sleep(0.6)

        fake_module = type(sys)("backend.transcriber_stub")
        fake_module.prepare_model_files = fake_prepare

        with patch.object(
            components, "missing_models", return_value=["model_en"]
        ), patch.object(
            components, "_repo_total_bytes", return_value=chunk * 4
        ), patch.dict(
            sys.modules, {"backend.transcriber": fake_module}
        ):
            state = components.start_model_download()
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["total_bytes"], chunk * 4)

            seen: list[float] = []
            deadline = clock.monotonic() + 12
            while clock.monotonic() < deadline:
                status = components.download_status()
                if status["percent"]:
                    seen.append(status["percent"])
                if status["status"] != "running":
                    break
                clock.sleep(0.4)

        final = components.download_status()
        self.assertEqual(final["status"], "done", final.get("error"))
        self.assertEqual(final["percent"], 100.0)
        self.assertTrue(seen, "прогресс не обновлялся ни разу")
        # Прогресс обязан расти, а не прыгать назад.
        self.assertEqual(seen, sorted(seen))
        self.assertLess(seen[0], 100.0, "первое значение уже 100%")

    def test_error_is_reported_not_swallowed(self) -> None:
        def broken(progress=None):
            raise RuntimeError("сеть недоступна")

        fake_module = type(sys)("backend.transcriber_stub")
        fake_module.prepare_model_files = broken

        with patch.object(
            components, "missing_models", return_value=["model_en"]
        ), patch.object(
            components, "_repo_total_bytes", return_value=1000
        ), patch.dict(
            sys.modules, {"backend.transcriber": fake_module}
        ):
            components.start_model_download()
            import time as clock

            deadline = clock.monotonic() + 6
            while clock.monotonic() < deadline:
                if components.download_status()["status"] != "running":
                    break
                clock.sleep(0.2)

        final = components.download_status()
        self.assertEqual(final["status"], "error")
        self.assertIn("сеть недоступна", final["error"])
