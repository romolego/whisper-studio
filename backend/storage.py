from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .project_locks import project_lock


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
UPLOADS_DIR = DATA_DIR / "sessions"
TEMP_DIR = DATA_DIR / "temp"
DICTATIONS_DIR = DATA_DIR / "dictations"
COOKIES_DIR = DATA_DIR / "cookies"
DB_PATH = DATA_DIR / "whisper.db"

# Размеры страницы для ленты проектов и истории диктовок. «all» — без
# разбиения; остаётся ради небольших библиотек, где страницы только мешают.
PAGE_SIZE_VALUES: set[str] = {"10", "20", "50", "100", "500", "1000", "all"}

DEFAULT_SETTINGS: dict[str, str] = {
    "materials_include_transcript": "on",
    "materials_transcript_format": "txt",
    "default_language": "ru",
    "default_quality": "accurate",
    "default_timestamps": "segments",
    "default_formatting": "formatted",
    "default_export": "docx",
    "default_view_timestamps": "none",
    "default_view_formatting": "formatted",
    "default_view_speakers": "off",
    "default_diarization": "off",
    "dictation_default_language": "ru",
    "dictation_default_quality": "fast",
    "project_name_format": "date_title",
    "project_view": "folders",
    "default_import_mode": "copy",
    # Сколько проектов и сколько сообщений диктовки показывать на одной
    # странице. Лента подгружает ровно эту страницу, а не всю библиотеку:
    # на сотнях проектов разница между «страница» и «всё» — это разница
    # между мгновенным открытием и секундами ожидания на каждом обновлении.
    "projects_page_size": "50",
    "dictation_page_size": "20",
    # Извлечённое аудио — то самое, что уходит в Whisper. Раньше оно всегда
    # жило во временной папке и удалялось после файла. Час записи в mono 16 кГц
    # весит около 115 МБ против трёх гигабайт исходного видео, поэтому вместе с
    # режимом «оставлять на месте» это главный способ не забивать диск.
    "keep_extracted_audio": "off",
    # Качество, которое подставляется под «Авто» при скачивании по ссылке.
    # Раньше это были жёсткие 1080p, и качество приходилось понижать руками
    # при каждой загрузке. «audio» — только звук: для расшифровки этого
    # достаточно, а весит в десятки раз меньше.
    "download_quality": "audio",
    # Ставить ли расшифровку сразу после скачивания по ссылке. Выключается,
    # когда видеокарта занята другим делом: проекты со скачанными записями
    # копятся, а распознавание запускается потом — руками и по одному.
    "download_transcribe": "on",
    # Показывать ли проекты внешних сервисов в общей ленте. По умолчанию скрыты:
    # это чужие расшифровки, они приходят пачками и забивают ленту, а нужны
    # редко. Папка сервиса при этом остаётся на месте — зашёл и посмотрел.
    "integration_feed": "hide",
    # Запускать ли телеграм-бота вместе с сервисом. Токен в data/telegram.json
    # отвечает на вопрос «настроен ли бот», а эта настройка — на вопрос «нужен
    # ли он сейчас»: бот можно отложить, не стирая токен и не отзывая его.
    "telegram_autostart": "on",
    # Откуда брать cookies YouTube, когда он требует подтвердить, что запрос
    # не от робота. По умолчанию — ниоткуда: cookies браузера привязывают
    # загрузки к личному аккаунту Google, а YouTube за выгрузки аккаунт
    # ограничивает, поэтому решение остаётся за владельцем. Обычные загрузки
    # обходятся без этого — сервис перебирает клиентов API сам.
    "youtube_cookies": "off",
    "diarization_device": "auto",
    # Новые задания используют Nemotron; явно сохранённый выбор сохраняется.
    "diarization_engine": "nemotron",
    "theme": "light",
    "managed_storage_root": str(UPLOADS_DIR),
    "temporary_storage_root": str(TEMP_DIR),
}

SETTING_VALUES: dict[str, set[str]] = {
    "materials_include_transcript": {"on", "off"},
    "materials_transcript_format": {"txt", "docx"},
    "default_language": {"ru", "en", "auto"},
    "default_quality": {"fast", "accurate"},
    "default_timestamps": {"none", "segments", "words"},
    "default_formatting": {"formatted", "plain"},
    "default_export": {"docx", "md", "txt", "srt", "vtt", "json"},
    "default_view_timestamps": {"none", "segments", "words"},
    "default_view_formatting": {"formatted", "plain"},
    "default_view_speakers": {"off", "on"},
    "default_diarization": {"off", "on"},
    "dictation_default_language": {"ru", "en", "auto"},
    "dictation_default_quality": {"fast", "accurate"},
    "project_name_format": {
        "date_title",
        "date_compact",
        "date_dotted",
        "date_time_title",
        "title_date",
        "title_only",
    },
    "project_view": {"flat", "folders"},
    "default_import_mode": {"copy", "reference"},
    "projects_page_size": PAGE_SIZE_VALUES,
    "dictation_page_size": PAGE_SIZE_VALUES,
    "keep_extracted_audio": {"off", "on"},
    "download_quality": {"audio", "360", "480", "720", "1080", "max"},
    "download_transcribe": {"on", "off"},
    "integration_feed": {"show", "hide"},
    "telegram_autostart": {"on", "off"},
    "youtube_cookies": {
        "off",
        "auto",
        "firefox",
        "chrome",
        "edge",
        "opera",
        "brave",
        "vivaldi",
        "chromium",
    },
    "diarization_device": {"auto", "cuda", "cpu"},
    "diarization_engine": {"pyannote", "nemotron"},
    "theme": {"light", "dark", "auto"},
}
PATH_SETTING_KEYS = {"managed_storage_root", "temporary_storage_root"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _column_names(db: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                storage_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                source_path TEXT,
                storage_mode TEXT NOT NULL DEFAULT 'managed',
                extracted_audio_path TEXT,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                options_json TEXT NOT NULL,
                detected_language TEXT,
                duration_seconds REAL,
                processing_seconds REAL,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                UNIQUE(job_id, kind)
            );

            CREATE TABLE IF NOT EXISTS speaker_aliases (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                speaker_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(job_id, speaker_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS media_uploads (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                source_path TEXT,
                storage_mode TEXT NOT NULL DEFAULT 'managed',
                size_bytes INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                format_name TEXT NOT NULL,
                streams_json TEXT NOT NULL,
                video_stream_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dictation_messages (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS dictation_fragments (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL
                    REFERENCES dictation_messages(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                audio_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                duration_seconds REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                transcript TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'ru',
                quality TEXT NOT NULL DEFAULT 'accurate',
                detected_language TEXT,
                processing_seconds REAL,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );
            """
        )

        folder_columns = _column_names(db, "folders")
        if "integration" not in folder_columns:
            # Папка внешнего сервиса. Отдельная колонка, а не вывод по проектам
            # внутри: пустая папка сервиса тоже должна быть защищена от удаления.
            db.execute("ALTER TABLE folders ADD COLUMN integration TEXT")
        if "feed_visible" not in folder_columns:
            # Показывать ли проекты этой папки в общей ленте. Сама папка из
            # навигации не исчезает — зашёл и увидел всё содержимое. Это
            # ровно то, чего не хватало архиву: он нужен раз в месяц, а
            # ленту забивал каждый день.
            db.execute(
                "ALTER TABLE folders ADD COLUMN feed_visible "
                "INTEGER NOT NULL DEFAULT 1"
            )

        session_columns = _column_names(db, "sessions")
        if "folder_id" not in session_columns:
            db.execute(
                "ALTER TABLE sessions ADD COLUMN folder_id TEXT "
                "REFERENCES folders(id) ON DELETE SET NULL"
            )
        if "storage_path" not in session_columns:
            db.execute("ALTER TABLE sessions ADD COLUMN storage_path TEXT")
        for row in db.execute(
            "SELECT id, storage_path FROM sessions"
        ).fetchall():
            current = row["storage_path"] or ""
            legacy_broken = f"{UPLOADS_DIR}{row['id']}"
            if not current or current == legacy_broken:
                db.execute(
                    "UPDATE sessions SET storage_path = ? WHERE id = ?",
                    (str(UPLOADS_DIR / row["id"]), row["id"]),
                )

        job_columns = _column_names(db, "jobs")
        if "position" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
            )
            db.execute("UPDATE jobs SET position = rowid WHERE position = 0")
        if "source_path" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN source_path TEXT")
        if "storage_mode" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN storage_mode TEXT "
                "NOT NULL DEFAULT 'managed'"
            )
        if "device" not in job_columns:
            # На чём считал Whisper: cuda или cpu. Раньше это вычислялось в
            # transcriber и выбрасывалось, поэтому узнать, что распознавание
            # ушло на процессор и стало в 20 раз медленнее, было негде.
            db.execute("ALTER TABLE jobs ADD COLUMN device TEXT")
        if "extracted_audio_path" not in job_columns:
            # Путь к сохранённому извлечённому аудио. NULL означает «аудио не
            # сохранялось»: либо настройка выключена, либо дорожку не извлекали
            # вовсе (Whisper читал исходный файл напрямую).
            db.execute(
                "ALTER TABLE jobs ADD COLUMN extracted_audio_path TEXT"
            )

        session_columns = _column_names(db, "sessions")
        if "integration" not in session_columns:
            # Какой внешний сервис создал проект. NULL — обычный проект,
            # созданный в интерфейсе.
            db.execute("ALTER TABLE sessions ADD COLUMN integration TEXT")
        if "integration_external_id" not in session_columns:
            # Идентификатор проекта на стороне внешнего сервиса: по нему
            # файлы одного разбора попадают в один и тот же проект.
            db.execute(
                "ALTER TABLE sessions ADD COLUMN integration_external_id TEXT"
            )

        dictation_columns = _column_names(db, "dictation_messages")
        if "project_id" not in dictation_columns:
            # Проект, в который диктовку сохранили «на будущее». NULL — не
            # сохраняли. Связь намеренно без внешнего ключа: удаление проекта не
            # должно тянуть за собой диктовку, она живёт своей жизнью.
            db.execute(
                "ALTER TABLE dictation_messages ADD COLUMN project_id TEXT"
            )

        media_columns = _column_names(db, "media_uploads")
        if "source_path" not in media_columns:
            db.execute("ALTER TABLE media_uploads ADD COLUMN source_path TEXT")
        if "storage_mode" not in media_columns:
            db.execute(
                "ALTER TABLE media_uploads ADD COLUMN storage_mode TEXT "
                "NOT NULL DEFAULT 'managed'"
            )

        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS jobs_session_position_idx
                ON jobs(session_id, position);
            CREATE INDEX IF NOT EXISTS sessions_folder_idx
                ON sessions(folder_id);
            CREATE INDEX IF NOT EXISTS speaker_aliases_job_idx
                ON speaker_aliases(job_id);
            CREATE INDEX IF NOT EXISTS dictation_fragments_message_position_idx
                ON dictation_fragments(message_id, position);
            CREATE INDEX IF NOT EXISTS dictation_fragments_status_idx
                ON dictation_fragments(status, created_at);
            """
        )
        db.execute(
            """
            UPDATE jobs
            SET status = 'queued', progress = 0, started_at = NULL,
                error = NULL
            WHERE status = 'processing'
            """
        )
        db.execute(
            """
            UPDATE dictation_fragments
            SET status = 'queued', started_at = NULL,
                error = NULL
            WHERE status = 'processing'
            """
        )
        for key, value in DEFAULT_SETTINGS.items():
            db.execute(
                "INSERT OR IGNORE INTO settings(key, value_json) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    DICTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    sync_readable_names()


def get_settings() -> dict[str, str]:
    settings = dict(DEFAULT_SETTINGS)
    with connect() as db:
        rows = db.execute("SELECT key, value_json FROM settings").fetchall()
    for row in rows:
        try:
            value = json.loads(row["value_json"])
        except json.JSONDecodeError:
            continue
        if row["key"] in SETTING_VALUES and value in SETTING_VALUES[row["key"]]:
            settings[row["key"]] = value
        elif row["key"] in PATH_SETTING_KEYS and isinstance(value, str) and value:
            settings[row["key"]] = value
    # Пути только для показа. Их нельзя менять, поэтому в DEFAULT_SETTINGS и
    # SETTING_VALUES их нет; интерфейсу они нужны, чтобы не держать у себя
    # захардкоженные «E:\whisper\...».
    settings["dictations_root"] = str(DICTATIONS_DIR)
    settings["cookies_root"] = str(COOKIES_DIR)
    settings["database_path"] = str(DB_PATH)
    return settings


def update_settings(values: dict[str, str]) -> dict[str, str]:
    for key, value in values.items():
        if key in SETTING_VALUES:
            if value not in SETTING_VALUES[key]:
                raise ValueError(f"Недопустимое значение настройки {key}")
        elif key in PATH_SETTING_KEYS:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError("Папка хранения должна иметь полный путь")
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"Не удалось создать папку «{path}»: {exc}") from exc
            values[key] = str(path.resolve())
        else:
            raise ValueError(f"Неизвестная настройка {key}")
    with connect() as db:
        for key, value in values.items():
            db.execute(
                """
                INSERT INTO settings(key, value_json) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                """,
                (key, json.dumps(value, ensure_ascii=False)),
            )
    return get_settings()


def create_folder(name: str, integration: str | None = None) -> dict[str, Any]:
    folder_id = uuid.uuid4().hex
    stamp = utc_now()
    clean_name = name.strip() or "Новая папка"
    with connect() as db:
        db.execute(
            """
            INSERT INTO folders(id, name, created_at, updated_at, integration)
            VALUES (?, ?, ?, ?, ?)
            """,
            (folder_id, clean_name, stamp, stamp, integration),
        )
    return get_folder(folder_id)


def _folder_from_row(row: sqlite3.Row) -> dict[str, Any]:
    folder = dict(row)
    # В SQLite это 0/1, а в интерфейсе — галочка. Приводим здесь, чтобы
    # JSON отдавал настоящий true/false и на клиенте не гадали.
    folder["feed_visible"] = bool(folder.get("feed_visible", 1))
    return folder


def get_folder(folder_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """
            SELECT f.*, COUNT(s.id) AS project_count
            FROM folders f
            LEFT JOIN sessions s ON s.folder_id = f.id
            WHERE f.id = ?
            GROUP BY f.id
            """,
            (folder_id,),
        ).fetchone()
    if row is None:
        raise KeyError(folder_id)
    return _folder_from_row(row)


def list_folders() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT f.*, COUNT(s.id) AS project_count
            FROM folders f
            LEFT JOIN sessions s ON s.folder_id = f.id
            GROUP BY f.id
            ORDER BY f.name COLLATE NOCASE
            """
        ).fetchall()
    return [_folder_from_row(row) for row in rows]


def mark_folder_integration(folder_id: str, integration: str | None) -> None:
    """Отметить папку как принадлежащую внешнему сервису."""
    with connect() as db:
        result = db.execute(
            "UPDATE folders SET integration = ?, updated_at = ? WHERE id = ?",
            (integration, utc_now(), folder_id),
        )
        if result.rowcount == 0:
            raise KeyError(folder_id)


def update_folder(
    folder_id: str,
    *,
    name: str | None = None,
    feed_visible: bool | None = None,
) -> dict[str, Any]:
    assignments: list[str] = []
    values: list[Any] = []
    if name is not None:
        assignments.append("name = ?")
        values.append(name.strip() or "Новая папка")
    if feed_visible is not None:
        assignments.append("feed_visible = ?")
        values.append(1 if feed_visible else 0)
    with connect() as db:
        if not assignments:
            if not db.execute(
                "SELECT 1 FROM folders WHERE id = ?", (folder_id,)
            ).fetchone():
                raise KeyError(folder_id)
        else:
            assignments.append("updated_at = ?")
            values.append(utc_now())
            values.append(folder_id)
            result = db.execute(
                f"UPDATE folders SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if result.rowcount == 0:
                raise KeyError(folder_id)
    return get_folder(folder_id)


def rename_folder(folder_id: str, name: str) -> dict[str, Any]:
    return update_folder(folder_id, name=name)


def delete_folder(folder_id: str, delete_projects: bool = False) -> None:
    project_ids: list[str] = []
    with connect() as db:
        row = db.execute(
            "SELECT integration FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        if not row:
            raise KeyError(folder_id)
        if row["integration"]:
            # Папку внешнего сервиса удалять нельзя: сервис создаст её заново при
            # следующем обращении, а вместе с папкой ушли бы и расшифровки,
            # которых в самом сервисе может уже не быть.
            raise RuntimeError(
                "Это папка внешнего сервиса — её нельзя удалить. "
                "Убрать её из общей ленты можно галочкой в самой папке "
                "или настройкой сразу для всех сервисов."
            )
        if delete_projects:
            project_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM sessions WHERE folder_id = ?", (folder_id,)
                ).fetchall()
            ]
            active = db.execute(
                """
                SELECT COUNT(*) AS count
                FROM jobs
                WHERE session_id IN (
                    SELECT id FROM sessions WHERE folder_id = ?
                ) AND status = 'processing'
                """,
                (folder_id,),
            ).fetchone()["count"]
            if active:
                raise RuntimeError(
                    "В папке есть проект, который сейчас распознаётся"
                )
        else:
            db.execute(
                "UPDATE sessions SET folder_id = NULL WHERE folder_id = ?",
                (folder_id,),
            )
            db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    if delete_projects:
        for project_id in project_ids:
            delete_session(project_id)
        with connect() as db:
            db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))


def _make_project_name(title: str | None, name_format: str | None) -> str:
    clean_title = (title or "").strip() or "Новый проект"
    selected = name_format or get_settings()["project_name_format"]
    now = datetime.now()
    if selected == "date_title":
        return f"{now:%Y} - {now:%m} - {now:%d} - {clean_title}"
    if selected == "date_compact":
        return f"{now:%Y-%m-%d} — {clean_title}"
    if selected == "date_dotted":
        return f"{now:%d.%m.%Y} — {clean_title}"
    if selected == "date_time_title":
        return f"{now:%Y-%m-%d} {now:%H-%M} — {clean_title}"
    if selected == "title_date":
        return f"{clean_title} — {now:%Y-%m-%d}"
    return clean_title


def create_session(
    name: str | None = None,
    folder_id: str | None = None,
    name_format: str | None = None,
    integration: str | None = None,
    integration_external_id: str | None = None,
) -> dict[str, Any]:
    session_id = uuid.uuid4().hex
    stamp = utc_now()
    clean_name = _make_project_name(name, name_format)
    project_path = Path(get_settings()["managed_storage_root"]) / session_id
    with connect() as db:
        if folder_id and not db.execute(
            "SELECT 1 FROM folders WHERE id = ?", (folder_id,)
        ).fetchone():
            raise KeyError(folder_id)
        db.execute(
            """
            INSERT INTO sessions(
                id, name, folder_id, storage_path, created_at, updated_at,
                integration, integration_external_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                clean_name,
                folder_id,
                str(project_path),
                stamp,
                stamp,
                integration,
                integration_external_id,
            ),
        )
    # Папка называется как проект. Сразу подставить имя в INSERT нельзя:
    # свободное имя ищется среди уже занятых, а для этого проект должен
    # существовать в базе.
    sync_session_dir(session_id)
    session_dir(session_id).mkdir(parents=True, exist_ok=True)
    return get_session(session_id)


def find_session_by_integration(
    integration: str,
    external_id: str | None,
) -> dict[str, Any] | None:
    """Проект внешнего сервиса по его собственному идентификатору.

    Без external_id ищется единственный проект этой интеграции: когда сервис не
    различает свои проекты, все его файлы собираются в один.
    """
    with connect() as db:
        if external_id:
            row = db.execute(
                """
                SELECT id FROM sessions
                WHERE integration = ? AND integration_external_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (integration, external_id),
            ).fetchone()
        else:
            row = db.execute(
                """
                SELECT id FROM sessions
                WHERE integration = ? AND integration_external_id IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (integration,),
            ).fetchone()
    return get_session(row["id"]) if row else None


def mark_session_integration(
    session_id: str,
    integration: str | None,
    external_id: str | None,
) -> None:
    """Отметить, что проект принадлежит внешнему сервису."""
    with connect() as db:
        result = db.execute(
            """
            UPDATE sessions
            SET integration = ?, integration_external_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (integration, external_id, utc_now(), session_id),
        )
        if result.rowcount == 0:
            raise KeyError(session_id)


def integration_usage() -> dict[str, dict[str, Any]]:
    """Сколько проектов и файлов пришло от каждого внешнего сервиса."""
    with connect() as db:
        rows = db.execute(
            """
            SELECT s.integration AS key,
                   COUNT(DISTINCT s.id) AS projects,
                   COUNT(j.id) AS files,
                   MAX(COALESCE(j.finished_at, j.created_at, s.created_at))
                       AS last_used_at
            FROM sessions s
            LEFT JOIN jobs j ON j.session_id = s.id
            WHERE s.integration IS NOT NULL
            GROUP BY s.integration
            """
        ).fetchall()
    return {
        str(row["key"]): {
            "projects": int(row["projects"] or 0),
            "files": int(row["files"] or 0),
            "last_used_at": row["last_used_at"],
        }
        for row in rows
    }


def session_dir(session_id: str) -> Path:
    with connect() as db:
        row = db.execute(
            "SELECT storage_path FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if row and row["storage_path"]:
        return Path(row["storage_path"])
    return UPLOADS_DIR / session_id


# Имена на диске: папка проекта называется как проект, файл — как запись.
#
# Раньше на диске лежали идентификаторы: папка проекта — шестнадцатеричный
# uuid, скопированное видео — тоже uuid, папка результатов — идентификатор
# задания. Внутри сервиса это удобно, снаружи бесполезно: найти нужную
# расшифровку в проводнике было нельзя, а сервис как стадия конвейера только
# этим наружу и повёрнут.
#
# Ограничения тут не выдуманные, а windows-овые. Полный путь не должен
# переваливать за 260 знаков, поэтому имена режутся: `data\sessions\` плюс имя
# проекта (80) плюс `outputs\` плюс имя файла (80) плюс расширение — это 200 с
# небольшим, есть запас. Символы `<>:"/\|?*` в имени файла запрещены,
# пробел и точка в конце молча отбрасываются самой системой, а имена вроде
# `CON` и `LPT1` заняты под устройства с времён DOS и не создаются вовсе.
NAME_LIMIT = 80
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_file_name(
    value: str,
    limit: int = NAME_LIMIT,
    fallback: str = "Без названия",
) -> str:
    """Имя файла или папки, которое Windows точно примет."""
    clean = _INVALID_NAME_CHARS.sub("_", value or "")
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    clean = clean[:limit].strip(" .")
    if not clean:
        return fallback
    if clean.upper().split(".")[0] in _RESERVED_NAMES:
        return f"_{clean}"
    return clean


def unique_child(folder: Path, name: str, suffix: str = "") -> Path:
    """Свободный путь в папке: к занятому имени добавляется номер."""
    candidate = folder / f"{name}{suffix}"
    index = 2
    while candidate.exists():
        candidate = folder / f"{name} ({index}){suffix}"
        index += 1
    return candidate


def project_directory_name(session_id: str, name: str) -> str:
    """Как назвать папку проекта, чтобы не столкнуться с соседями.

    Названия проектов не уникальны — два «Созвона» в разных папках нормальны.
    На диске они разойдутся номером, как это делает сам проводник.
    """
    base = safe_file_name(name, NAME_LIMIT, "Проект")
    root = Path(get_settings()["managed_storage_root"])
    with connect() as db:
        taken = {
            row["storage_path"]
            for row in db.execute(
                "SELECT storage_path FROM sessions WHERE id != ?",
                (session_id,),
            ).fetchall()
            if row["storage_path"]
        }
    current = str(session_dir(session_id))
    candidate = base
    index = 2
    while str(root / candidate) != current and (
        str(root / candidate) in taken or (root / candidate).exists()
    ):
        candidate = f"{base} ({index})"
        index += 1
    return candidate


DOWNLOAD_STATE_NAME = "url_download.json"
_LIVE_DOWNLOAD_STATUSES = {
    "queued",
    "downloading",
    "resuming",
    "extracting",
}


def _is_busy(session_id: str, root: Path) -> bool:
    """Можно ли сейчас трогать папку проекта.

    Нельзя ровно в двух случаях: файл распознаётся (ffmpeg и Whisper держат
    открытыми исходник и выводы) или идёт скачивание по ссылке (yt-dlp пишет
    в `media` по пути, который запомнил при старте). Файл в очереди помехой не
    является: распознаватель читает пути из базы в момент, когда берётся за
    работу, а не заранее.
    """
    with connect() as db:
        row = db.execute(
            """
            SELECT COUNT(*) AS count FROM jobs
            WHERE session_id = ? AND status = 'processing'
            """,
            (session_id,),
        ).fetchone()
    if row["count"]:
        return True
    try:
        state = json.loads(
            (root / DOWNLOAD_STATE_NAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return state.get("status") in _LIVE_DOWNLOAD_STATUSES


def _rewrite_download_state(root: Path, old: Path, new: Path) -> None:
    """Перевести пути приостановленной загрузки на новую папку.

    Загрузка запоминает, куда пишет, в своём файле состояния. Если его не
    поправить, продолжение после паузы попыталось бы дописать файл по пути,
    которого больше нет, и начало бы качать заново.
    """
    state_path = root / DOWNLOAD_STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return
    changed = False
    for key in ("out_template", "filename"):
        moved = _moved_path(state.get(key), old, new)
        if moved:
            state[key] = moved
            changed = True
    if changed:
        try:
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return


def _moved_path(value: Any, old: Path, new: Path) -> str | None:
    """Путь после переезда папки — или None, если он лежал вне её."""
    if not value:
        return None
    try:
        relative = Path(str(value)).relative_to(old)
    except ValueError:
        return None
    return str(new / relative)


def _rewrite_session_paths(session_id: str, old: Path, new: Path) -> None:
    """Перевести все записанные в базе пути проекта на новую папку.

    Пути в базе абсолютные, и на них опираются проверки вида «файл обязан
    лежать внутри проекта». Если после переезда останется хоть один старый
    путь, файл будет считаться посторонним и перестанет удаляться и
    открываться. Поэтому переписываются все колонки разом.

    Исходники в режиме «оставлять на месте» лежат вне проекта: их пути
    `_moved_path` не трогает, потому что они не начинаются с папки проекта.
    """
    with connect() as db:
        db.execute(
            "UPDATE sessions SET storage_path = ? WHERE id = ?",
            (str(new), session_id),
        )
        for table, column in (
            ("jobs", "stored_path"),
            ("jobs", "extracted_audio_path"),
            ("jobs", "source_path"),
            ("media_uploads", "stored_path"),
            ("media_uploads", "source_path"),
        ):
            for row in db.execute(
                f"SELECT id, {column} FROM {table} WHERE session_id = ?",
                (session_id,),
            ).fetchall():
                moved = _moved_path(row[column], old, new)
                if moved:
                    db.execute(
                        f"UPDATE {table} SET {column} = ? WHERE id = ?",
                        (moved, row["id"]),
                    )
        for row in db.execute(
            """
            SELECT rowid AS row_id, path FROM outputs
            WHERE job_id IN (SELECT id FROM jobs WHERE session_id = ?)
            """,
            (session_id,),
        ).fetchall():
            moved = _moved_path(row["path"], old, new)
            if moved:
                db.execute(
                    "UPDATE outputs SET path = ? WHERE rowid = ?",
                    (moved, row["row_id"]),
                )


def sync_session_dir(session_id: str) -> Path:
    with project_lock(session_id):
        return _sync_session_dir(session_id)


def _sync_session_dir(session_id: str) -> Path:
    """Привести папку проекта в соответствие с его названием.

    Вызывается при создании, переименовании и по завершении распознавания.
    Переезд не делается, пока в проекте есть файл в очереди или в работе:
    распознаватель держит открытыми и исходник, и выводы, и Windows такую
    папку переименовать не даст. Ничего страшного в этом нет — папка догонит
    название на ближайшем завершении файла.
    """
    current = session_dir(session_id)
    try:
        session = get_session(session_id)
        root = Path(get_settings()["managed_storage_root"])
        target = root / project_directory_name(session_id, session["name"])
    except (KeyError, OSError, sqlite3.Error):
        return current
    if current == target or _is_busy(session_id, current):
        return current
    try:
        root.mkdir(parents=True, exist_ok=True)
        if current.exists():
            current.rename(target)
        else:
            target.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Папка занята: открыт файл, работает проводник, антивирус держит
        # выводы. Название проекта уже сменилось, папка переедет позже.
        return current
    try:
        _rewrite_session_paths(session_id, current, target)
        _rewrite_download_state(target, current, target)
    except Exception:
        # Пути в базе не переписались. Оставить папку переехавшей нельзя:
        # сервис искал бы файлы по старым путям и не нашёл ни одного, — поэтому
        # переезд откатывается целиком.
        try:
            target.rename(current)
        except OSError:
            pass
        return current
    return target


def keep_extracted_audio() -> bool:
    """Сохранять ли извлечённое аудио в проекте вместо временной папки."""
    return get_settings()["keep_extracted_audio"] == "on"


def extracted_audio_dir(session_id: str) -> Path:
    """Папка проекта для сохранённого аудио.

    Лежит внутри проекта, а не во временной папке, поэтому удаляется вместе с
    проектом (delete_session сносит каталог целиком) и переживает перезапуск.
    """
    path = session_dir(session_id) / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


_TECHNICAL_STEM = re.compile(r"^[0-9a-f]{32}$")


def _move_file(source: Path, target: Path) -> Path | None:
    """Переложить файл. None, если не вышло — тогда всё остаётся как было."""
    if source == target:
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    except OSError:
        return None
    return target


def _repoint(table: str, column: str, row_id: Any, path: Path) -> None:
    key = "rowid" if table == "outputs" else "id"
    with connect() as db:
        db.execute(
            f"UPDATE {table} SET {column} = ? WHERE {key} = ?",
            (str(path), row_id),
        )


def _rename_session_files(session_id: str) -> None:
    """Дать понятные имена файлам проекта, которые уже лежат на диске.

    Первые версии сервиса раскладывали файлы по идентификаторам: копия видео
    называлась идентификатором записи, результаты лежали в подпапке с
    идентификатором задания. Разбирать такой проект в проводнике нельзя.

    Проход идемпотентный: файл, который уже назван правильно, не трогается,
    поэтому он спокойно повторяется при каждом запуске и заодно чинит то, что
    не удалось переложить в прошлый раз (например открытый в плеере файл).
    """
    root = session_dir(session_id)
    if not root.exists():
        return
    outputs_root = root / "outputs"
    with connect() as db:
        jobs = db.execute(
            "SELECT * FROM jobs WHERE session_id = ?", (session_id,)
        ).fetchall()
        uploads = db.execute(
            "SELECT * FROM media_uploads WHERE session_id = ?", (session_id,)
        ).fetchall()

    for job in jobs:
        outputs = {}
        with connect() as db:
            rows = db.execute(
                "SELECT rowid AS row_id, kind, path FROM outputs WHERE job_id = ?",
                (job["id"],),
            ).fetchall()
        # Результаты переезжают из подпапки задания в общую папку проекта.
        for row in rows:
            source = Path(row["path"])
            if not source.is_file() or source.parent == outputs_root:
                outputs[row["kind"]] = row["path"]
                continue
            moved = _move_file(source, outputs_root / source.name)
            if moved:
                _repoint("outputs", "path", row["row_id"], moved)
                outputs[row["kind"]] = str(moved)

        # Чекпоинт незаконченного файла — в скрытую папку, к остальным.
        legacy_checkpoint = outputs_root / job["id"] / "checkpoint.json"
        if legacy_checkpoint.is_file():
            _move_file(
                legacy_checkpoint,
                outputs_root / ".checkpoints" / f"{job['id']}.json",
            )
        legacy_dir = outputs_root / job["id"]
        if legacy_dir.is_dir():
            try:
                legacy_dir.rmdir()
            except OSError:
                pass

        base = job_file_base({**dict(job), "outputs": outputs})
        stored = Path(job["stored_path"]) if job["stored_path"] else None
        if (
            job["storage_mode"] == "managed"
            and stored
            and stored.is_file()
            and _TECHNICAL_STEM.match(stored.stem)
        ):
            moved = _move_file(
                stored,
                unique_child(stored.parent, base, stored.suffix),
            )
            if moved:
                _repoint("jobs", "stored_path", job["id"], moved)

        audio = (
            Path(job["extracted_audio_path"])
            if job["extracted_audio_path"]
            else None
        )
        if audio and audio.is_file() and _TECHNICAL_STEM.match(audio.stem):
            moved = _move_file(
                audio, unique_child(audio.parent, base, audio.suffix)
            )
            if moved:
                _repoint("jobs", "extracted_audio_path", job["id"], moved)

    for upload in uploads:
        stored = Path(upload["stored_path"]) if upload["stored_path"] else None
        if (
            upload["storage_mode"] != "managed"
            or not stored
            or not stored.is_file()
            or not _TECHNICAL_STEM.match(stored.stem)
        ):
            continue
        base = safe_file_name(
            Path(upload["original_name"]).stem, NAME_LIMIT, "Запись"
        )
        moved = _move_file(
            stored, unique_child(stored.parent, base, stored.suffix)
        )
        if moved:
            _repoint("media_uploads", "stored_path", upload["id"], moved)


def sync_readable_names() -> None:
    """Привести имена на диске в соответствие с базой — при каждом запуске.

    Сбой на одном проекте не должен мешать остальным и тем более срывать
    запуск сервиса: проект просто останется с прежними именами до следующего
    раза.
    """
    with connect() as db:
        ids = [
            row["id"] for row in db.execute("SELECT id FROM sessions").fetchall()
        ]
    for session_id in ids:
        try:
            sync_session_dir(session_id)
            _rename_session_files(session_id)
        except Exception:
            continue


def job_file_base(job: dict[str, Any]) -> str:
    """Общее имя всех файлов задания — как называется сама запись.

    Имя обязано быть одним и тем же при каждой записи результата: файл
    перезапускают заново, размечают спикеров, чистят от галлюцинаций, и каждый
    раз должны переписываться те же файлы, а не появляться копии с номером.
    Поэтому уже занятое имя берётся из готовых выводов, а новое подбирается
    один раз — по названию записи.
    """
    for kind in ("txt", "json", "md", "docx"):
        existing = (job.get("outputs") or {}).get(kind)
        if existing:
            return Path(existing).stem
    base = safe_file_name(
        Path(job["original_name"]).stem, NAME_LIMIT, "Запись"
    )
    folder = session_dir(job["session_id"]) / "outputs"
    folder.mkdir(parents=True, exist_ok=True)
    return unique_child(folder, base, ".txt").stem


def job_audio_path(job: dict[str, Any]) -> Path:
    """Куда класть извлечённое аудио задания."""
    existing = job.get("extracted_audio_path")
    if existing:
        return Path(existing)
    return extracted_audio_dir(job["session_id"]) / f"{job_file_base(job)}.wav"


def set_extracted_audio(job_id: str, path: Path | None) -> None:
    update_job(job_id, extracted_audio_path=str(path) if path else None)


def drop_extracted_audio(job_id: str) -> bool:
    """Удалить сохранённое аудио задания. True, если файл действительно был.

    Путь обязан лежать внутри каталога проекта: очистка не должна уметь удалять
    что-то за его пределами, даже если в базу попал произвольный путь.
    """
    try:
        job = get_job(job_id)
    except KeyError:
        return False
    value = job.get("extracted_audio_path")
    if not value:
        return False
    path = Path(value)
    removed = False
    try:
        resolved = path.resolve()
        root = session_dir(job["session_id"]).resolve()
        if root in resolved.parents and resolved.is_file():
            resolved.unlink()
            removed = True
    except OSError:
        removed = False
    set_extracted_audio(job_id, None)
    return removed


def temporary_dir() -> Path:
    path = Path(get_settings()["temporary_storage_root"])
    path.mkdir(parents=True, exist_ok=True)
    return path


PROJECT_SELECT = """
    SELECT s.*, f.name AS folder_name,
           COUNT(j.id) AS job_count,
           SUM(CASE WHEN j.status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
           SUM(CASE WHEN j.status = 'queued' THEN 1 ELSE 0 END) AS queued_count,
           SUM(CASE WHEN j.status = 'processing' THEN 1 ELSE 0 END) AS processing_count,
           SUM(CASE WHEN j.status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
           SUM(CASE WHEN j.status = 'cancelled' THEN 1 ELSE 0 END)
               AS cancelled_count,
           COALESCE(SUM(j.duration_seconds), 0) AS duration_seconds
    FROM sessions s
    LEFT JOIN folders f ON f.id = s.folder_id
    LEFT JOIN jobs j ON j.session_id = s.id
"""


def get_session(session_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            PROJECT_SELECT + " WHERE s.id = ? GROUP BY s.id",
            (session_id,),
        ).fetchone()
    if row is None:
        raise KeyError(session_id)
    return dict(row)


def _session_filters(
    folder_id: str | None | object = ...,
    hide_integrations: bool = False,
    hide_quiet_folders: bool = False,
) -> tuple[str, list[Any]]:
    """Условия отбора проектов для ленты.

    `folder_id` разделяет три случая: `...` — не фильтровать вовсе, `None` —
    только проекты без папки, строка — конкретная папка. Скрытия действуют
    только на общую ленту: зайдя в папку, человек хочет увидеть её целиком,
    даже если из ленты она убрана.
    """
    clauses: list[str] = []
    values: list[Any] = []
    if folder_id is not ...:
        if folder_id is None:
            clauses.append("s.folder_id IS NULL")
        else:
            clauses.append("s.folder_id = ?")
            values.append(folder_id)
    if hide_integrations:
        clauses.append("s.integration IS NULL")
    if hide_quiet_folders:
        clauses.append(
            "(s.folder_id IS NULL OR s.folder_id IN "
            "(SELECT id FROM folders WHERE feed_visible = 1))"
        )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, values


def count_sessions(
    folder_id: str | None | object = ...,
    hide_integrations: bool = False,
    hide_quiet_folders: bool = False,
) -> int:
    where, values = _session_filters(
        folder_id, hide_integrations, hide_quiet_folders
    )
    with connect() as db:
        row = db.execute(
            f"SELECT COUNT(*) AS total FROM sessions s{where}", values
        ).fetchone()
    return int(row["total"])


def list_sessions(
    folder_id: str | None | object = ...,
    hide_integrations: bool = False,
    hide_quiet_folders: bool = False,
    order: str = "recent",
    offset: int = 0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    where, values = _session_filters(
        folder_id, hide_integrations, hide_quiet_folders
    )
    # «folder» повторяет порядок, в котором лента рисует группы: сначала
    # папки по алфавиту, «Без папки» в конце. Только при таком порядке
    # страница режет список по границам групп, а не рассыпает одну папку
    # по трём страницам.
    if order == "folder":
        ordering = (
            " ORDER BY (s.folder_id IS NULL), f.name COLLATE NOCASE,"
            " s.updated_at DESC"
        )
    else:
        ordering = " ORDER BY s.updated_at DESC"
    query = PROJECT_SELECT + where + " GROUP BY s.id" + ordering
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        values = [*values, int(limit), max(0, int(offset))]
    with connect() as db:
        rows = db.execute(query, values).fetchall()
    return [dict(row) for row in rows]


def update_session(
    session_id: str,
    name: str | None = None,
    folder_id: str | None | object = ...,
) -> dict[str, Any]:
    values: list[Any] = []
    assignments: list[str] = []
    with connect() as db:
        if name is not None:
            assignments.append("name = ?")
            values.append(name.strip() or "Новый проект")
        if folder_id is not ...:
            if folder_id and not db.execute(
                "SELECT 1 FROM folders WHERE id = ?", (folder_id,)
            ).fetchone():
                raise KeyError(str(folder_id))
            assignments.append("folder_id = ?")
            values.append(folder_id)
        if assignments:
            assignments.append("updated_at = ?")
            values.append(utc_now())
            values.append(session_id)
            result = db.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if result.rowcount == 0:
                raise KeyError(session_id)
        elif not db.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone():
            raise KeyError(session_id)
    if name is not None:
        sync_session_dir(session_id)
    return get_session(session_id)


def rename_session(session_id: str, name: str) -> dict[str, Any]:
    return update_session(session_id, name=name)


def delete_session(session_id: str) -> None:
    with project_lock(session_id):
        _delete_session(session_id)


def _delete_session(session_id: str) -> None:
    path = session_dir(session_id).resolve()
    session = get_session(session_id)
    root = path.parent.resolve()
    if root not in path.parents:
        raise ValueError("Некорректный путь проекта")
    with connect() as db:
        row = db.execute(
            """
            SELECT s.id,
                   SUM(CASE WHEN j.status = 'processing' THEN 1 ELSE 0 END) AS active
            FROM sessions s
            LEFT JOIN jobs j ON j.session_id = s.id
            WHERE s.id = ?
            GROUP BY s.id
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        if row["active"]:
            raise RuntimeError("Дождитесь завершения текущего файла")
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    if path.exists():
        shutil.rmtree(path)


def create_media(
    media_id: str,
    session_id: str,
    original_name: str,
    stored_path: Path,
    metadata: dict[str, Any],
    source_path: Path | None = None,
    storage_mode: str = "managed",
) -> dict[str, Any]:
    if storage_mode not in {"managed", "reference"}:
        raise ValueError("Некорректный режим хранения")
    with connect() as db:
        db.execute(
            """
            INSERT INTO media_uploads(
                id, session_id, original_name, stored_path, size_bytes,
                duration_seconds, format_name, streams_json,
                video_stream_count, source_path, storage_mode, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                media_id,
                session_id,
                original_name,
                str(stored_path),
                int(metadata["size_bytes"]),
                float(metadata["duration_seconds"]),
                str(metadata["format_name"]),
                json.dumps(metadata["audio_streams"], ensure_ascii=False),
                int(metadata["video_stream_count"]),
                str(source_path) if source_path else None,
                storage_mode,
                utc_now(),
            ),
        )
        db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (utc_now(), session_id),
        )
    return get_media(media_id)


def _media_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["audio_streams"] = json.loads(item.pop("streams_json"))
    return item


def get_media(media_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM media_uploads WHERE id = ?", (media_id,)
        ).fetchone()
    if row is None:
        raise KeyError(media_id)
    return _media_from_row(row)


def list_media(session_id: str | None = None) -> list[dict[str, Any]]:
    with connect() as db:
        if session_id:
            rows = db.execute(
                """
                SELECT * FROM media_uploads
                WHERE session_id = ?
                ORDER BY rowid
                """,
                (session_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM media_uploads ORDER BY rowid"
            ).fetchall()
    return [_media_from_row(row) for row in rows]


def remove_media_record(media_id: str) -> None:
    with connect() as db:
        result = db.execute("DELETE FROM media_uploads WHERE id = ?", (media_id,))
        if result.rowcount == 0:
            raise KeyError(media_id)


def delete_media(media_id: str) -> None:
    item = get_media(media_id)
    path = Path(item["stored_path"]).resolve()
    remove_media_record(media_id)
    if item.get("storage_mode") == "managed":
        root = session_dir(item["session_id"]).resolve()
        if root not in path.parents:
            raise ValueError("Некорректный путь медиафайла")
        if path.exists():
            path.unlink()


def create_job(
    session_id: str,
    original_name: str,
    stored_path: Path,
    options: dict[str, Any],
    source_path: Path | None = None,
    storage_mode: str = "managed",
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    stamp = utc_now()
    with connect() as db:
        position = db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM jobs WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        db.execute(
            """
            INSERT INTO jobs(
                id, session_id, original_name, stored_path, status,
                progress, options_json, source_path, storage_mode,
                created_at, position
            ) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                session_id,
                original_name,
                str(stored_path),
                json.dumps(options, ensure_ascii=False),
                str(source_path) if source_path else None,
                storage_mode,
                stamp,
                position,
            ),
        )
        db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (stamp, session_id),
        )
    return get_job(job_id)


def update_job(job_id: str, **values: Any) -> None:
    if not values:
        return
    allowed = {
        "status",
        "progress",
        "detected_language",
        "duration_seconds",
        "processing_seconds",
        "device",
        "extracted_audio_path",
        "error",
        "started_at",
        "finished_at",
    }
    invalid = set(values) - allowed
    if invalid:
        raise ValueError(f"Unsupported job fields: {sorted(invalid)}")
    columns = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [job_id]
    with connect() as db:
        db.execute(f"UPDATE jobs SET {columns} WHERE id = ?", params)
        if "status" in values:
            db.execute(
                """
                UPDATE sessions SET updated_at = ?
                WHERE id = (SELECT session_id FROM jobs WHERE id = ?)
                """,
                (utc_now(), job_id),
            )


def add_output(job_id: str, kind: str, path: Path) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO outputs(job_id, kind, path)
            VALUES (?, ?, ?)
            ON CONFLICT(job_id, kind) DO UPDATE SET path = excluded.path
            """,
            (job_id, kind, str(path)),
        )


def list_speaker_aliases(project_id: str) -> dict[tuple[str, str], str]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT a.job_id, a.speaker_id, a.display_name
            FROM speaker_aliases a
            JOIN jobs j ON j.id = a.job_id
            WHERE j.session_id = ?
            ORDER BY j.position, a.speaker_id
            """,
            (project_id,),
        ).fetchall()
    return {
        (str(row["job_id"]), str(row["speaker_id"])): str(row["display_name"])
        for row in rows
    }


def set_speaker_alias(
    project_id: str,
    job_id: str,
    speaker_id: str,
    display_name: str,
) -> None:
    clean_name = display_name.strip()
    with connect() as db:
        job = db.execute(
            "SELECT 1 FROM jobs WHERE id = ? AND session_id = ?",
            (job_id, project_id),
        ).fetchone()
        if job is None:
            raise KeyError(job_id)
        if clean_name:
            db.execute(
                """
                INSERT INTO speaker_aliases(
                    job_id, speaker_id, display_name, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, speaker_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = excluded.updated_at
                """,
                (job_id, speaker_id, clean_name, utc_now()),
            )
        else:
            db.execute(
                """
                DELETE FROM speaker_aliases
                WHERE job_id = ? AND speaker_id = ?
                """,
                (job_id, speaker_id),
            )
        db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (utc_now(), project_id))


def _job_from_row(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["options"] = json.loads(job.pop("options_json"))
    outputs = db.execute(
        "SELECT kind, path FROM outputs WHERE job_id = ? ORDER BY kind",
        (job["id"],),
    ).fetchall()
    job["outputs"] = {output["kind"]: output["path"] for output in outputs}
    return job


def get_job(job_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job_from_row(db, row)


def list_jobs(session_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
    with connect() as db:
        if session_id:
            rows = db.execute(
                """
                SELECT * FROM jobs
                WHERE session_id = ?
                ORDER BY position, rowid
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM jobs ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_job_from_row(db, row) for row in rows]


def cancel_queued_job(job_id: str) -> str:
    """Снять с очереди задачу, которая ещё не начала выполняться.

    Возвращает состояние, в котором задача была найдена: 'queued' — успешно
    помечена как остановленная, 'processing' — остановку должен подхватить
    воркер, остальное — останавливать уже нечего.
    """
    with connect() as db:
        row = db.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        status = str(row["status"])
        if status == "queued":
            result = db.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', progress = 0, error = NULL,
                    finished_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (utc_now(), job_id),
            )
            if result.rowcount == 0:
                latest = db.execute(
                    "SELECT status FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                return str(latest["status"]) if latest else status
            db.execute(
                """
                UPDATE sessions SET updated_at = ?
                WHERE id = (SELECT session_id FROM jobs WHERE id = ?)
                """,
                (utc_now(), job_id),
            )
        return status


def requeue_job(job_id: str) -> dict[str, Any]:
    """Вернуть остановленную или неудачную задачу в конец очереди."""
    with connect() as db:
        row = db.execute(
            "SELECT status, session_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        if str(row["status"]) in {"queued", "processing"}:
            raise RuntimeError("Задание уже в очереди")
        db.execute(
            """
            UPDATE jobs
            SET status = 'queued', progress = 0, error = NULL,
                started_at = NULL, finished_at = NULL,
                processing_seconds = NULL
            WHERE id = ?
            """,
            (job_id,),
        )
        db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (utc_now(), row["session_id"]),
        )
    return get_job(job_id)


def prepare_job_rerun(
    job_id: str,
    overrides: dict[str, Any],
    rerun_mode: str,
) -> dict[str, Any]:
    if rerun_mode not in {"transcribe", "diarization"}:
        raise ValueError("Некорректный режим повторной обработки")
    with connect() as db:
        row = db.execute(
            "SELECT status, session_id, options_json FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        if str(row["status"]) != "completed":
            raise RuntimeError("Повторно обработать можно только готовый файл")
        current_options = json.loads(row["options_json"])
        next_options = {
            **current_options,
            **overrides,
            "_rerun_mode": rerun_mode,
            "_previous_options": current_options,
        }
        db.execute(
            """
            UPDATE jobs
            SET status = 'queued', progress = 0, error = NULL,
                started_at = NULL, finished_at = NULL,
                options_json = ?
            WHERE id = ?
            """,
            (json.dumps(next_options, ensure_ascii=False), job_id),
        )
        db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (utc_now(), row["session_id"]),
        )
    return get_job(job_id)


def set_job_options(job_id: str, options: dict[str, Any]) -> None:
    with connect() as db:
        result = db.execute(
            "UPDATE jobs SET options_json = ? WHERE id = ?",
            (json.dumps(options, ensure_ascii=False), job_id),
        )
        if result.rowcount == 0:
            raise KeyError(job_id)


def clear_speaker_aliases(job_id: str) -> None:
    with connect() as db:
        db.execute(
            "DELETE FROM speaker_aliases WHERE job_id = ?",
            (job_id,),
        )


def cancel_queued_dictation_fragment(fragment_id: str) -> str:
    """Аналог cancel_queued_job для фрагмента диктовки."""
    with connect() as db:
        row = db.execute(
            "SELECT status FROM dictation_fragments WHERE id = ?",
            (fragment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(fragment_id)
        status = str(row["status"])
        if status == "queued":
            db.execute(
                """
                UPDATE dictation_fragments
                SET status = 'cancelled', error = NULL, finished_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (utc_now(), fragment_id),
            )
        return status


def queued_job_ids() -> list[str]:
    with connect() as db:
        rows = db.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY rowid"
        ).fetchall()
    return [row["id"] for row in rows]


def dictation_message_dir(message_id: str) -> Path:
    return DICTATIONS_DIR / message_id


def _dictation_fragment_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _dictation_message_from_row(
    db: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    message = dict(row)
    fragments = db.execute(
        """
        SELECT * FROM dictation_fragments
        WHERE message_id = ?
        ORDER BY position, rowid
        """,
        (message["id"],),
    ).fetchall()
    message["fragments"] = [
        _dictation_fragment_from_row(fragment) for fragment in fragments
    ]
    # Проект, если диктовку в него сохраняли. Ссылка не проверяется на живость
    # здесь: проект могли удалить, и это выясняется при переходе.
    message["project_id"] = message.get("project_id")
    return message


def set_dictation_project(message_id: str, project_id: str | None) -> None:
    with connect() as db:
        result = db.execute(
            """
            UPDATE dictation_messages
            SET project_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (project_id, utc_now(), message_id),
        )
        if result.rowcount == 0:
            raise KeyError(message_id)


def create_dictation_message() -> dict[str, Any]:
    message_id = uuid.uuid4().hex
    stamp = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO dictation_messages(
                id, text, status, created_at, updated_at
            ) VALUES (?, '', 'draft', ?, ?)
            """,
            (message_id, stamp, stamp),
        )
    dictation_message_dir(message_id).mkdir(parents=True, exist_ok=True)
    return get_dictation_message(message_id)


def get_dictation_message(message_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM dictation_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return _dictation_message_from_row(db, row)


def count_dictation_messages(status: str | None = None) -> int:
    with connect() as db:
        if status is None:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM dictation_messages"
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM dictation_messages "
                "WHERE status = ?",
                (status,),
            ).fetchone()
    return int(row["total"])


def list_dictation_messages(
    limit: int | None = 500,
    *,
    status: str | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = " WHERE status = ?" if status else ""
    values: list[Any] = [status] if status else []
    query = (
        "SELECT * FROM dictation_messages"
        + where
        + """
        ORDER BY
            CASE status WHEN 'draft' THEN 0 ELSE 1 END,
            updated_at DESC
        """
    )
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        values += [int(limit), max(0, int(offset))]
    with connect() as db:
        rows = db.execute(query, values).fetchall()
        return [_dictation_message_from_row(db, row) for row in rows]


def update_dictation_message(
    message_id: str,
    *,
    text: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    if status is not None and status not in {"draft", "confirmed"}:
        raise ValueError("Некорректный статус диктовки")
    assignments: list[str] = []
    values: list[Any] = []
    if text is not None:
        assignments.append("text = ?")
        values.append(text)
    if status is not None:
        assignments.append("status = ?")
        values.append(status)
        assignments.append("confirmed_at = ?")
        values.append(utc_now() if status == "confirmed" else None)
    assignments.append("updated_at = ?")
    values.append(utc_now())
    values.append(message_id)
    with connect() as db:
        result = db.execute(
            f"""
            UPDATE dictation_messages
            SET {", ".join(assignments)}
            WHERE id = ?
            """,
            values,
        )
        if result.rowcount == 0:
            raise KeyError(message_id)
    return get_dictation_message(message_id)


def delete_dictation_message(message_id: str) -> None:
    path = dictation_message_dir(message_id).resolve()
    root = DICTATIONS_DIR.resolve()
    if root not in path.parents:
        raise ValueError("Некорректный путь диктовки")
    with connect() as db:
        active = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM dictation_fragments
            WHERE message_id = ? AND status = 'processing'
            """,
            (message_id,),
        ).fetchone()["count"]
        if active:
            raise RuntimeError("Дождитесь окончания распознавания фрагмента")
        result = db.execute(
            "DELETE FROM dictation_messages WHERE id = ?",
            (message_id,),
        )
        if result.rowcount == 0:
            raise KeyError(message_id)
    if path.exists():
        shutil.rmtree(path)


def clear_confirmed_dictation_messages() -> int:
    with connect() as db:
        rows = db.execute(
            """
            SELECT id
            FROM dictation_messages
            WHERE status = 'confirmed'
            ORDER BY updated_at ASC
            """
        ).fetchall()
    deleted = 0
    for row in rows:
        delete_dictation_message(str(row["id"]))
        deleted += 1
    return deleted


def create_dictation_fragment(
    fragment_id: str,
    message_id: str,
    audio_path: Path,
    mime_type: str,
    duration_seconds: float,
    language: str,
    quality: str,
    *,
    status: str = "queued",
    error: str | None = None,
) -> dict[str, Any]:
    if language not in {"auto", "ru", "en"}:
        raise ValueError("Некорректный язык")
    if quality not in {"fast", "accurate"}:
        raise ValueError("Некорректный режим")
    stamp = utc_now()
    with connect() as db:
        message = db.execute(
            "SELECT status FROM dictation_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if message is None:
            raise KeyError(message_id)
        if message["status"] != "draft":
            raise RuntimeError("Подтверждённую диктовку нельзя продолжить")
        position = db.execute(
            """
            SELECT COALESCE(MAX(position), 0) + 1
            FROM dictation_fragments
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()[0]
        db.execute(
            """
            INSERT INTO dictation_fragments(
                id, message_id, position, audio_path, mime_type,
                duration_seconds, status, language, quality,
                error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fragment_id,
                message_id,
                position,
                str(audio_path),
                mime_type,
                max(0.0, float(duration_seconds)),
                status,
                language,
                quality,
                error,
                stamp,
            ),
        )
        db.execute(
            """
            UPDATE dictation_messages
            SET updated_at = ?
            WHERE id = ?
            """,
            (stamp, message_id),
        )
    return get_dictation_fragment(fragment_id)


def get_dictation_fragment(fragment_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM dictation_fragments WHERE id = ?",
            (fragment_id,),
        ).fetchone()
    if row is None:
        raise KeyError(fragment_id)
    return _dictation_fragment_from_row(row)


def update_dictation_fragment(
    fragment_id: str,
    **values: Any,
) -> dict[str, Any]:
    allowed = {
        "status",
        "error",
        "started_at",
        "finished_at",
        "processing_seconds",
        "detected_language",
    }
    invalid = set(values) - allowed
    if invalid:
        raise ValueError(f"Unsupported dictation fields: {sorted(invalid)}")
    if not values:
        return get_dictation_fragment(fragment_id)
    columns = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [fragment_id]
    with connect() as db:
        result = db.execute(
            f"UPDATE dictation_fragments SET {columns} WHERE id = ?",
            params,
        )
        if result.rowcount == 0:
            raise KeyError(fragment_id)
    return get_dictation_fragment(fragment_id)


def complete_dictation_fragment(
    fragment_id: str,
    transcript: str,
    detected_language: str,
    processing_seconds: float,
) -> dict[str, Any]:
    clean = transcript.strip()
    stamp = utc_now()
    with connect() as db:
        row = db.execute(
            """
            SELECT message_id, status
            FROM dictation_fragments
            WHERE id = ?
            """,
            (fragment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(fragment_id)
        if row["status"] == "completed":
            pass
        else:
            db.execute(
                """
                UPDATE dictation_fragments
                SET status = 'completed', transcript = ?,
                    detected_language = ?, processing_seconds = ?,
                    error = NULL, finished_at = ?
                WHERE id = ?
                """,
                (
                    clean,
                    detected_language,
                    processing_seconds,
                    stamp,
                    fragment_id,
                ),
            )
            if clean:
                current = db.execute(
                    "SELECT text FROM dictation_messages WHERE id = ?",
                    (row["message_id"],),
                ).fetchone()["text"]
                separator = "\n\n" if current.strip() else ""
                db.execute(
                    """
                    UPDATE dictation_messages
                    SET text = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        current.rstrip() + separator + clean,
                        stamp,
                        row["message_id"],
                    ),
                )
    return get_dictation_fragment(fragment_id)


def _text_without_fragment(text: str, transcript: str) -> str:
    """Убрать вклад одного фрагмента из текста сообщения.

    Вырезается именно вхождение, а не пересобирается весь текст: текст сообщения
    пользователь правит руками, и сборка заново стёрла бы правки. Если после
    правок вхождение уже не находится, текст остаётся как есть — тогда лишний
    кусок убирается вручную, и это честнее, чем потерять всё остальное.
    """
    if not transcript:
        return text
    position = text.find(transcript)
    if position < 0:
        return text
    remainder = text[:position] + text[position + len(transcript) :]
    # После вырезания остаются двойные разделители: «\n\n\n\n» между соседями.
    remainder = re.sub(r"\n{3,}", "\n\n", remainder)
    return remainder.strip()


def delete_dictation_fragment(fragment_id: str) -> dict[str, Any]:
    """Убрать фрагмент из сообщения вместе с его аудио.

    Нужно, когда один фрагмент из длинной диктовки записан зря: раньше избавиться
    от него можно было только удалив всё сообщение целиком.
    """
    fragment = get_dictation_fragment(fragment_id)
    message_id = fragment["message_id"]
    transcript = (fragment.get("transcript") or "").strip()
    audio = Path(fragment["audio_path"])

    with connect() as db:
        result = db.execute(
            "DELETE FROM dictation_fragments WHERE id = ?", (fragment_id,)
        )
        if result.rowcount == 0:
            raise KeyError(fragment_id)
        if transcript:
            row = db.execute(
                "SELECT text FROM dictation_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row:
                trimmed = _text_without_fragment(row["text"], transcript)
                if trimmed != row["text"]:
                    db.execute(
                        """
                        UPDATE dictation_messages
                        SET text = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (trimmed, utc_now(), message_id),
                    )

    # Файл мог быть ещё открыт распознаванием: на Windows удаление занятого
    # файла не проходит. Запись в базе уже удалена, поэтому из интерфейса
    # фрагмент исчезнет в любом случае; на файл даётся несколько попыток.
    try:
        resolved = audio.resolve()
        inside = DICTATIONS_DIR.resolve() in resolved.parents
    except OSError:
        inside = False
    if inside:
        for attempt in range(4):
            try:
                resolved.unlink(missing_ok=True)
                break
            except OSError:
                if attempt == 3:
                    break
                time.sleep(0.3)

    return get_dictation_message(message_id)


def queued_dictation_fragment_ids() -> list[str]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT id FROM dictation_fragments
            WHERE status = 'queued'
            ORDER BY created_at, position
            """
        ).fetchall()
    return [row["id"] for row in rows]
