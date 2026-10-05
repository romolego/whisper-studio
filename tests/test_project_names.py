"""Имена на диске: папка называется как проект, файл — как запись.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_project_names

Главное здесь не красота имён, а целость путей. Пути в базе абсолютные, и на
них опираются проверки вида «файл обязан лежать внутри проекта»: если после
переезда останется хоть один старый путь, файл станет посторонним и перестанет
и открываться, и удаляться. Поэтому переименование проверяется вместе с тем,
что все записи в базе поехали следом.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from backend import storage


class _StorageCase(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-names-"))
        self._saved = {
            "DB_PATH": storage.DB_PATH,
            "DATA_DIR": storage.DATA_DIR,
            "UPLOADS_DIR": storage.UPLOADS_DIR,
            "TEMP_DIR": storage.TEMP_DIR,
            "DEFAULTS": dict(storage.DEFAULT_SETTINGS),
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

    def tearDown(self) -> None:
        for key in ("DB_PATH", "DATA_DIR", "UPLOADS_DIR", "TEMP_DIR"):
            setattr(storage, key, self._saved[key])
        storage.DEFAULT_SETTINGS.clear()
        storage.DEFAULT_SETTINGS.update(self._saved["DEFAULTS"])
        shutil.rmtree(self._root, ignore_errors=True)

    def _job(self, session_id: str, name: str = "Запись.mp4") -> dict:
        media_dir = storage.session_dir(session_id) / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        source = media_dir / name
        source.write_bytes(b"fake")
        return storage.create_job(
            session_id,
            name,
            source,
            {
                "language": "ru",
                "quality": "accurate",
                "timestamps": "segments",
                "formatting": "formatted",
                "diarization": False,
            },
        )


class SafeNameTests(unittest.TestCase):
    def test_forbidden_characters_are_replaced(self) -> None:
        self.assertEqual(
            storage.safe_file_name('Отчёт: "качество"/2026'),
            "Отчёт_ _качество__2026",
        )

    def test_trailing_dot_and_space_are_dropped(self) -> None:
        # Windows молча срезает их сам, и путь в базе перестал бы совпадать с
        # путём на диске.
        self.assertEqual(storage.safe_file_name("Совещание. "), "Совещание")

    def test_device_names_are_escaped(self) -> None:
        # `CON` и `LPT1` заняты под устройства с времён DOS: файл с таким
        # именем не создаётся вовсе.
        self.assertEqual(storage.safe_file_name("con"), "_con")
        self.assertEqual(storage.safe_file_name("LPT1.mp4"), "_LPT1.mp4")

    def test_empty_name_falls_back(self) -> None:
        self.assertEqual(storage.safe_file_name("  ...  "), "Без названия")

    def test_long_name_is_cut_to_fit_the_path_limit(self) -> None:
        self.assertEqual(len(storage.safe_file_name("а" * 300)), 80)


class ProjectDirectoryTests(_StorageCase):
    def test_new_project_gets_a_readable_folder(self) -> None:
        session = storage.create_session("Планёрка", name_format="title_only")
        self.assertEqual(storage.session_dir(session["id"]).name, "Планёрка")
        self.assertTrue(storage.session_dir(session["id"]).is_dir())

    def test_second_project_with_the_same_name_gets_a_number(self) -> None:
        first = storage.create_session("Планёрка", name_format="title_only")
        second = storage.create_session("Планёрка", name_format="title_only")
        self.assertEqual(storage.session_dir(first["id"]).name, "Планёрка")
        self.assertEqual(storage.session_dir(second["id"]).name, "Планёрка (2)")

    def test_rename_moves_the_folder_and_all_paths(self) -> None:
        session = storage.create_session("Старое", name_format="title_only")
        job = self._job(session["id"])
        outputs_dir = storage.session_dir(session["id"]) / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        result = outputs_dir / "Запись.txt"
        result.write_text("текст", encoding="utf-8")
        storage.add_output(job["id"], "txt", result)

        storage.rename_session(session["id"], "Новое")

        moved = storage.session_dir(session["id"])
        self.assertEqual(moved.name, "Новое")
        self.assertTrue(moved.is_dir())
        fresh = storage.get_job(job["id"])
        self.assertTrue(Path(fresh["stored_path"]).is_file())
        self.assertEqual(Path(fresh["stored_path"]).parent.parent, moved)
        self.assertTrue(Path(fresh["outputs"]["txt"]).is_file())

    def test_source_left_in_place_is_never_rewritten(self) -> None:
        # Режим «оставлять на месте»: исходник лежит вне проекта, и переезд
        # папки не имеет к нему никакого отношения.
        session = storage.create_session("Проект", name_format="title_only")
        outside = self._root / "видео" / "лекция.mp4"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"fake")
        job = storage.create_job(
            session["id"],
            "лекция.mp4",
            outside,
            {
                "language": "ru",
                "quality": "accurate",
                "timestamps": "segments",
                "formatting": "formatted",
                "diarization": False,
            },
            source_path=outside,
            storage_mode="reference",
        )

        storage.rename_session(session["id"], "Переименованный")

        fresh = storage.get_job(job["id"])
        self.assertEqual(Path(fresh["stored_path"]), outside)
        self.assertTrue(outside.is_file())

    def test_busy_project_keeps_its_folder_until_the_file_is_done(self) -> None:
        # Переименовать папку, из которой сейчас читает распознаватель, Windows
        # не даст. Название проекта меняется сразу, папка догоняет позже.
        session = storage.create_session("Занятый", name_format="title_only")
        job = self._job(session["id"])
        storage.update_job(job["id"], status="processing")

        before = storage.session_dir(session["id"])

        storage.rename_session(session["id"], "Другое имя")

        self.assertEqual(storage.session_dir(session["id"]), before)
        self.assertEqual(storage.get_session(session["id"])["name"], "Другое имя")

        storage.update_job(job["id"], status="completed")
        self.assertEqual(
            storage.sync_session_dir(session["id"]).name, "Другое имя"
        )


class JobFileNameTests(_StorageCase):
    def test_base_repeats_the_name_of_the_recording(self) -> None:
        session = storage.create_session("Проект", name_format="title_only")
        job = storage.get_job(self._job(session["id"], "Совещание.mp4")["id"])
        self.assertEqual(storage.job_file_base(job), "Совещание")

    def test_base_stays_the_same_after_the_result_is_written(self) -> None:
        # Файл перезапускают, размечают спикеров, чистят от галлюцинаций —
        # каждый раз должны переписываться те же файлы, а не появляться копии.
        session = storage.create_session("Проект", name_format="title_only")
        job = storage.get_job(self._job(session["id"], "Совещание.mp4")["id"])
        outputs_dir = storage.session_dir(session["id"]) / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        target = outputs_dir / f"{storage.job_file_base(job)}.txt"
        target.write_text("текст", encoding="utf-8")
        storage.add_output(job["id"], "txt", target)

        self.assertEqual(
            storage.job_file_base(storage.get_job(job["id"])),
            "Совещание",
        )

    def test_two_recordings_with_one_name_do_not_collide(self) -> None:
        session = storage.create_session("Проект", name_format="title_only")
        first = storage.get_job(self._job(session["id"], "Запись.mp4")["id"])
        outputs_dir = storage.session_dir(session["id"]) / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / f"{storage.job_file_base(first)}.txt").write_text(
            "текст", encoding="utf-8"
        )
        storage.add_output(
            first["id"], "txt", outputs_dir / "Запись.txt"
        )
        second = storage.get_job(self._job(session["id"], "Запись.mp4")["id"])

        self.assertEqual(storage.job_file_base(second), "Запись (2)")


class LegacyLayoutTests(_StorageCase):
    """Проекты, созданные до понятных имён, разбираются на месте."""

    def _legacy_project(self) -> tuple[dict, dict, Path]:
        session = storage.create_session("Вебинар", name_format="title_only")
        root = storage.session_dir(session["id"])
        job = self._job(session["id"], "Лекция.mp4")

        # Как это выглядело раньше: копия видео названа идентификатором записи,
        # результаты лежат в подпапке с идентификатором задания.
        legacy_media = root / "media" / f"{'a' * 32}.mp4"
        (root / "media" / "Лекция.mp4").rename(legacy_media)
        with storage.connect() as db:
            db.execute(
                "UPDATE jobs SET stored_path = ? WHERE id = ?",
                (str(legacy_media), job["id"]),
            )

        legacy_outputs = root / "outputs" / job["id"]
        legacy_outputs.mkdir(parents=True, exist_ok=True)
        for kind in ("txt", "json"):
            path = legacy_outputs / f"Лекция.{kind}"
            path.write_text("{}", encoding="utf-8")
            storage.add_output(job["id"], kind, path)
        (legacy_outputs / "checkpoint.json").write_text("{}", encoding="utf-8")

        legacy_audio = root / "audio" / f"{job['id']}.wav"
        legacy_audio.parent.mkdir(parents=True, exist_ok=True)
        legacy_audio.write_bytes(b"RIFF")
        storage.set_extracted_audio(job["id"], legacy_audio)
        return session, job, root

    def test_files_move_to_readable_names(self) -> None:
        session, job, root = self._legacy_project()

        storage.sync_readable_names()

        fresh = storage.get_job(job["id"])
        self.assertEqual(Path(fresh["stored_path"]).name, "Лекция.mp4")
        self.assertTrue(Path(fresh["stored_path"]).is_file())
        self.assertEqual(
            Path(fresh["outputs"]["txt"]).parent, root / "outputs"
        )
        self.assertTrue(Path(fresh["outputs"]["txt"]).is_file())
        self.assertEqual(Path(fresh["extracted_audio_path"]).name, "Лекция.wav")
        self.assertTrue(Path(fresh["extracted_audio_path"]).is_file())

    def test_checkpoint_moves_out_of_sight_but_survives(self) -> None:
        _, job, root = self._legacy_project()

        storage.sync_readable_names()

        moved = root / "outputs" / ".checkpoints" / f"{job['id']}.json"
        self.assertTrue(moved.is_file())
        self.assertFalse((root / "outputs" / job["id"]).exists())

    def test_second_pass_changes_nothing(self) -> None:
        _, job, _ = self._legacy_project()

        storage.sync_readable_names()
        first = storage.get_job(job["id"])
        storage.sync_readable_names()
        second = storage.get_job(job["id"])

        self.assertEqual(first["stored_path"], second["stored_path"])
        self.assertEqual(first["outputs"], second["outputs"])
        self.assertEqual(
            first["extracted_audio_path"], second["extracted_audio_path"]
        )


if __name__ == "__main__":
    unittest.main()
