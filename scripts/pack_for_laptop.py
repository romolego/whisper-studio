"""Собрать переносимую копию сервиса для другой машины.

Что едет и что не едет — решается здесь, и это главное содержание скрипта.

Не едет ничего, что привязано к железу или восстанавливается установщиком:
среды Python (.venv, .venv_diarization_gpu — в последней torch с CUDA на 4.6 ГБ,
на ноутбуке без видеокарты он бесполезен), node_modules, библиотеки CUDA из
vendor, логи и run-каталог с чужими PID.

Едет то, что иначе пришлось бы качать заново или добывать вручную: сами модели
Whisper на 2.3 ГБ, собранный интерфейс и модель Pyannote из кеша Hugging Face —
последняя избавляет от token и повторного принятия условий Community-1 на новой
машине.

Записи и база по умолчанию остаются дома: на ноутбуке сервис начинает с чистого
листа. Ключ --with-data берёт их с собой.

Отдельно едут пакеты: колёса Python и node_modules. Без них установка на
ноутбуке качает около гигабяйта из сети и собирает интерфейс — минуты ожидания
на медленном рабочем интернете. С ними из сети остаются только четыре программы
через winget: Python, Node.js, FFmpeg и PowerShell 7. Ключ --lean собирает
архив без пакетов, если важнее его размер.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Список того, что копируется, а не того, что исключается. Разница в цене
# ошибки: забытое исключение тащит в архив 4.6 ГБ чужого torch или чужие
# записи, забытое включение — всего лишь заметно при первой же проверке.
DIRECTORIES = (
    "backend",
    "scripts",
    "app",
    "public",
    "tests",
    "dist",
    "models",
)

FILES = (
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "next.config.ts",
    "postcss.config.mjs",
    "vite.config.ts",
    "eslint.config.mjs",
    "requirements.txt",
    "requirements-diarization.txt",
    "README.md",
    "RESTORE.md",
    "INSTALL.md",
    "REMOTE.md",
    "LAPTOP_SETUP.md",
    "AGENTS.md",
    ".gitignore",
    "Установить Whisper.bat",
    "Запустить Whisper.bat",
    "Запустить удалённый GPU.bat",
    "Остановить Whisper.bat",
    "Проверить Whisper.bat",
)

# Тестовые записи нужны проверке установки: она реально распознаёт их на новой
# машине и показывает настоящую скорость.
DATA_FILES = ("test-ru.wav", "test-en.wav")

SKIP_NAMES = {"__pycache__", ".pytest_cache", "node_modules", ".git"}

PYANNOTE_MODEL = "models--pyannote--speaker-diarization-community-1"
NEMOTRON_MODEL = "models--nvidia--Nemotron-3-Diarization"
HUB_CACHE = Path.home() / ".cache" / "huggingface" / "hub"


def log(text: str) -> None:
    print(text, flush=True)


def ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in SKIP_NAMES}


def copy_tree(source: Path, target: Path, filtered: bool = True) -> int:
    """Копирует каталог и возвращает его размер в байтах.

    filtered=False нужен ровно для node_modules: там 27 вложенных node_modules,
    и это не мусор, а честные деревья зависимостей для конфликтующих версий.
    Общий фильтр вырезал бы их и привёз на ноутбук неработающую сборку.
    """
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target,
        ignore=ignore if filtered else None,
        symlinks=False,
    )
    return sum(item.stat().st_size for item in target.rglob("*") if item.is_file())


def human(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} ГБ"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.0f} МБ"
    return f"{size / 1024:.0f} КБ"


SERVICE_WHEELS = "wheels"
DIARIZATION_WHEELS = "wheels-diarization"


def has_wheels(directory: Path) -> bool:
    return directory.is_dir() and any(directory.glob("*.whl"))


def ensure_wheels(refresh: bool = False) -> None:
    """Скачать колёса в bundle/, если их там ещё нет.

    Колёса складываются в проект, а не сразу в сборку: они переживают
    пересборку архива, и второй раз тот же гигабайт не качается.

    Набор годится ровно для Windows x64 и Python 3.11 — той связки, на которой
    он собран. Установщик тянет именно 3.11, поэтому совпадение гарантировано,
    а на случай расхождения он всё равно умеет добрать недостающее из сети.
    """
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        log("   [?] Среда .venv не найдена — колёса не собрать")
        return

    jobs = (
        (
            SERVICE_WHEELS,
            "пакеты сервиса",
            [["-m", "pip", "download", "-d", "{target}", "-r", str(ROOT / "requirements.txt")]],
        ),
        (
            DIARIZATION_WHEELS,
            "torch и Pyannote",
            [
                [
                    "-m", "pip", "download", "-d", "{target}",
                    "--index-url", "https://download.pytorch.org/whl/cpu",
                    "torch==2.11.0", "torchaudio==2.11.0",
                ],
                [
                    "-m", "pip", "download", "-d", "{target}",
                    # Каталог назначения указан и источником: torch из
                    # процессорного индекса уже лежит там, и pyannote возьмёт
                    # именно его.
                    "--find-links", "{target}",
                    # Версии torch повторены здесь не для красоты. Pyannote
                    # просит torch>=2.8, и без закрепления pip притаскивает с
                    # PyPI вторую копию посвежее: в наборе оказываются torch
                    # 2.13 и torchaudio 2.11, а офлайн-установка потом
                    # собирает из них неработающую пару.
                    "torch==2.11.0", "torchaudio==2.11.0",
                    "-r", str(ROOT / "requirements-diarization.txt"),
                ],
            ],
        ),
    )

    for name, title, commands in jobs:
        target = ROOT / "bundle" / name
        if has_wheels(target) and not refresh:
            log(f"   [+] {title} — колёса уже скачаны")
            continue
        log(f"   ... скачиваются колёса: {title}")
        target.mkdir(parents=True, exist_ok=True)
        failed = False
        for command in commands:
            arguments = [str(python)] + [
                part.replace("{target}", str(target)) for part in command
            ]
            result = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip().splitlines()
                log(f"   [-] {title}: {(tail or [''])[-1][:180]}")
                failed = True
                break
        if not failed:
            log(f"   [+] {title} — колёса готовы")


def export_model(target_root: Path, folder: str, title: str) -> bool:
    """Модель из кеша Hugging Face — с раскрытием символьных ссылок.

    В кеше Hugging Face файлы снимка — символьные ссылки на blobs. Обычное
    копирование на другую машину принесло бы ссылки в никуда, поэтому
    copytree вызывается с symlinks=False: он кладёт содержимое, а не ссылку.
    """
    source = HUB_CACHE / folder
    if not (source / "snapshots").exists():
        log(f"   [?] Модель {title} в кеше не найдена — поедет без неё")
        return False
    destination = target_root / "bundle" / "hf-cache" / folder
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = copy_tree(source, destination)
    log(f"   [+] Модель {title} — {human(size)}")
    return True


FIRST_READ = """Локальный Whisper — перенос на эту машину

1. Распакуйте папку целиком туда, где ей жить. Путь без пробелов не требуется,
   но диск нужен с запасом: после установки папка займёт около 6 ГБ.

2. Запустите «Установить Whisper.bat». В конце он реально распознает тестовую
   запись и покажет настоящую скорость на этом процессоре.

   Модели, пакеты Python и пакеты интерфейса уже лежат в этой папке — качать
   их не придётся. Из сети ставятся только четыре программы через winget:
   Python 3.11, Node.js, FFmpeg и PowerShell 7. Если какая-то из них уже есть,
   она не трогается.

   Если что-то не встало — запустите ещё раз. Установщик доделывает только то,
   что не вышло, и не трогает уже готовое.

3. Запустите «Запустить Whisper.bat». Откроется http://127.0.0.1:3000

Что стоит знать про машину без видеокарты:

  Распознавание уйдёт на процессор. Насколько это медленнее — покажет проверка
  в конце установки; она печатает скорость и оценку «час записи займёт столько
  то». Режим «быстро» на процессоре не помогает: время съедает энкодер, а он
  от ширины поиска не зависит.

  Разметка спикеров тоже уйдёт на процессор и станет самой долгой частью —
  дольше, чем само распознавание. Выключается в Настройки -> Спикеры.

Telegram-бот и Android-приложение не входят в исходники студии.

Проверить состояние в любой момент: «Проверить Whisper.bat».
"""


def build_zip(stage: Path) -> Path | None:
    """Архив через встроенный в Windows tar: быстрее и без лимитов Compress-Archive."""
    archive = stage.with_suffix(".zip")
    if archive.exists():
        archive.unlink()
    # Именно системный tar.exe, а не первый из PATH. В PATH может стоять GNU tar
    # из Git for Windows, и он читает "E:\\whisper-portable" как адрес удалённой
    # машины: "Cannot connect to E: resolve failed". Встроенный в Windows bsdtar
    # понимает буквы дисков и умеет писать zip ключом -a.
    system_tar = (
        pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "tar.exe"
    )
    tar = str(system_tar) if system_tar.exists() else shutil.which("tar")
    if not tar:
        log("   [?] tar.exe не найден — архив не собран, копируйте папку как есть")
        return None
    started = time.perf_counter()
    result = subprocess.run(
        [tar, "-a", "-c", "-f", str(archive), "-C", str(stage.parent), stage.name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        log(f"   [-] Архив не собрался: {(result.stderr or '').strip()[:200]}")
        return None
    log(
        f"   [+] Архив собран за {time.perf_counter() - started:.0f} с — "
        f"{human(archive.stat().st_size)}"
    )
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка копии сервиса для переноса")
    parser.add_argument(
        "--destination",
        default=str(ROOT.parent / "whisper-portable"),
        help="Куда сложить копию (по умолчанию рядом с проектом)",
    )
    parser.add_argument(
        "--with-data",
        action="store_true",
        help="Взять с собой записи, базу и настройки",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Оставить папку, не собирать архив",
    )
    parser.add_argument(
        "--lean",
        action="store_true",
        help="Без пакетов Python и node_modules — архив меньше, установка дольше",
    )
    parser.add_argument(
        "--refresh-wheels",
        action="store_true",
        help="Перекачать колёса Python заново",
    )
    arguments = parser.parse_args()

    stage = Path(arguments.destination).resolve()
    if stage == ROOT or ROOT in stage.parents:
        log("Каталог назначения не должен быть внутри проекта.")
        return 1

    log(f"Сборка копии для переноса\nИсточник:  {ROOT}\nНазначение: {stage}\n")
    if stage.exists():
        log("Очищается прошлая сборка...")
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    total = 0

    log("Каталоги:")
    for name in DIRECTORIES:
        source = ROOT / name
        if not source.is_dir():
            continue
        size = copy_tree(source, stage / name)
        total += size
        log(f"   [+] {name} — {human(size)}")

    log("Файлы:")
    missing = []
    for name in FILES:
        source = ROOT / name
        if not source.is_file():
            missing.append(name)
            continue
        shutil.copy2(source, stage / name)
        total += source.stat().st_size
    log(f"   [+] {len(FILES) - len(missing)} файлов в корне")
    if missing:
        log(f"   [?] не найдены: {', '.join(missing)}")

    log("Данные:")
    data_target = stage / "data"
    if arguments.with_data:
        size = copy_tree(ROOT / "data", data_target)
        total += size
        log(f"   [+] data целиком — {human(size)}")
    else:
        data_target.mkdir(parents=True, exist_ok=True)
        for name in DATA_FILES:
            source = ROOT / "data" / name
            if source.is_file():
                shutil.copy2(source, data_target / name)
                total += source.stat().st_size
        log("   [+] только тестовые записи для проверки")
        # Токен Hugging Face едет отдельно от записей: он нужен, чтобы на
        # новой машине можно было дозагрузить модель, если её не оказалось.
        token = ROOT / "data" / "diarization" / "huggingface.token"
        if token.is_file():
            (data_target / "diarization").mkdir(parents=True, exist_ok=True)
            shutil.copy2(token, data_target / "diarization" / token.name)
            log("   [+] token Hugging Face")

    log("Модели разметки спикеров:")
    export_model(stage, PYANNOTE_MODEL, "Pyannote")
    export_model(stage, NEMOTRON_MODEL, "Nemotron 3")

    if arguments.lean:
        log("Пакеты: пропущены по ключу --lean, поставятся из сети на месте")
    else:
        log("Пакеты Python:")
        ensure_wheels(refresh=arguments.refresh_wheels)
        for name in (SERVICE_WHEELS, DIARIZATION_WHEELS):
            source = ROOT / "bundle" / name
            if not has_wheels(source):
                continue
            size = copy_tree(source, stage / "bundle" / name)
            total += size
            log(f"   [+] bundle/{name} — {human(size)}")

        log("Пакеты интерфейса:")
        modules = ROOT / "node_modules"
        if (modules / "vinext").is_dir():
            # Едут готовыми, а не через npm ci: платформа та же (Windows x64),
            # интерфейс уже собран в dist, и Node на ноутбуке нужен только
            # чтобы это запустить. Заодно отпадает сборка.
            size = copy_tree(modules, stage / "node_modules", filtered=False)
            total += size
            log(f"   [+] node_modules — {human(size)}")
        else:
            log("   [?] node_modules не найдены — на ноутбуке пойдёт npm ci")

    (stage / "ЧИТАТЬ ПЕРВЫМ.txt").write_text(FIRST_READ, encoding="utf-8")
    log("   [+] инструкция «ЧИТАТЬ ПЕРВЫМ.txt» в корне")

    log("")
    log(f"Скопировано {human(total)}.")

    if not arguments.no_zip:
        log("Архив:")
        archive = build_zip(stage)
        if archive is not None:
            log("")
            log(f"Готово: {archive}")
            log("На ноутбуке: распакуйте и запустите «Установить Whisper.bat».")
            return 0

    log("")
    log(f"Готово: {stage}")
    log("Скопируйте папку на ноутбук и запустите «Установить Whisper.bat».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
