"""Запуск бота: python -m telegram_bot [--check]

`--check` ничего не слушает, а только проверяет три вещи по очереди:
настройки, доступность Телеграма и доступность локального Whisper. Это первое,
что нужно после установки, и единственное, что отвечает на вопрос «почему бот
молчит» без чтения логов.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .bot import Bot
from .botapi import BotApi, BotApiError, BotNetworkError
from .config import ConfigError, load, looks_like_token
from .whisper import WhisperApi


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "telegram.log"


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _network_reason(exc: Exception) -> str:
    """Одна понятная строка вместо стека requests.

    Человек, у которого не поднялся VPN, не должен читать про
    HTTPSConnectionPool и ConnectTimeoutError: ему нужно знать, что именно
    не получилось — не доехали, не разрешилось имя или отказал прокси.
    """
    text = str(exc)
    if "PySocks" in text:
        return text
    if "ProxyError" in text or "SOCKS" in text:
        return "не работает прокси, указанный в настройках"
    if "timed out" in text or "ConnectTimeout" in text:
        return "соединение не устанавливается, ответа нет"
    if "NameResolutionError" in text or "getaddrinfo" in text:
        return "имя api.telegram.org не разрешается"
    if "refused" in text:
        return "соединение отклонено"
    return text[:200]


def setup() -> int:
    """Одна дверь для настройки: спросить токен, если его нет, и проверить связь.

    Нужна из-за `Настроить Телеграм.bat`: у файла, который запускают двойным
    щелчком, не может быть двух разных режимов. Поэтому решает не человек, а
    состояние настроек — токена нет, значит спрашиваем; есть — значит проверяем.
    """
    try:
        config = load(require_token=False)
    except ConfigError as exc:
        print(exc)
        return 1

    if not config.token:
        print("Токен бота ещё не записан.")
        print("Получите его в Телеграме у @BotFather: команда /newbot.")
        print()
        return set_token(None)

    if not looks_like_token(config.token):
        print(f"Токен в {config.path} не похож на настоящий.")
        print("Запишем заново.")
        print()
        return set_token(None)

    return check()


def set_token(value: str | None) -> int:
    """Записать токен за человека, минуя редактор.

    Правка `data/telegram.json` руками оказалась ненадёжной: папку создаёт
    служебный процесс, и обычный Блокнот может не сохранить в неё файл, ничего
    об этом не сказав. Итог — токен «вписан», а в файле пусто. Здесь запись
    делает тот же процесс, который потом будет файл читать, и сразу
    показывает результат.
    """
    if value is None:
        print("Вставьте токен от @BotFather и нажмите Enter.")
        try:
            value = input("Токен: ")
        except (EOFError, KeyboardInterrupt):
            print("\nОтменено.")
            return 1

    token = (value or "").strip().strip('"').strip("'")
    if not token:
        print("Токен пустой, ничего не записал.")
        return 1
    if not looks_like_token(token):
        print(
            "Это не похоже на токен. У @BotFather он выглядит так:\n"
            "    123456789:AAExampleExampleExampleExampleExample\n"
            "Номер бота, двоеточие и длинный хвост — без кавычек и пробелов."
        )
        return 1

    try:
        config = load(require_token=False)
    except ConfigError as exc:
        print(exc)
        return 1

    config.token = token
    config.save()

    written = load(require_token=False).token
    if written != token:
        print(f"Записать не удалось: {config.path} остался прежним.")
        return 1
    print(f"Токен записан в {config.path}")
    print()
    return check()


def check() -> int:
    try:
        config = load()
    except ConfigError as exc:
        print(exc)
        return 1

    print(f"Настройки: {config.path}")
    print(f"Адрес Bot API: {config.api_base}")
    print(f"Прокси: {config.proxy or 'не задан'}")
    limit = config.download_limit
    print(
        "Предел на скачивание: "
        + (f"{limit // 1024 // 1024} МБ" if limit else "без ограничения")
    )
    print(f"Код привязки: {config.setup_code}")
    print(
        "Привязанные пользователи: "
        + (", ".join(str(item) for item in config.allowed_users) or "пока никого")
    )

    api = BotApi(config.token, config.api_base, config.proxy)
    try:
        me = api.call("getMe")
        print(f"Телеграм: бот @{me.get('username')} на связи")
    except BotNetworkError as exc:
        print(f"Телеграм недоступен: {_network_reason(exc)}")
        print("Обычно это значит, что не включён VPN или не указан proxy.")
        return 1
    except BotApiError as exc:
        print(f"Телеграм отказал: {exc.description}")
        print("Скорее всего, неверный token.")
        return 1

    whisper = WhisperApi(config.whisper_api)
    if whisper.alive():
        print("Локальный Whisper отвечает")
    else:
        print(f"Локальный Whisper не отвечает по адресу {config.whisper_api}")
        return 1
    return 0


def main(argv: list[str]) -> int:
    if "--setup" in argv:
        return setup()
    if "--set-token" in argv:
        index = argv.index("--set-token") + 1
        # Токен можно передать сразу, а можно ввести по запросу: в командной
        # строке он остаётся в истории, и не всем это подходит.
        given = argv[index] if index < len(argv) else None
        return set_token(given)
    if "--check" in argv:
        return check()

    _setup_logging()
    logger = logging.getLogger("telegram_bot")
    try:
        config = load()
    except ConfigError as exc:
        # Не настроен — это не авария: сервис запускается и без бота, а
        # человеку нужно прочитать одну строчку, а не разбор стека.
        logger.info("Бот не запущен. %s", exc)
        return 0

    logger.info("Бот запускается, Bot API: %s", config.api_base)
    bot = Bot(config)
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        logger.info("Остановлен вручную")
    except BotApiError as exc:
        logger.error("Телеграм отказал окончательно: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
