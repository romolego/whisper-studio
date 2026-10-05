"""Страницы ленты и диктовки, видимость папки в общей ленте.

Три требования, которые проверяются здесь:

* лента отдаёт ровно запрошенную страницу, а не всю библиотеку, и сообщает
  общее количество заголовком — иначе пагинатор не знает, сколько страниц;
* папка со снятой галочкой уходит из общей ленты, но внутри себя показывает
  всё: скрытие — это архив, а не удаление;
* черновик диктовки приходит на любой странице истории: это рабочее место,
  и переключение страницы не должно его прятать.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_pagination_and_folder_feed
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import Response

from backend import app as backend_app
from backend import storage


class PaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-pages-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "TEMP_DIR": storage.TEMP_DIR,
            "DICTATIONS_DIR": storage.DICTATIONS_DIR,
            "COOKIES_DIR": storage.COOKIES_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
        }
        storage.DATA_DIR = self._root
        storage.DB_PATH = self._root / "test.db"
        storage.UPLOADS_DIR = self._root / "sessions"
        storage.TEMP_DIR = self._root / "temp"
        storage.DICTATIONS_DIR = self._root / "dictations"
        storage.COOKIES_DIR = self._root / "cookies"
        storage.DEFAULT_SETTINGS["managed_storage_root"] = str(
            storage.UPLOADS_DIR
        )
        storage.DEFAULT_SETTINGS["temporary_storage_root"] = str(
            storage.TEMP_DIR
        )
        storage.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        storage.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        storage.init_db()

    def tearDown(self) -> None:
        for key in (
            "DB_PATH",
            "DATA_DIR",
            "UPLOADS_DIR",
            "TEMP_DIR",
            "DICTATIONS_DIR",
            "COOKIES_DIR",
        ):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        shutil.rmtree(self._root, ignore_errors=True)

    def _projects(self, count: int, folder_id: str | None = None) -> list[str]:
        return [
            storage.create_session(f"Проект {index}", folder_id)["id"]
            for index in range(count)
        ]

    def _page(self, **kwargs) -> tuple[list[dict], int]:
        response = Response()
        items = backend_app.projects(response, **kwargs)
        return items, int(response.headers["X-Total-Count"])

    # --- лента проектов ---

    def test_page_returns_only_requested_slice(self) -> None:
        self._projects(25)

        first, total = self._page(page=1, per_page=10)
        second, _ = self._page(page=2, per_page=10)
        third, _ = self._page(page=3, per_page=10)

        self.assertEqual(total, 25, "общее количество приходит заголовком")
        self.assertEqual([len(first), len(second), len(third)], [10, 10, 5])
        seen = {item["id"] for item in first + second + third}
        self.assertEqual(len(seen), 25, "страницы не пересекаются")

    def test_without_per_page_returns_everything(self) -> None:
        self._projects(7)

        items, total = self._page()

        self.assertEqual(len(items), 7)
        self.assertEqual(total, 7)

    def test_folder_filter_counts_only_its_projects(self) -> None:
        folder = storage.create_folder("Архив")
        self._projects(3, folder["id"])
        self._projects(2)

        inside, total = self._page(folder=folder["id"], per_page=10)
        loose, loose_total = self._page(folder="none", per_page=10)

        self.assertEqual((len(inside), total), (3, 3))
        self.assertEqual((len(loose), loose_total), (2, 2))

    def test_folder_order_keeps_groups_together(self) -> None:
        beta = storage.create_folder("Бета")
        alpha = storage.create_folder("Альфа")
        self._projects(2, beta["id"])
        self._projects(2, alpha["id"])
        self._projects(1)

        items, _ = self._page(order="folder", per_page=10)
        names = [item["folder_name"] for item in items]

        self.assertEqual(
            names,
            ["Альфа", "Альфа", "Бета", "Бета", None],
            "папки по алфавиту, «без папки» в конце",
        )

    # --- видимость папки в общей ленте ---

    def test_hidden_folder_leaves_feed_but_stays_reachable(self) -> None:
        folder = storage.create_folder("Архив")
        self._projects(4, folder["id"])
        self._projects(2)

        storage.update_folder(folder["id"], feed_visible=False)

        feed, feed_total = self._page(per_page=50)
        inside, inside_total = self._page(folder=folder["id"], per_page=50)

        self.assertEqual((len(feed), feed_total), (2, 2))
        self.assertEqual(
            (len(inside), inside_total),
            (4, 4),
            "внутри папки видно всё, что в ней есть",
        )
        self.assertFalse(storage.get_folder(folder["id"])["feed_visible"])

    def test_folder_returns_to_feed(self) -> None:
        folder = storage.create_folder("Архив")
        self._projects(3, folder["id"])
        storage.update_folder(folder["id"], feed_visible=False)

        storage.update_folder(folder["id"], feed_visible=True)

        _, total = self._page(per_page=50)
        self.assertEqual(total, 3)

    def test_integration_projects_hidden_from_feed_only(self) -> None:
        """Раньше этот отбор жил в интерфейсе; теперь его делает сервер."""
        folder = storage.create_folder("Сервис", integration="webinar")
        service_ids = self._projects(3, folder["id"])
        for project_id in service_ids:
            storage.mark_session_integration(project_id, "webinar", None)
        self._projects(2)

        feed, feed_total = self._page(per_page=50)
        inside, inside_total = self._page(folder=folder["id"], per_page=50)

        self.assertEqual((len(feed), feed_total), (2, 2))
        self.assertEqual(
            (len(inside), inside_total),
            (3, 3),
            "внутри папки сервиса видно всё",
        )

        storage.update_settings({"integration_feed": "show"})
        _, shown_total = self._page(per_page=50)
        self.assertEqual(shown_total, 5)

    def test_patch_endpoint_changes_only_given_fields(self) -> None:
        folder = storage.create_folder("Архив")

        hidden = backend_app.edit_folder(
            folder["id"], backend_app.FolderPatch(feed_visible=False)
        )
        self.assertEqual(hidden["name"], "Архив", "имя не тронуто")
        self.assertFalse(hidden["feed_visible"])

        renamed = backend_app.edit_folder(
            folder["id"], backend_app.FolderPatch(name="Старое")
        )
        self.assertEqual(renamed["name"], "Старое")
        self.assertFalse(
            renamed["feed_visible"], "галочка пережила переименование"
        )

    # --- история диктовок ---

    def _dictation_page(self, **kwargs) -> tuple[list[dict], int]:
        response = Response()
        items = backend_app.dictation_messages(response, **kwargs)
        return items, int(response.headers["X-Total-Count"])

    def test_dictation_page_keeps_draft_on_every_page(self) -> None:
        for index in range(12):
            message = storage.create_dictation_message()
            storage.update_dictation_message(
                message["id"], text=f"Сообщение {index}", status="confirmed"
            )
        draft = storage.create_dictation_message()

        first, total = self._dictation_page(page=1, per_page=5)
        third, _ = self._dictation_page(page=3, per_page=5)

        self.assertEqual(total, 12, "считаются только подтверждённые")
        self.assertEqual(len(first), 6, "черновик сверх страницы")
        self.assertEqual(len(third), 3, "хвост истории плюс черновик")
        for page in (first, third):
            self.assertIn(draft["id"], {item["id"] for item in page})

    def test_dictation_pages_do_not_overlap(self) -> None:
        ids = []
        for index in range(9):
            message = storage.create_dictation_message()
            storage.update_dictation_message(
                message["id"], text=f"Сообщение {index}", status="confirmed"
            )
            ids.append(message["id"])

        seen: set[str] = set()
        for page in (1, 2, 3):
            items, _ = self._dictation_page(page=page, per_page=3)
            seen |= {item["id"] for item in items}

        self.assertEqual(seen, set(ids))


if __name__ == "__main__":
    unittest.main()
