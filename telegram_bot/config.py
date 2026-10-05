"""Настройки бота: один файл data/telegram.json, который правит человек.

Почему не общие настройки сервиса. В `/api/settings` лежат значения, которые
отдаются браузеру, а здесь токен бота — то есть полный доступ к переписке. Он
не должен уезжать на страницу даже на локальном компьютере, поэтому живёт
отдельным файлом в `data/`, который целиком в .gitignore.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "telegram.json"

# Облачный Bot API не отдаёт боту файлы больше 20 МБ — это его ограничение, а
# не наше. Свой сервер Bot API снимает лимит совсем, поэтому предел считается
# из адреса: чужой адрес значит локальный сервер и предел не применяется.
CLOUD_API = "https://api.telegram.org"
CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024

LANGUAGES = {"ru", "en", "auto"}
QUALITIES = {"fast", "accurate"}
TIMESTAMPS = {"none", "segments", "words"}
FORMATTINGS = {"formatted", "plain"}
# Форматы ответа. `auto` — короткое текстом, длинное документом: расшифровка
# получаса разговора не влезает в сообщение, а короткую диктовку неудобно
# получать файлом.
REPLY_FORMATS = {"auto", "text", "docx", "txt", "srt", "vtt"}


# Токен от @BotFather: номер бота, двоеточие и длинный хвост. Проверка нужна
# не ради строгости, а чтобы поймать частую ошибку — в поле попадает имя бота,
# ссылка на него или строка вместе с кавычками из инструкции.
TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class ConfigError(RuntimeError):
    """Настройки не заполнены или заполнены неверно."""


def looks_like_token(value: str) -> bool:
    return bool(TOKEN_PATTERN.match(value.strip()))


@dataclass
class Config:
    token: str = ""
    setup_code: str = ""
    allowed_users: list[int] = field(default_factory=list)
    proxy: str = ""
    api_base: str = CLOUD_API
    whisper_api: str = "http://127.0.0.1:8765/api"
    language: str = "ru"
    quality: str = "accurate"
    timestamps: str = "segments"
    formatting: str = "formatted"
    diarization: bool = False
    reply_format: str = "auto"
    download_quality: str = "audio"
    path: Path = CONFIG_PATH

    @property
    def download_limit(self) -> int:
        """Сколько байт бот способен забрать. 0 — без ограничения."""
        if self.api_base.rstrip("/") == CLOUD_API:
            return CLOUD_DOWNLOAD_LIMIT
        return 0

    def allows(self, user_id: int) -> bool:
        return int(user_id) in self.allowed_users

    def allow(self, user_id: int) -> None:
        if int(user_id) not in self.allowed_users:
            self.allowed_users.append(int(user_id))
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": self.token,
            "setup_code": self.setup_code,
            "allowed_users": self.allowed_users,
            "proxy": self.proxy,
            "api_base": self.api_base,
            "whisper_api": self.whisper_api,
            "language": self.language,
            "quality": self.quality,
            "timestamps": self.timestamps,
            "formatting": self.formatting,
            "diarization": self.diarization,
            "reply_format": self.reply_format,
            "download_quality": self.download_quality,
        }
        # Пишем через временный файл: обрыв на записи не должен оставить
        # пользователя с обрезанным файлом и потерянным токеном.
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _choice(raw: dict[str, Any], key: str, allowed: set[str], fallback: str) -> str:
    value = str(raw.get(key) or fallback).strip()
    if value not in allowed:
        raise ConfigError(
            f"В {CONFIG_PATH.name} у «{key}» значение «{value}», "
            f"а допустимы: {', '.join(sorted(allowed))}."
        )
    return value


def load(path: Path = CONFIG_PATH, require_token: bool = True) -> Config:
    """Прочитать настройки. Файла нет — создать заготовку и объяснить, что дальше.

    Заготовка создаётся с готовым кодом привязки, чтобы человеку осталось
    вписать один токен: код придумывать самому не нужно, а случайный код лучше
    придуманного — по нему к боту не подберётся посторонний.

    `require_token=False` нужен тому, кто как раз собирается токен записать:
    ему настройки нужны любыми, в том числе пустыми.
    """
    if not path.exists():
        blank = Config(setup_code=secrets.token_hex(4), path=path)
        blank.save()
        if require_token:
            raise ConfigError(
                f"Создан файл настроек {path}. Впишите в него token от "
                f"@BotFather командой:\n"
                f"    .venv\\Scripts\\python.exe -m telegram_bot --set-token"
            )
        return blank

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} испорчен: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} должен содержать объект настроек")

    token = str(raw.get("token") or "").strip()
    if require_token and not token:
        raise ConfigError(
            f"В {path} не заполнен token. Получите его у @BotFather в Телеграме "
            f"и запишите командой:\n"
            f"    .venv\\Scripts\\python.exe -m telegram_bot --set-token"
        )

    users: list[int] = []
    for item in raw.get("allowed_users") or []:
        try:
            users.append(int(item))
        except (TypeError, ValueError):
            raise ConfigError(
                f"В {path} у allowed_users попался не числовой id: {item!r}"
            ) from None

    config = Config(
        token=token,
        setup_code=str(raw.get("setup_code") or "").strip() or secrets.token_hex(4),
        allowed_users=users,
        proxy=str(raw.get("proxy") or "").strip(),
        api_base=str(raw.get("api_base") or CLOUD_API).strip().rstrip("/"),
        whisper_api=(
            str(raw.get("whisper_api") or "http://127.0.0.1:8765/api")
            .strip()
            .rstrip("/")
        ),
        language=_choice(raw, "language", LANGUAGES, "ru"),
        quality=_choice(raw, "quality", QUALITIES, "accurate"),
        timestamps=_choice(raw, "timestamps", TIMESTAMPS, "segments"),
        formatting=_choice(raw, "formatting", FORMATTINGS, "formatted"),
        diarization=bool(raw.get("diarization", False)),
        reply_format=_choice(raw, "reply_format", REPLY_FORMATS, "auto"),
        download_quality=str(raw.get("download_quality") or "audio").strip(),
        path=path,
    )
    # Код привязки мог не сохраниться, если файл писали руками. Дописываем
    # молча: без него первый запуск бота упирается в тупик.
    if str(raw.get("setup_code") or "").strip() != config.setup_code:
        config.save()
    return config
