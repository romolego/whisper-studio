"""Внешние сервисы: своя папка, проект на разбор, отметка источника.

Раньше Анализ вебинаров складывал все свои файлы в один проект с фиксированным
названием на верхнем уровне. В общей ленте он не отличался от своих проектов, а
внутри росла свалка: понять, что и когда транскрибировалось, было нельзя. Тесты
закрепляют новый порядок и — отдельно — что он общий, а не про один сервис.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_integrations
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import integrations, storage


class IntegrationProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-integrations-"))
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
        self.analyzer = integrations.REGISTRY["webinar_analyzer"]

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        shutil.rmtree(self._root, ignore_errors=True)

    # --- опознание сервиса ---

    def test_service_is_recognised_by_its_header(self) -> None:
        found = integrations.by_header("webinar-analyzer-local")
        self.assertIsNotNone(found)
        self.assertEqual(found.key, "webinar_analyzer")

    def test_unknown_header_is_not_a_service(self) -> None:
        for value in (None, "", "  ", "нечто-чужое"):
            with self.subTest(value=value):
                self.assertIsNone(integrations.by_header(value))

    # --- папка ---

    def test_folder_is_created_once_and_reused(self) -> None:
        first = integrations.folder(self.analyzer)
        second = integrations.folder(self.analyzer)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["name"], "Анализ вебинаров")
        self.assertEqual(len(storage.list_folders()), 1)

    # Удаление папки сервиса теперь запрещено — это проверяется в
    # FolderProtectionTests ниже. Прежний тест на «удалили и создалась заново»
    # закреплял поведение, от которого сознательно ушли.

    # --- проекты ---

    def test_project_lands_in_the_service_folder(self) -> None:
        project = integrations.project(self.analyzer, "ext-1", "Разбор от 10 августа")

        folder = integrations.folder(self.analyzer)
        self.assertEqual(project["folder_id"], folder["id"])
        self.assertEqual(project["integration"], "webinar_analyzer")
        self.assertIn("Разбор от 10 августа", project["name"])

    def test_same_external_project_reuses_one_project(self) -> None:
        """Файлы одного разбора дописываются в один проект."""
        first = integrations.project(self.analyzer, "ext-1", "Разбор")
        second = integrations.project(self.analyzer, "ext-1", "Разбор")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(storage.list_sessions()), 1)

    def test_different_external_projects_get_their_own(self) -> None:
        """Главное отличие от прежнего поведения: не одна общая свалка."""
        first = integrations.project(self.analyzer, "ext-1", "Первый разбор")
        second = integrations.project(self.analyzer, "ext-2", "Второй разбор")

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(storage.list_sessions()), 2)
        # Оба в одной папке.
        self.assertEqual(first["folder_id"], second["folder_id"])

    def test_without_external_id_everything_goes_into_one_project(self) -> None:
        """Без идентификатора разборы неразличимы — плодить проекты нельзя."""
        first = integrations.project(self.analyzer, None, None)
        second = integrations.project(self.analyzer, None, None)

        self.assertEqual(first["id"], second["id"])
        self.assertIn(self.analyzer.fallback_project, first["name"])

    def test_nameless_project_gets_a_readable_fallback(self) -> None:
        project = integrations.project(self.analyzer, "ext-9", "   ")
        self.assertIn(self.analyzer.fallback_project, project["name"])

    def test_own_projects_are_not_marked(self) -> None:
        """Обычный проект из интерфейса не должен выглядеть как чужой."""
        mine = storage.create_session("Мой проект")
        self.assertIsNone(mine.get("integration"))

    # --- подбор старых проектов ---

    def test_legacy_project_is_moved_into_the_folder(self) -> None:
        legacy = storage.create_session(
            "Webinar Analyzer · локальная очередь", name_format="title_only"
        )
        self.assertIsNone(legacy["folder_id"])

        adopted = integrations.adopt_legacy_projects()

        self.assertEqual(adopted, 1)
        moved = storage.get_session(legacy["id"])
        self.assertEqual(moved["integration"], "webinar_analyzer")
        self.assertEqual(
            moved["folder_id"], integrations.folder(self.analyzer)["id"]
        )

    def test_adoption_is_idempotent(self) -> None:
        """Запускается при каждом старте — второй раз не должен ничего менять."""
        storage.create_session(
            "Webinar Analyzer · локальная очередь", name_format="title_only"
        )
        self.assertEqual(integrations.adopt_legacy_projects(), 1)
        self.assertEqual(integrations.adopt_legacy_projects(), 0)
        self.assertEqual(len(storage.list_folders()), 1)

    def test_adoption_does_not_touch_own_projects(self) -> None:
        mine = storage.create_session("Мой созвон")

        integrations.adopt_legacy_projects()

        untouched = storage.get_session(mine["id"])
        self.assertIsNone(untouched["integration"])
        self.assertIsNone(untouched["folder_id"])

    # --- список сервисов ---

    def test_connected_lists_every_registered_service(self) -> None:
        listed = {item["key"] for item in integrations.connected()}
        self.assertEqual(listed, set(integrations.REGISTRY))

    def test_usage_counts_projects_and_files(self) -> None:
        project = integrations.project(self.analyzer, "ext-1", "Разбор")
        audio = self._root / "a.wav"
        audio.write_bytes(b"x")
        storage.create_job(project["id"], "a.wav", audio, {"language": "ru"})
        integrations.project(self.analyzer, "ext-2", "Второй")

        report = next(
            item
            for item in integrations.connected()
            if item["key"] == "webinar_analyzer"
        )

        self.assertEqual(report["projects"], 2)
        self.assertEqual(report["files"], 1)
        self.assertTrue(report["last_used_at"])

    def test_unused_service_shows_zero_not_absence(self) -> None:
        """Сервис должен быть виден, даже если ещё ни разу не приходил."""
        report = next(
            item
            for item in integrations.connected()
            if item["key"] == "webinar_analyzer"
        )
        self.assertEqual(report["projects"], 0)
        self.assertEqual(report["files"], 0)
        self.assertIsNone(report["last_used_at"])


if __name__ == "__main__":
    unittest.main()


class FolderProtectionTests(unittest.TestCase):
    """Папку внешнего сервиса нельзя удалить.

    Иначе вместе с ней ушли бы расшифровки, которых в самом сервисе может уже не
    быть. Один раз это уже случилось: пакетное удаление проектов снесло проект
    Анализа вебинаров вместе со всем содержимым.
    """

    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-protect-"))
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
        self.analyzer = integrations.REGISTRY["webinar_analyzer"]

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        shutil.rmtree(self._root, ignore_errors=True)

    def test_integration_folder_is_marked(self) -> None:
        folder = integrations.folder(self.analyzer)
        self.assertEqual(folder["integration"], "webinar_analyzer")

    def test_integration_folder_cannot_be_deleted(self) -> None:
        folder = integrations.folder(self.analyzer)

        with self.assertRaises(RuntimeError) as caught:
            storage.delete_folder(folder["id"])

        self.assertIn("внешнего сервиса", str(caught.exception))
        self.assertTrue(storage.get_folder(folder["id"]))

    def test_protection_holds_with_delete_projects_too(self) -> None:
        """Удаление «вместе с проектами» тоже должно упираться в защиту."""
        folder = integrations.folder(self.analyzer)
        integrations.project(self.analyzer, "ext-1", "Разбор")

        with self.assertRaises(RuntimeError):
            storage.delete_folder(folder["id"], delete_projects=True)

        self.assertEqual(len(storage.list_sessions()), 1)

    def test_empty_integration_folder_is_protected(self) -> None:
        """Защита на самой папке, а не на её содержимом."""
        folder = integrations.folder(self.analyzer)
        self.assertEqual(folder["project_count"], 0)

        with self.assertRaises(RuntimeError):
            storage.delete_folder(folder["id"])

    def test_own_folder_is_still_deletable(self) -> None:
        mine = storage.create_folder("Мои созвоны")

        storage.delete_folder(mine["id"])

        self.assertEqual(storage.list_folders(), [])

    def test_renaming_keeps_the_protection(self) -> None:
        """Папку можно переименовать — она остаётся папкой сервиса."""
        folder = integrations.folder(self.analyzer)
        storage.rename_folder(folder["id"], "Вебинары клиента")

        again = integrations.folder(self.analyzer)

        self.assertEqual(again["id"], folder["id"])
        self.assertEqual(again["name"], "Вебинары клиента")
        with self.assertRaises(RuntimeError):
            storage.delete_folder(folder["id"])

    def test_legacy_folder_gets_marked_on_first_use(self) -> None:
        """Папка, созданная до появления отметки, подбирается по названию."""
        legacy = storage.create_folder("Анализ вебинаров")
        self.assertIsNone(legacy["integration"])

        found = integrations.folder(self.analyzer)

        self.assertEqual(found["id"], legacy["id"])
        self.assertEqual(found["integration"], "webinar_analyzer")
        with self.assertRaises(RuntimeError):
            storage.delete_folder(legacy["id"])


class FeedVisibilityTests(unittest.TestCase):
    """Настройка скрытия из общей ленты."""

    def test_hidden_by_default(self) -> None:
        self.assertEqual(storage.DEFAULT_SETTINGS["integration_feed"], "hide")

    def test_only_two_values_allowed(self) -> None:
        self.assertEqual(
            storage.SETTING_VALUES["integration_feed"], {"show", "hide"}
        )


class TelegramIntegrationTests(unittest.TestCase):
    """Телеграм-бот пользуется тем же общим механизмом, что и первый сервис.

    Отдельный класс нужен не ради второй интеграции как таковой, а чтобы
    закрепить обещание модуля: новый сервис — это запись в REGISTRY и общая
    ручка, а не своя копия логики про папки и проекты.
    """

    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-telegram-integration-"))
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

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        shutil.rmtree(self._root, ignore_errors=True)

    def test_bot_is_recognised_by_its_header(self) -> None:
        found = integrations.by_header("telegram-bot-local")

        self.assertIsNotNone(found)
        self.assertEqual(found.key, "telegram")

    def test_projects_land_in_the_telegram_folder(self) -> None:
        telegram = integrations.REGISTRY["telegram"]

        project = integrations.project(telegram, "chat42:day:2026-08-18", "Телеграм")

        folder = integrations.folder(telegram)
        self.assertEqual(project["folder_id"], folder["id"])
        self.assertEqual(folder["name"], "Телеграм")
        self.assertEqual(project["integration"], "telegram")

    def test_same_day_reuses_the_project(self) -> None:
        """Десять голосовых за день — один проект, а не десять."""
        telegram = integrations.REGISTRY["telegram"]

        first = integrations.project(telegram, "chat42:day:2026-08-18", "Телеграм")
        again = integrations.project(telegram, "chat42:day:2026-08-18", "Телеграм")
        other = integrations.project(telegram, "chat42:day:2026-08-19", "Телеграм")

        self.assertEqual(first["id"], again["id"])
        self.assertNotEqual(first["id"], other["id"])

    def test_two_services_do_not_share_a_folder(self) -> None:
        analyzer = integrations.folder(integrations.REGISTRY["webinar_analyzer"])
        telegram = integrations.folder(integrations.REGISTRY["telegram"])

        self.assertNotEqual(analyzer["id"], telegram["id"])
