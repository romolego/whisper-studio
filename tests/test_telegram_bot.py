"""Телеграм-бот: приём, очередь, выдача и отказы.

Тесты нужны здесь больше обычного: у бота нет способа проверить себя вручную
без интернета и включённого VPN, а половина логики — это как раз поведение при
недоступной сети и недоступном сервисе. Поэтому и Телеграм, и локальный Whisper
здесь подменены заглушками, а проверяется то, что видит человек в чате.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_telegram_bot
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

import requests

from backend import storage
from telegram_bot import bot as bot_module
from telegram_bot.bot import Bot
from telegram_bot.botapi import BotApi, BotApiError, BotNetworkError, split_text
from telegram_bot.config import Config, ConfigError, load, looks_like_token
from telegram_bot.whisper import WhisperUnavailable


class FakeBotApi:
    """Телеграм: помнит, что отправлено, и отдаёт заранее заготовленное."""

    def __init__(self) -> None:
        self.updates: list[list[dict[str, Any]]] = []
        self.sent: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[str] = []
        self.files: dict[str, str] = {}
        self.downloads = 0
        self._next_id = 100

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        return self.updates.pop(0) if self.updates else []

    def get_file(self, file_id: str) -> dict[str, Any]:
        return {"file_path": self.files.get(file_id, f"voice/{file_id}.oga")}

    def download(self, file_path: str, destination: Path) -> Path:
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audio")
        return destination

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        buttons: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        message = {
            "message_id": self._next_id,
            "chat_id": chat_id,
            "text": text,
            "buttons": buttons,
        }
        self.sent.append(message)
        return message

    def send_long_text(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        buttons: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self.send_message(chat_id, part, buttons=buttons)
            for part in split_text(text)
        ]

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: dict[str, Any] | None = None,
    ) -> None:
        self.edits.append({"message_id": message_id, "text": text})

    def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str | None = None,
        reply_to: int | None = None,
        buttons: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.documents.append(
            {
                "name": path.name,
                "caption": caption,
                "buttons": buttons,
                "existed": path.exists(),
            }
        )
        return {"message_id": 1}

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append(text)

    # --- удобные проверки ---

    @property
    def texts(self) -> list[str]:
        return [item["text"] for item in self.sent]

    @property
    def last_edit(self) -> str:
        return self.edits[-1]["text"] if self.edits else ""


class FakeWhisper:
    """Локальный сервис: очередь из словарей вместо базы и видеокарты."""

    def __init__(self) -> None:
        self.jobs_by_id: dict[str, dict[str, Any]] = {}
        self.texts: dict[str, str] = {}
        self.projects: list[dict[str, Any]] = []
        self.enqueued: list[dict[str, Any]] = []
        self.downloads: dict[str, dict[str, Any]] = {}
        self.exports: list[tuple[str, str]] = []
        self.probe_result: dict[str, Any] = {"title": "Вебинар", "duration_sec": 3600}
        self.offline = False
        self.health_payload = {"ok": True, "queued": 0, "worker_alive": True}

    def _guard(self) -> None:
        if self.offline:
            raise WhisperUnavailable("соединение отклонено")

    def health(self) -> dict[str, Any]:
        self._guard()
        return self.health_payload

    def alive(self) -> bool:
        return not self.offline

    def project(self, external_id: str, name: str) -> dict[str, Any]:
        self._guard()
        project = {"id": f"p{len(self.projects) + 1}", "name": name, "external": external_id}
        self.projects.append(project)
        return project

    def enqueue_file(
        self,
        project_id: str,
        path: Path,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        self._guard()
        job_id = f"j{len(self.enqueued) + 1}"
        self.enqueued.append(
            {
                "project_id": project_id,
                "name": path.name,
                "exists": path.exists(),
                "options": options,
            }
        )
        self.jobs_by_id[job_id] = {
            "id": job_id,
            "status": "queued",
            "queue_position": 1,
            "original_name": path.name,
        }
        return self.jobs_by_id[job_id]

    def job(self, job_id: str) -> dict[str, Any]:
        self._guard()
        return self.jobs_by_id[job_id]

    def jobs(self, project_id: str) -> list[dict[str, Any]]:
        self._guard()
        return [
            job
            for job in self.jobs_by_id.values()
            if job.get("project_id", project_id) == project_id
        ]

    def text(self, job_id: str) -> str:
        self._guard()
        return self.texts.get(job_id, "")

    def export(self, job_id: str, kind: str, destination: Path) -> Path:
        self._guard()
        self.exports.append((job_id, kind))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"export")
        return destination

    def probe_url(self, url: str, passcode: str | None = None) -> dict[str, Any]:
        self._guard()
        return self.probe_result

    def start_url_download(
        self,
        url: str,
        project_id: str,
        quality: str,
        options: dict[str, Any],
        passcode: str | None = None,
    ) -> dict[str, Any]:
        self._guard()
        self.downloads[project_id] = {"status": "downloading", "percent": 10}
        return {"project": {"id": project_id}}

    def url_download_status(self, project_id: str) -> dict[str, Any]:
        self._guard()
        return self.downloads.get(project_id, {"status": "idle"})


def voice_message(message_id: int = 5, size: int = 400_000) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "chat": {"id": 42},
        "from": {"id": 7},
        "voice": {
            "file_id": "AAA",
            "file_unique_id": "U1",
            "file_size": size,
            "mime_type": "audio/ogg",
        },
    }


def document_message(name: str, size: int = 1000) -> dict[str, Any]:
    return {
        "message_id": 6,
        "chat": {"id": 42},
        "from": {"id": 7},
        "document": {
            "file_id": "BBB",
            "file_unique_id": "U2",
            "file_size": size,
            "file_name": name,
        },
    }


def text_message(text: str, user_id: int = 7) -> dict[str, Any]:
    return {
        "message_id": 7,
        "chat": {"id": 42},
        "from": {"id": user_id},
        "text": text,
    }


class BotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-telegram-"))
        self._saved_work = bot_module.WORK_DIR
        bot_module.WORK_DIR = self._root / "work"
        self.config = Config(
            token="test-token",
            setup_code="secret",
            allowed_users=[7],
            path=self._root / "telegram.json",
        )
        self.api = FakeBotApi()
        self.whisper = FakeWhisper()
        self.bot = Bot(
            self.config,
            api=self.api,
            whisper=self.whisper,
            state_path=self._root / "state.json",
        )

    def tearDown(self) -> None:
        bot_module.WORK_DIR = self._saved_work
        shutil.rmtree(self._root, ignore_errors=True)

    def complete(self, job_id: str, text: str) -> None:
        self.whisper.jobs_by_id[job_id]["status"] = "completed"
        self.whisper.texts[job_id] = text


class AccessTests(BotTestCase):
    def test_stranger_is_refused(self) -> None:
        self.bot.handle_message(text_message("привет", user_id=999))
        self.assertIn("личный бот", self.api.texts[-1])
        self.assertEqual(self.config.allowed_users, [7])

    def test_setup_code_binds_user(self) -> None:
        self.bot.handle_message(text_message("secret", user_id=999))
        self.assertIn(999, self.config.allowed_users)
        # Привязка переживает перезапуск: она сразу уходит в файл настроек.
        self.assertIn("999", self.config.path.read_text(encoding="utf-8"))

    def test_stranger_cannot_press_buttons(self) -> None:
        self.bot.handle_callback(
            {
                "id": "cb",
                "from": {"id": 999},
                "message": {"message_id": 1, "chat": {"id": 42}},
                "data": "fmt:docx:j1",
            }
        )
        self.assertEqual(self.api.answers, ["Это личный бот"])
        self.assertEqual(self.whisper.exports, [])


class VoiceTests(BotTestCase):
    def test_voice_goes_to_queue(self) -> None:
        self.bot.handle_message(voice_message())
        self.assertEqual(self.api.downloads, 1)
        self.assertEqual(len(self.whisper.enqueued), 1)
        # Файл должен существовать в момент постановки в очередь и не остаться
        # на диске после: сервис забирает его себе.
        self.assertTrue(self.whisper.enqueued[0]["exists"])
        self.assertFalse(list((bot_module.WORK_DIR / "incoming").glob("*")))
        self.assertIn("очереди", self.api.last_edit)

    def test_too_big_file_is_refused_before_download(self) -> None:
        self.bot.handle_message(voice_message(size=30 * 1024 * 1024))
        self.assertEqual(self.api.downloads, 0)
        self.assertEqual(self.whisper.enqueued, [])
        self.assertIn("Пришлите ссылку", self.api.texts[-1])

    def test_local_api_server_has_no_limit(self) -> None:
        self.config.api_base = "http://127.0.0.1:8081"
        self.bot.handle_message(voice_message(size=300 * 1024 * 1024))
        self.assertEqual(self.api.downloads, 1)

    def test_non_media_document_is_refused(self) -> None:
        self.bot.handle_message(document_message("договор.pdf"))
        self.assertEqual(self.api.downloads, 0)
        self.assertIn("не звук и не видео", self.api.texts[-1])

    def test_service_down_is_explained(self) -> None:
        self.whisper.offline = True
        self.bot.handle_message(voice_message())
        self.assertIn("не отвечает", self.api.last_edit)
        self.assertEqual(self.bot.pending, [])


class DeliveryTests(BotTestCase):
    def test_short_text_comes_as_message(self) -> None:
        self.bot.handle_message(voice_message())
        self.complete("j1", "Короткая расшифровка.")
        self.bot.check_pending()

        self.assertIn("Короткая расшифровка.", self.api.texts)
        self.assertEqual(self.api.documents, [])
        self.assertEqual(self.bot.pending, [])
        # Под ответом лежат остальные форматы, но не тот, которым ответили.
        buttons = self.api.sent[-1]["buttons"]["inline_keyboard"][0]
        labels = [item["text"] for item in buttons]
        self.assertIn("DOCX", labels)
        self.assertNotIn("Текстом", labels)

    def test_long_text_comes_as_document(self) -> None:
        self.bot.handle_message(voice_message())
        self.complete("j1", "фраза. " * 2000)
        self.bot.check_pending()

        self.assertEqual(self.whisper.exports, [("j1", "docx")])
        self.assertEqual(len(self.api.documents), 1)
        self.assertTrue(self.api.documents[0]["existed"])
        # Файл выгрузки после отправки не копится на диске.
        self.assertFalse(list((bot_module.WORK_DIR / "outgoing").glob("*")))

    def test_failed_job_reports_reason(self) -> None:
        self.bot.handle_message(voice_message())
        self.whisper.jobs_by_id["j1"].update(status="failed", error="нет звука")
        self.bot.check_pending()
        self.assertIn("нет звука", self.api.last_edit)
        self.assertEqual(self.bot.pending, [])

    def test_button_sends_other_format(self) -> None:
        self.bot.handle_message(voice_message())
        self.complete("j1", "Короткая расшифровка.")
        self.bot.check_pending()

        self.bot.handle_callback(
            {
                "id": "cb",
                "from": {"id": 7},
                "message": {"message_id": 1, "chat": {"id": 42}},
                "data": "fmt:srt:j1",
            }
        )
        self.assertEqual(self.whisper.exports, [("j1", "srt")])
        self.assertEqual(len(self.api.documents), 1)

    def test_undeliverable_result_drops_the_task(self) -> None:
        """Отказ Телеграма на отправке не должен крутиться вечно."""
        self.bot.handle_message(voice_message())
        self.complete("j1", "фраза. " * 2000)

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise BotApiError("sendDocument", 413, "Request Entity Too Large")

        self.api.send_document = refuse  # type: ignore[assignment]
        self.bot.check_pending()

        self.assertEqual(self.bot.pending, [])
        self.assertIn("не отправился", self.api.texts[-1])


class LinkTests(BotTestCase):
    def test_link_starts_download_then_transcription(self) -> None:
        self.bot.handle_message(text_message("https://vk.com/video-1"))
        self.assertIn("Вебинар", self.api.edits[0]["text"])
        self.assertEqual(self.bot.pending[0]["kind"], "download")

        # Сервис докачал файл и сам поставил его в очередь.
        project_id = self.bot.pending[0]["project_id"]
        self.whisper.downloads[project_id] = {"status": "completed", "percent": 100}
        self.whisper.jobs_by_id["j1"] = {
            "id": "j1",
            "status": "processing",
            "progress": 0.5,
            "project_id": project_id,
            "original_name": "video.mp4",
        }
        self.bot.check_pending()

        self.assertEqual(self.bot.pending[0]["kind"], "job")
        self.assertIn("50%", self.api.last_edit)

    def test_code_after_the_link_reaches_the_service(self) -> None:
        seen: dict[str, Any] = {}

        def probe(url: str, passcode: str | None = None) -> dict[str, Any]:
            seen["passcode"] = passcode
            return self.whisper.probe_result

        self.whisper.probe_url = probe  # type: ignore[assignment]
        self.bot.handle_message(text_message("https://zoom.us/rec/share/x AbC12"))

        self.assertEqual(seen["passcode"], "AbC12")

    def test_words_around_the_link_are_not_a_passcode(self) -> None:
        """«вот запись <ссылка> посмотри» — это не код доступа."""
        seen: dict[str, Any] = {}

        def probe(url: str, passcode: str | None = None) -> dict[str, Any]:
            seen["url"] = url
            seen["passcode"] = passcode
            return self.whisper.probe_result

        self.whisper.probe_url = probe  # type: ignore[assignment]
        self.bot.handle_message(
            text_message("вот запись https://vk.com/video-1 посмотри потом")
        )

        self.assertEqual(seen["url"], "https://vk.com/video-1")
        self.assertIsNone(seen["passcode"])

    def test_broken_link_mentions_zoom_passcode(self) -> None:
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise bot_module.WhisperError("Не удалось открыть ссылку")

        self.whisper.probe_url = refuse  # type: ignore[assignment]
        self.bot.handle_message(text_message("https://zoom.us/rec/share/x"))
        self.assertIn("код доступа", self.api.last_edit)


class ResilienceTests(BotTestCase):
    def test_offset_is_confirmed_before_handling(self) -> None:
        """Повтор обновления дороже пропуска: подтверждаем до работы."""
        self.api.updates = [[{"update_id": 500, "message": voice_message()}]]
        self.bot.poll_once(timeout=0)

        self.assertEqual(self.bot.offset, 501)
        restarted = Bot(
            self.config,
            api=self.api,
            whisper=self.whisper,
            state_path=self.bot.state_path,
        )
        self.assertEqual(restarted.offset, 501)
        self.assertEqual(len(restarted.pending), 1)

    def test_service_loss_notifies_once(self) -> None:
        self.bot.handle_message(voice_message())
        self.whisper.offline = True

        self.bot.check_pending()
        self.bot.check_pending()
        self.bot.check_pending()

        mark = "осталось в очереди"
        warnings = [text for text in self.api.texts if mark in text]
        edits = [item for item in self.api.edits if mark in item["text"]]
        self.assertEqual(len(warnings) + len(edits), 1)
        # Задание не выброшено: сервис ещё может вернуться.
        self.assertEqual(len(self.bot.pending), 1)

    def test_pending_survives_restart(self) -> None:
        self.bot.handle_message(voice_message())
        restarted = Bot(
            self.config,
            api=self.api,
            whisper=self.whisper,
            state_path=self.bot.state_path,
        )
        self.complete("j1", "Готово.")
        restarted.check_pending()
        self.assertIn("Готово.", self.api.texts)


class ConfigTests(unittest.TestCase):
    """Настройки: запись токена без редактора.

    Правка файла руками один раз уже не сработала — Блокнот не сохранил файл в
    служебную папку и промолчал, а бот видел пустой токен. Поэтому запись
    делает сам бот, и она проверяется чтением с диска.
    """

    def setUp(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="whisper-telegram-config-"))
        self.path = self._root / "telegram.json"

    def tearDown(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)

    def test_missing_file_is_created_as_a_template(self) -> None:
        config = load(self.path, require_token=False)

        self.assertTrue(self.path.exists())
        self.assertEqual(config.token, "")
        # Код привязки готов сразу: придумывать его человеку не нужно.
        self.assertTrue(config.setup_code)

    def test_empty_token_points_at_the_command(self) -> None:
        load(self.path, require_token=False)

        with self.assertRaises(ConfigError) as caught:
            load(self.path)

        self.assertIn("--set-token", str(caught.exception))

    def test_written_token_is_read_back(self) -> None:
        config = load(self.path, require_token=False)
        config.token = "123456789:AAHexampleexampleexampleexample12345"
        config.save()

        again = load(self.path)

        self.assertEqual(again.token, config.token)
        self.assertEqual(again.setup_code, config.setup_code)

    def test_real_token_shape_is_accepted(self) -> None:
        self.assertTrue(
            looks_like_token("123456789:AAHexampleexampleexampleexample12345")
        )

    def test_common_mistakes_are_rejected(self) -> None:
        for wrong in (
            "мой-бот",
            "@my_whisper_bot",
            "https://t.me/my_whisper_bot",
            "",
            "123456789",
            "123456789:short",
        ):
            with self.subTest(wrong=wrong):
                self.assertFalse(looks_like_token(wrong))


class TokenLeakTests(unittest.TestCase):
    """Токен не должен попадать в текст ошибок, а через них — в лог.

    В Bot API токен лежит прямо в адресе, и requests подставляет полный URL в
    сообщение о сетевой ошибке. Лог показывают и пересылают куда охотнее, чем
    файл настроек, поэтому обрыв связи не должен стоить бота.
    """

    def setUp(self) -> None:
        self.token = "123456789:AAHexampleexampleexampleexample12345"
        self.api = BotApi(self.token)

    def test_network_error_hides_the_token(self) -> None:
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise requests.ConnectionError(
                f"Max retries exceeded with url: "
                f"/bot{self.token}/getUpdates (timed out)"
            )

        self.api.session.post = refuse  # type: ignore[assignment]

        with self.assertRaises(BotNetworkError) as caught:
            self.api.call("getUpdates")

        message = str(caught.exception)
        self.assertNotIn(self.token, message)
        self.assertIn("***", message)

    def test_download_error_hides_the_token(self) -> None:
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise requests.ConnectionError(f"/file/bot{self.token}/voice/x.oga")

        self.api.session.get = refuse  # type: ignore[assignment]

        with self.assertRaises(BotNetworkError) as caught:
            self.api.download("voice/x.oga", Path("nowhere/x.oga"))

        self.assertNotIn(self.token, str(caught.exception))


class SplitTests(unittest.TestCase):
    def test_splits_by_paragraph(self) -> None:
        text = ("а" * 100 + "\n\n") * 60
        parts = split_text(text, limit=1000)
        self.assertTrue(all(len(part) <= 1000 for part in parts))
        self.assertTrue(all(not part.endswith("а" * 101) for part in parts))

    def test_keeps_words_whole(self) -> None:
        parts = split_text("слово " * 2000, limit=500)
        self.assertTrue(all(len(part) <= 500 for part in parts))
        for part in parts:
            self.assertFalse(part.endswith("слов"))

    def test_short_text_is_not_split(self) -> None:
        self.assertEqual(split_text("привет"), ["привет"])
        self.assertEqual(split_text("   "), [])


class AutostartSettingTests(unittest.TestCase):
    """Выключатель автозапуска бота.

    Настройка живёт в сервисе, а не в файле бота, потому что её меняют из
    интерфейса. Токен при этом остаётся на месте: выключить эксперимент и
    отозвать бота у @BotFather — разные вещи.
    """

    def test_enabled_by_default(self) -> None:
        self.assertEqual(storage.DEFAULT_SETTINGS["telegram_autostart"], "on")

    def test_only_two_values_allowed(self) -> None:
        self.assertEqual(
            storage.SETTING_VALUES["telegram_autostart"], {"on", "off"}
        )

    def test_api_knows_the_field(self) -> None:
        """Без поля в модели PATCH принял бы настройку и молча её потерял."""
        from backend.app import SettingsPatch

        self.assertIn("telegram_autostart", SettingsPatch.model_fields)


if __name__ == "__main__":
    unittest.main()
