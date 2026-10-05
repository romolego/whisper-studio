"""Что бот делает с тем, что ему прислали.

Три сценария и один общий хвост. Голосовое или файл — скачать и поставить в
очередь; ссылка — отдать её скачивальщику сервиса; дальше в обоих случаях одно
и то же: следить за заданием, править статус в том же сообщении и прислать
результат.

Цикл однопоточный. Пока идёт скачивание файла из Телеграма, новые сообщения не
разбираются — для одного пользователя это незаметно, зато не нужно ни блокировок
вокруг состояния, ни защиты от двух одновременных ответов в один чат.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import botapi
from .botapi import BotApi, BotApiError, BotNetworkError, keyboard
from .config import Config
from .whisper import WhisperApi, WhisperError, WhisperUnavailable


logger = logging.getLogger("telegram_bot")

ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = ROOT / "data" / "telegram"
STATE_PATH = WORK_DIR / "state.json"

URL_PATTERN = re.compile(r"https?://\S+")

# Форматы, которые можно попросить кнопкой под готовым ответом.
FORMAT_TITLES = {
    "text": "Текстом",
    "docx": "DOCX",
    "txt": "TXT",
    "srt": "SRT",
    "vtt": "VTT",
}

# Расширение по типу вложения: имени файла у голосового и кружочка нет.
MIME_SUFFIXES = {
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-wav": ".wav",
    "audio/wav": ".wav",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
}

# То, что заведомо не звук и не видео. Проверка нужна до скачивания: иначе
# человек ждёт загрузку ради ответа «FFmpeg не открыл этот файл».
REJECTED_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf",
    ".zip", ".rar", ".7z", ".exe", ".msi", ".apk", ".iso",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".svg",
}

HELP = (
    "Я расшифровываю голосовые, аудио и видео на домашнем компьютере.\n\n"
    "Пришлите:\n"
    "• голосовое сообщение или кружочек;\n"
    "• аудио- или видеофайл;\n"
    "• ссылку на YouTube, VK, RuTube, Яндекс.Диск, Google Drive или Zoom "
    "(для Zoom — ссылка и код доступа через пробел).\n\n"
    "Когда расшифровка готова, под ответом будут кнопки: тот же текст можно "
    "получить документом или с таймкодами.\n\n"
    "/status — что сейчас с сервисом и очередью."
)


class Bot:
    def __init__(
        self,
        config: Config,
        api: BotApi | None = None,
        whisper: WhisperApi | None = None,
        state_path: Path = STATE_PATH,
    ) -> None:
        self.config = config
        self.api = api or BotApi(config.token, config.api_base, config.proxy)
        self.whisper = whisper or WhisperApi(config.whisper_api)
        self.state_path = state_path
        self.offset = 0
        self.pending: list[dict[str, Any]] = []
        self.stopped = False
        self._load_state()

    # --- состояние между запусками ---------------------------------------

    def _load_state(self) -> None:
        """Поднять смещение и незаконченные задания.

        Смещение хранится на диске, потому что Телеграм отдаёт обновление
        повторно, пока его не подтвердили. Без этого перезапуск бота посреди
        работы означал бы вторую расшифровку того же голосового.
        """
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Состояние не прочиталось, начинаем с чистого")
            return
        self.offset = int(raw.get("offset") or 0)
        self.pending = [item for item in raw.get("pending") or [] if isinstance(item, dict)]

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"offset": self.offset, "pending": self.pending}
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    # --- цикл -------------------------------------------------------------

    def run_forever(self) -> None:
        """Длинный опрос с паузой на ошибках сети.

        Пауза растёт до минуты: когда отвалился VPN, стучаться каждую секунду
        бессмысленно, а когда он вернулся — минута ожидания незаметна.
        """
        pause = 1
        while not self.stopped:
            try:
                self.poll_once()
                pause = 1
            except BotNetworkError as exc:
                logger.warning("Телеграм недоступен: %s", exc)
                time.sleep(pause)
                pause = min(pause * 2, 60)
            except BotApiError as exc:
                # Неверный токен или отозванный доступ не лечатся повтором.
                if exc.code in {401, 404}:
                    logger.error("Телеграм отказал: %s", exc)
                    raise
                logger.warning("Телеграм отказал: %s", exc)
                time.sleep(pause)
                pause = min(pause * 2, 60)

    def poll_once(self, timeout: int | None = None) -> int:
        """Один заход: забрать обновления, разобрать, проверить очередь.

        Пока есть незаконченные задания, опрос короче: статус в чате должен
        обновляться примерно раз в пять секунд, а не раз в полминуты.
        """
        if timeout is None:
            timeout = 5 if self.pending else 25
        updates = self.api.get_updates(self.offset, timeout=timeout)
        for update in updates:
            self.offset = int(update.get("update_id", 0)) + 1
            # Смещение подтверждается до обработки, а не после. Телеграм отдаёт
            # неподтверждённое обновление снова, и падение посреди работы
            # означало бы вторую расшифровку того же файла — а это занятая
            # видеокарта и второй ответ в чат. Потерять одно сообщение при
            # аварии дешевле: его видно, и его можно прислать заново.
            self._save_state()
            try:
                self.handle_update(update)
            except (BotApiError, BotNetworkError):
                raise
            except Exception:
                logger.exception("Обновление не обработалось")
        self.check_pending()
        return len(updates)

    # --- разбор обновлений -------------------------------------------------

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self.handle_message(update["message"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        user_id = int((message.get("from") or {}).get("id") or 0)
        if not chat_id:
            return
        text = str(message.get("text") or "").strip()

        if not self.config.allows(user_id):
            self._greet_stranger(chat_id, user_id, text)
            return

        if text.startswith("/"):
            self._handle_command(chat_id, text)
            return

        attachment = _attachment(message)
        if attachment:
            self._accept_file(message, chat_id, attachment)
            return

        if URL_PATTERN.search(text):
            self._accept_link(message, chat_id, text)
            return

        self.api.send_message(chat_id, HELP)

    def _greet_stranger(self, chat_id: int, user_id: int, text: str) -> None:
        """Чужих не обслуживаем: имя бота публично, а видеокарта одна.

        Привязка по коду из файла настроек, а не по «первый написавший — хозяин»:
        второй вариант отдаёт бота тому, кто успел раньше.
        """
        code = self.config.setup_code
        if code and text.strip() == code:
            self.config.allow(user_id)
            logger.info("Привязан пользователь %s", user_id)
            self.api.send_message(
                chat_id,
                "Готово, теперь я работаю с вами.\n\n" + HELP,
            )
            return
        self.api.send_message(
            chat_id,
            "Это личный бот. Чтобы им пользоваться, пришлите код привязки "
            "из файла data/telegram.json на компьютере с сервисом.",
        )

    def _handle_command(self, chat_id: int, text: str) -> None:
        command = text.split()[0].split("@")[0].lower()
        if command == "/status":
            self.api.send_message(chat_id, self._service_status())
        elif command in {"/start", "/help"}:
            self.api.send_message(chat_id, HELP)
        else:
            self.api.send_message(chat_id, "Такой команды нет.\n\n" + HELP)

    def _service_status(self) -> str:
        try:
            health = self.whisper.health()
        except (WhisperUnavailable, WhisperError):
            return (
                "Сервис распознавания не отвечает. Скорее всего, он выключен — "
                "запустите «Запустить Whisper» на компьютере."
            )
        queued = int(health.get("queued") or 0)
        working = "работает" if health.get("worker_alive") else "не запущен"
        waiting = "очередь пуста" if not queued else f"в очереди: {queued}"
        mine = len(self.pending)
        line = f"Сервис {working}, {waiting}."
        if mine:
            line += f"\nВаших заданий в работе: {mine}."
        return line

    # --- сценарий: файл ----------------------------------------------------

    def _accept_file(
        self,
        message: dict[str, Any],
        chat_id: int,
        attachment: dict[str, Any],
    ) -> None:
        message_id = int(message.get("message_id") or 0)
        size = int(attachment.get("file_size") or 0)
        limit = self.config.download_limit
        title = attachment["title"]

        suffix = _suffix(attachment)
        if suffix in REJECTED_SUFFIXES:
            self.api.send_message(
                chat_id,
                f"«{title}» — это не звук и не видео, распознавать нечего.",
                reply_to=message_id,
            )
            return

        if limit and size > limit:
            self.api.send_message(
                chat_id,
                f"«{title}» весит {_megabytes(size)}, а Телеграм отдаёт боту "
                f"файлы только до {_megabytes(limit)}. "
                "Пришлите ссылку на файл — по ней ограничения нет.",
                reply_to=message_id,
            )
            return

        status = self.api.send_message(
            chat_id,
            f"Принял «{title}». Забираю из Телеграма…",
            reply_to=message_id,
        )
        status_id = int(status.get("message_id") or 0)

        try:
            info = self.api.get_file(attachment["file_id"])
        except BotApiError as exc:
            self.api.edit_message_text(
                chat_id, status_id, f"Телеграм не отдал файл: {exc.description}"
            )
            return

        remote = str(info.get("file_path") or "")
        if not suffix:
            suffix = Path(remote).suffix.lower() or ".bin"
        local = WORK_DIR / "incoming" / f"{attachment['file_unique_id']}{suffix}"
        source = self.api.download(remote, local)
        # Свой сервер Bot API отдаёт файл на месте, а не копией: удалять после
        # постановки в очередь можно только то, что скачали сами.
        owned = source == local

        try:
            project = self._project_for_day(chat_id)
            job = self.whisper.enqueue_file(project["id"], source, self._options())
        except WhisperUnavailable:
            self.api.edit_message_text(chat_id, status_id, _OFFLINE)
            return
        except WhisperError as exc:
            self.api.edit_message_text(chat_id, status_id, f"Сервис отказал: {exc}")
            return
        finally:
            if owned:
                source.unlink(missing_ok=True)

        self._track(
            {
                "kind": "job",
                "chat_id": chat_id,
                "status_id": status_id,
                "reply_to": message_id,
                "job_id": job["id"],
                "title": title,
                "status_text": "",
            }
        )

    def _project_for_day(self, chat_id: int) -> dict[str, Any]:
        """Один проект на день, а не на каждое голосовое.

        Десять сообщений за день — это десять файлов в одном проекте с общим
        результатом, а не десять проектов в ленте. Ссылки живут отдельно: у
        каждой своя загрузка, а состояние загрузки в сервисе одно на проект.
        """
        today = datetime.now()
        return self.whisper.project(
            f"chat{chat_id}:day:{today:%Y-%m-%d}",
            f"Телеграм · {today:%d.%m.%Y}",
        )

    # --- сценарий: ссылка --------------------------------------------------

    def _accept_link(self, message: dict[str, Any], chat_id: int, text: str) -> None:
        message_id = int(message.get("message_id") or 0)
        match = URL_PATTERN.search(text)
        url = match.group(0) if match else ""
        # Код доступа Zoom приходит следом за ссылкой: в самой ссылке его нет, а
        # спрашивать отдельным сообщением — лишний круг переписки. Берём слово
        # после ссылки и только если оно там одно: «вот запись <ссылка>» — это
        # не код, и принять его за код значит сорвать проверку.
        tail = text[match.end():].split() if match else []
        passcode = tail[0] if len(tail) == 1 else None

        status = self.api.send_message(
            chat_id, "Проверяю ссылку…", reply_to=message_id
        )
        status_id = int(status.get("message_id") or 0)

        try:
            metadata = self.whisper.probe_url(url, passcode)
        except WhisperUnavailable:
            self.api.edit_message_text(chat_id, status_id, _OFFLINE)
            return
        except WhisperError as exc:
            self.api.edit_message_text(
                chat_id,
                status_id,
                f"Ссылка не открылась: {exc}\n\n"
                "Если это запись Zoom, пришлите ссылку и код доступа через пробел.",
            )
            return

        title = str(metadata.get("title") or "Видео по ссылке")
        duration = _duration(metadata.get("duration_sec"))
        self.api.edit_message_text(
            chat_id, status_id, f"«{title}»{duration}\nСкачиваю…"
        )

        try:
            project = self.whisper.project(
                f"chat{chat_id}:link:{message_id}", title
            )
            self.whisper.start_url_download(
                url,
                project["id"],
                self.config.download_quality,
                self._options(),
                passcode,
            )
        except WhisperUnavailable:
            self.api.edit_message_text(chat_id, status_id, _OFFLINE)
            return
        except WhisperError as exc:
            self.api.edit_message_text(chat_id, status_id, f"Сервис отказал: {exc}")
            return

        self._track(
            {
                "kind": "download",
                "chat_id": chat_id,
                "status_id": status_id,
                "reply_to": message_id,
                "project_id": project["id"],
                "title": title,
                "status_text": "",
            }
        )

    # --- слежение за работой ------------------------------------------------

    def _track(self, entry: dict[str, Any]) -> None:
        self.pending.append(entry)
        self._save_state()
        self.check_pending()

    def check_pending(self) -> None:
        if not self.pending:
            return
        alive: list[dict[str, Any]] = []
        for entry in self.pending:
            try:
                keep = (
                    self._check_download(entry)
                    if entry.get("kind") == "download"
                    else self._check_job(entry)
                )
            except WhisperUnavailable:
                # Сервис могли перезапустить: молча ждём его возвращения,
                # но один раз предупреждаем, чтобы не выглядело зависанием.
                if not entry.get("offline_notified"):
                    entry["offline_notified"] = True
                    self._say(entry, _OFFLINE_WAIT)
                keep = True
            except WhisperError as exc:
                self._say(entry, f"Сервис отказал: {exc}")
                keep = False
            except BotApiError as exc:
                # Отказ на конкретном сообщении — например, готовый файл
                # больше, чем боту разрешено отправлять. Повторять его
                # бессмысленно: задание снимается, причина уходит в чат.
                logger.warning("Ответ не ушёл: %s", exc)
                try:
                    self.api.send_message(
                        int(entry["chat_id"]),
                        f"Результат «{entry.get('title')}» не отправился: "
                        f"{exc.description}",
                    )
                except (BotApiError, BotNetworkError):
                    pass
                keep = False
            if keep:
                alive.append(entry)
        self.pending = alive
        self._save_state()

    def _check_download(self, entry: dict[str, Any]) -> bool:
        state = self.whisper.url_download_status(entry["project_id"])
        status = str(state.get("status") or "")

        if status in {"completed", "idle"}:
            # Сервис сам ставит скачанный файл в очередь: задание появляется
            # в том же проекте, и дальше это обычная расшифровка.
            jobs = self.whisper.jobs(entry["project_id"])
            if jobs:
                entry["kind"] = "job"
                entry["job_id"] = jobs[0]["id"]
                entry["status_text"] = ""
                return self._check_job(entry)
            if status == "idle":
                self._say(entry, "Загрузка потерялась. Пришлите ссылку ещё раз.")
                return False
            return True

        if status in {"failed", "error"}:
            reason = str(state.get("error") or "причина не указана")
            self._say(entry, f"Скачать не удалось: {reason}")
            return False
        if status == "cancelled":
            self._say(entry, "Загрузка остановлена.")
            return False

        percent = state.get("percent")
        shown = f" {int(percent)}%" if isinstance(percent, (int, float)) else ""
        self._status(entry, f"Скачиваю «{entry['title']}»{shown}")
        return True

    def _check_job(self, entry: dict[str, Any]) -> bool:
        job = self.whisper.job(entry["job_id"])
        status = str(job.get("status") or "")

        if status == "completed":
            self._deliver(entry, job)
            return False
        if status == "failed":
            reason = str(job.get("error") or "причина не указана")
            self._say(entry, f"Распознать не удалось: {reason}")
            return False
        if status == "cancelled":
            self._say(entry, "Распознавание остановлено.")
            return False

        if status == "processing":
            done = float(job.get("progress") or 0) * 100
            self._status(entry, f"Распознаю «{entry['title']}» — {done:.0f}%")
        else:
            position = job.get("queue_position")
            place = f" (место {position})" if position else ""
            self._status(entry, f"«{entry['title']}» в очереди{place}")
        return True

    def _status(self, entry: dict[str, Any], text: str) -> None:
        """Правим то же сообщение, если текст изменился.

        Своего статусного сообщения может и не быть — например, когда формат
        переспрашивают кнопкой под уже готовым ответом. Переписывать в этом
        случае нечего, и трогать чужое сообщение нельзя.
        """
        if entry.get("status_text") == text:
            return
        entry["status_text"] = text
        if not entry.get("status_id"):
            return
        try:
            self.api.edit_message_text(entry["chat_id"], entry["status_id"], text)
        except BotApiError as exc:
            logger.warning("Статус не обновился: %s", exc)

    def _say(self, entry: dict[str, Any], text: str) -> None:
        self._status(entry, text)

    # --- выдача результата --------------------------------------------------

    def _deliver(
        self,
        entry: dict[str, Any],
        job: dict[str, Any],
        fmt: str | None = None,
    ) -> None:
        chat_id = int(entry["chat_id"])
        job_id = str(job["id"])
        choice = fmt or self.config.reply_format

        text = ""
        if choice in {"auto", "text"}:
            text = self.whisper.text(job_id)

        # Расшифровка почти всегда длиннее сообщения: в режиме «auto» короткая
        # приходит текстом, чтобы её можно было сразу скопировать, длинная —
        # документом, иначе чат превращается в десяток простыней.
        if choice == "auto":
            choice = "text" if len(text) <= botapi.MESSAGE_LIMIT else "docx"

        if choice == "text":
            if not text.strip():
                self._status(entry, "Распознавание пустое: речи не нашлось.")
                return
            self._status(entry, f"Готово: «{entry['title']}»")
            self.api.send_long_text(
                chat_id,
                text,
                reply_to=entry.get("reply_to"),
                buttons=_other_formats(job_id, "text"),
            )
            return

        export = WORK_DIR / "outgoing" / f"{job_id}.{choice}"
        self.whisper.export(job_id, choice, export)
        try:
            self._status(entry, f"Готово: «{entry['title']}»")
            self.api.send_document(
                chat_id,
                export,
                caption=entry["title"],
                reply_to=entry.get("reply_to"),
                buttons=_other_formats(job_id, choice),
            )
        finally:
            export.unlink(missing_ok=True)

    def handle_callback(self, query: dict[str, Any]) -> None:
        """Кнопка «тот же текст другим форматом» под готовым ответом."""
        callback_id = str(query.get("id") or "")
        user_id = int((query.get("from") or {}).get("id") or 0)
        message = query.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        data = str(query.get("data") or "")

        if not self.config.allows(user_id):
            self.api.answer_callback(callback_id, "Это личный бот")
            return
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "fmt":
            self.api.answer_callback(callback_id)
            return
        _, choice, job_id = parts
        if choice not in FORMAT_TITLES:
            self.api.answer_callback(callback_id)
            return

        self.api.answer_callback(callback_id, FORMAT_TITLES[choice])
        try:
            job = self.whisper.job(job_id)
        except WhisperUnavailable:
            self.api.send_message(chat_id, _OFFLINE)
            return
        except WhisperError as exc:
            self.api.send_message(chat_id, f"Сервис отказал: {exc}")
            return

        entry = {
            "chat_id": chat_id,
            # Своего статусного сообщения у этой выдачи нет: под кнопкой лежит
            # прошлый ответ, и переписывать его нельзя.
            "status_id": 0,
            "reply_to": None,
            "title": str(job.get("original_name") or "расшифровка"),
            "status_text": None,
        }
        try:
            self._deliver(entry, job, fmt=choice)
        except WhisperUnavailable:
            self.api.send_message(chat_id, _OFFLINE)
        except WhisperError as exc:
            self.api.send_message(chat_id, f"Не получилось: {exc}")

    # --- настройки распознавания -------------------------------------------

    def _options(self) -> dict[str, Any]:
        return {
            "language": self.config.language,
            "quality": self.config.quality,
            "timestamps": self.config.timestamps,
            "formatting": self.config.formatting,
            "diarization": self.config.diarization,
        }


# Два разных случая, и путать их нельзя. При приёме сервис недоступен — значит
# работа не начата и файл надо прислать заново. В ожидании результата — значит
# задание уже в очереди и переживёт перезапуск сервиса само.
_OFFLINE = (
    "Сервис распознавания не отвечает — похоже, он выключен. "
    "Пришлите файл снова, когда запустите Whisper на компьютере."
)
_OFFLINE_WAIT = (
    "Сервис распознавания перестал отвечать. Задание осталось в очереди: "
    "как только он вернётся, пришлю результат сюда же."
)


def _other_formats(job_id: str, sent: str) -> dict[str, Any]:
    """Кнопки с остальными форматами того же результата.

    Выбор после ответа, а не до: пока расшифровки нет, выбирать не из чего,
    а получив её, обычно и становится понятно, нужен ли файл и таймкоды.
    """
    buttons = [
        (title, f"fmt:{key}:{job_id}")
        for key, title in FORMAT_TITLES.items()
        if key != sent and key != "vtt"
    ]
    return keyboard(buttons)


def _attachment(message: dict[str, Any]) -> dict[str, Any] | None:
    """Вытащить вложение и придумать ему человеческое имя."""
    stamp = datetime.now().strftime("%d.%m.%Y %H-%M")
    for key, label in (
        ("voice", f"Голосовое {stamp}"),
        ("video_note", f"Кружочек {stamp}"),
        ("audio", f"Аудио {stamp}"),
        ("video", f"Видео {stamp}"),
        ("document", f"Файл {stamp}"),
    ):
        payload = message.get(key)
        if not isinstance(payload, dict):
            continue
        name = str(payload.get("file_name") or "").strip()
        return {
            "file_id": payload.get("file_id"),
            "file_unique_id": payload.get("file_unique_id") or payload.get("file_id"),
            "file_size": payload.get("file_size") or 0,
            "mime_type": str(payload.get("mime_type") or ""),
            "file_name": name,
            "title": name or label,
            "kind": key,
        }
    return None


def _suffix(attachment: dict[str, Any]) -> str:
    name = attachment.get("file_name") or ""
    if name and Path(name).suffix:
        return Path(name).suffix.lower()
    mime = attachment.get("mime_type") or ""
    if mime in MIME_SUFFIXES:
        return MIME_SUFFIXES[mime]
    if attachment.get("kind") == "voice":
        return ".ogg"
    if attachment.get("kind") == "video_note":
        return ".mp4"
    return ""


def _megabytes(size: int) -> str:
    return f"{size / 1024 / 1024:.0f} МБ"


def _duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f", {hours} ч {minutes} мин"
    if minutes:
        return f", {minutes} мин"
    return f", {secs} с"
