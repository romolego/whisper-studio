"""Тонкий клиент Bot API поверх requests.

Почему свой, а не готовая библиотека. Нужны ровно семь методов, зато нужны
две вещи, из-за которых готовые обёртки пришлось бы всё равно настраивать:
прокси на один процесс (Телеграм в России без него не открывается) и подмена
адреса API — на неё завязана и разработка без интернета, и переезд на свой
сервер Bot API, если упрёмся в лимит на скачивание.

Работа с сетью здесь синхронная и однопоточная: пользователь у бота один,
а один поток — это отсутствие гонок за смещение обновлений, которое дороже
любой параллельности.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import requests


# Предел длины сообщения в Телеграме. Расшифровка почти всегда длиннее,
# поэтому текст режется на части, а не обрезается.
MESSAGE_LIMIT = 4096
# Предел подписи под документом — свой и втрое короче.
CAPTION_LIMIT = 1024


class BotApiError(RuntimeError):
    """Телеграм ответил отказом: запрос дошёл, но не понравился."""

    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"{method}: {description} (код {code})")
        self.method = method
        self.code = code
        self.description = description


class BotNetworkError(RuntimeError):
    """До Телеграма не достучались: нет сети, упал прокси, отвалился VPN."""


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Разрезать текст по границам абзацев, строк или слов.

    Рвать посреди слова нельзя: расшифровку читают, а не листают. Поэтому
    ищется ближайший разумный шов, и только если его нет на всём куске —
    режется жёстко.
    """
    clean = text.strip()
    if not clean:
        return []
    parts: list[str] = []
    while len(clean) > limit:
        window = clean[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        parts.append(clean[:cut].strip())
        clean = clean[cut:].strip()
    if clean:
        parts.append(clean)
    return parts


def keyboard(buttons: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Ряд кнопок под сообщением: подпись и то, что вернётся в callback_data."""
    row = [{"text": text, "callback_data": data} for text, data in buttons]
    return {"inline_keyboard": [row]}


def _markup(buttons: dict[str, Any] | None) -> str | None:
    if not buttons:
        return None
    return json.dumps(buttons, ensure_ascii=False)


class BotApi:
    def __init__(
        self,
        token: str,
        api_base: str = "https://api.telegram.org",
        proxy: str = "",
    ) -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    # --- транспорт -------------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def _safe(self, text: str) -> str:
        """Убрать токен из текста ошибки.

        В Bot API токен — часть адреса, а requests кладёт в сообщение об ошибке
        полный URL. Без этой замены каждый обрыв связи писал бы токен в
        logs/telegram.log, а лог показывают и пересылают куда охотнее, чем файл
        настроек: одного скриншота хватило бы, чтобы отдать бота чужому.
        """
        if not self.token:
            return text
        return text.replace(self.token, "***")

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        payload = {
            key: value
            for key, value in (params or {}).items()
            if value is not None
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.post(
                    self._url(method),
                    data=payload,
                    files=files,
                    timeout=(10, timeout),
                )
            except requests.exceptions.InvalidSchema as exc:
                # requests умеет socks5 только с PySocks, и его сообщение про
                # missing dependencies человеку ничего не объясняет.
                raise BotNetworkError(
                    "Для прокси socks5 нужен пакет PySocks: "
                    ".venv\\Scripts\\python.exe -m pip install PySocks"
                ) from exc
            except requests.RequestException as exc:
                raise BotNetworkError(self._safe(str(exc))) from exc

            try:
                body = response.json()
            except ValueError as exc:
                raise BotNetworkError(
                    f"{method}: ответ не разобрался ({response.status_code})"
                ) from exc

            if body.get("ok"):
                return body.get("result")

            code = int(body.get("error_code") or response.status_code)
            description = str(body.get("description") or "без объяснения")
            pause = (body.get("parameters") or {}).get("retry_after")
            # Слишком частые запросы — единственная ошибка, которую имеет смысл
            # пережидать самому: Телеграм прямо говорит, сколько ждать.
            if code == 429 and pause and attempt <= 3 and int(pause) <= 60:
                time.sleep(int(pause) + 1)
                continue
            raise BotApiError(method, code, description)

    # --- приём -----------------------------------------------------------

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        """Длинный опрос: соединение исходящее, порты наружу не нужны."""
        result = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=timeout + 15,
        )
        return result or []

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.call("getFile", {"file_id": file_id})

    def download(self, file_path: str, destination: Path) -> Path:
        """Забрать присланный файл.

        Свой сервер Bot API вместо ссылки отдаёт абсолютный путь: файл уже
        лежит на этом же диске, и качать его по HTTP у самого себя незачем.
        """
        source = Path(file_path)
        if source.is_absolute() and source.exists():
            return source

        url = f"{self.api_base}/file/bot{self.token}/{file_path}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.session.get(url, stream=True, timeout=(10, 300)) as response:
                response.raise_for_status()
                with destination.open("wb") as target:
                    for chunk in response.iter_content(1024 * 256):
                        target.write(chunk)
        except requests.RequestException as exc:
            destination.unlink(missing_ok=True)
            raise BotNetworkError(
                self._safe(f"файл не скачался: {exc}")
            ) from exc
        return destination

    # --- ответ -----------------------------------------------------------

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        buttons: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Одно короткое сообщение: статус, ошибка, подсказка.

        Длинный текст сюда не передают — для него send_long_text, который
        режет его на части. Разделение намеренное: у статусного сообщения
        должен быть один message_id, который потом редактируется.
        """
        return self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:MESSAGE_LIMIT],
                "reply_to_message_id": reply_to,
                "reply_markup": _markup(buttons),
            },
        )

    def send_long_text(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        buttons: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Расшифровка текстом: кнопки вешаются на последнюю часть."""
        parts = split_text(text)
        sent: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            sent.append(
                self.send_message(
                    chat_id,
                    part,
                    reply_to=reply_to if index == 0 else None,
                    buttons=buttons if last else None,
                )
            )
        return sent

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: dict[str, Any] | None = None,
    ) -> None:
        """Обновить статус на месте, а не завалить чат новыми сообщениями."""
        try:
            self.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text[:MESSAGE_LIMIT],
                    "reply_markup": _markup(buttons),
                },
            )
        except BotApiError as exc:
            # Совпадение текста — не ошибка: статус не изменился с прошлой
            # проверки, и Телеграм честно об этом сообщает.
            if "not modified" not in exc.description.lower():
                raise

    def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str | None = None,
        reply_to: int | None = None,
        buttons: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with path.open("rb") as handle:
            return self.call(
                "sendDocument",
                {
                    "chat_id": chat_id,
                    "caption": (caption or "")[:CAPTION_LIMIT] or None,
                    "reply_to_message_id": reply_to,
                    "reply_markup": _markup(buttons),
                },
                files={"document": (path.name, handle)},
                timeout=300,
            )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Погасить часики на нажатой кнопке."""
        try:
            self.call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text or None},
            )
        except BotApiError:
            # Кнопку могли нажать давно: запрос протух, но работа сделана.
            pass
