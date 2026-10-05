"""Проверка установки: что есть, чего нет и с какой скоростью это считает.

Запускается сам в конце установки и отдельно — «Проверить Whisper.bat».
Отвечает на вопрос, который иначе выясняется методом «загрузил файл и ждёшь»:
сервис действительно соберётся и посчитает, или чего-то не хватает.

Проверка не верит инвентарю на слово. Инвентарь говорит, что модель лежит на
диске; здесь она реально загружается и реально распознаёт тестовую запись.
Разница существенная: битый model.bin, несовместимая сборка CTranslate2 и
отсутствующие DLL видны только на настоящем проходе.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Вывод всегда в UTF-8. Кодовая страница консоли Windows по умолчанию не знает
# ни «×», ни половины русского текста, и проверка падала бы на печати отчёта —
# самом безобидном месте из всех.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# CUDA-библиотеки лежат в проекте, а не в системе. Без этого на машине с
# видеокартой проверка ушла бы на процессор и показала неверную скорость.
CUDA_BIN = ROOT / "vendor" / "cuda" / "bin"
if CUDA_BIN.exists():
    os.add_dll_directory(str(CUDA_BIN))
    os.environ["PATH"] = f"{CUDA_BIN}{os.pathsep}{os.environ['PATH']}"

SAMPLE = ROOT / "data" / "test-ru.wav"

OK = "  [+]"
NO = "  [-]"
WARN = "  [?]"


def line(mark: str, title: str, detail: str = "") -> None:
    print(f"{mark} {title}" + (f" — {detail}" if detail else ""), flush=True)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)
    print("-" * len(title), flush=True)


def check_components() -> tuple[list[str], list[str]]:
    from backend import components

    data = components.inventory(refresh=True)
    groups = {
        "runtime": "Программы",
        "model": "Модели",
        "python": "Пакеты Python",
    }
    missing_required: list[str] = []
    missing_optional: list[str] = []

    for group, title in groups.items():
        section(title)
        for item in data["items"]:
            if item["group"] != group:
                continue
            if item["present"]:
                line(OK, item["title"], item["detail"])
            elif item["required"]:
                missing_required.append(item["title"])
                line(NO, item["title"], item["impact"])
            else:
                missing_optional.append(item["title"])
                line(WARN, item["title"], item["impact"])
    return missing_required, missing_optional


def cpu_instructions() -> str:
    """Какие векторные расширения есть у процессора.

    Это не украшение отчёта, а главный множитель скорости на процессоре.
    CTranslate2 считает матрицы в int8, и без AVX2 целочисленные операции
    остаются 128-битными: тот же алгоритм идёт примерно вдвое медленнее при
    той же тактовой частоте. Замер на Xeon E3-1240 v2 (AVX без AVX2) —
    0.83x реального времени; на процессоре тех же лет с AVX2 было бы заметно
    быстрее без единой правки в коде.
    """
    try:
        import ctypes

        present = ctypes.windll.kernel32.IsProcessorFeaturePresent
    except (AttributeError, OSError):
        return ""
    # Номера из winnt.h: PF_AVX_INSTRUCTIONS_AVAILABLE и соседние.
    flags = []
    for number, name in ((39, "AVX"), (40, "AVX2"), (41, "AVX-512")):
        try:
            if present(number):
                flags.append(name)
        except OSError:
            return ""
    return ", ".join(flags) if flags else "нет векторных расширений"


def long_sample(destination: Path, seconds: float = 60.0) -> Path | None:
    """Минута звука из восьмисекундного образца.

    На коротком файле замер врёт в разы: разогрев VAD и первая свёртка стоят
    столько же, сколько сама расшифровка, и восьмисекундная запись показывает
    скорость вчетверо ниже настоящей. Час записи по такой цифре оценивается
    неправильно — а именно эту оценку человек и читает.
    """
    import wave

    try:
        with wave.open(str(SAMPLE), "rb") as source:
            params = source.getparams()
            frames = source.readframes(source.getnframes())
        one_pass = params.nframes / float(params.framerate)
        if one_pass <= 0:
            return None
        repeats = max(1, int(seconds / one_pass) + 1)
        with wave.open(str(destination), "wb") as target:
            target.setparams(params)
            for _ in range(repeats):
                target.writeframes(frames)
    except Exception:
        return None
    return destination


def check_speed() -> bool | None:
    """Реальный проход по тестовой записи: скорость и работоспособность."""
    section("Проверка распознавания")
    if not SAMPLE.exists():
        line(
            WARN,
            "Замер пропущен: нет тестовой записи",
            "записи не входят в копию с GitHub; для проверки положите короткую "
            f"русскую запись в {SAMPLE} и запустите проверку повторно",
        )
        return None

    from backend import transcriber

    threads = transcriber.cpu_threads()
    line(OK, "Потоков процессора для расчёта", str(threads))
    instructions = cpu_instructions()
    if instructions:
        line(OK, "Инструкции процессора", instructions)

    started = time.perf_counter()
    try:
        model, device = transcriber._load_model("ru")
    except Exception as error:  # noqa: BLE001 — показать причину, а не трассировку
        line(NO, "Загрузка модели", str(error)[:200])
        return False
    load_seconds = time.perf_counter() - started

    where = "видеокарта" if device == "cuda" else "процессор"
    line(OK, "Модель загружена", f"{where}, {load_seconds:.1f} с")

    def run(path: Path) -> tuple[str, float]:
        started_at = time.perf_counter()
        segments, _info = model.transcribe(
            str(path),
            language="ru",
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
        )
        recognised = " ".join(segment.text.strip() for segment in segments)
        return recognised.strip(), time.perf_counter() - started_at

    # Первый проход разогревочный: на нём грузится VAD и прогреваются ядра.
    # Его время в замер не идёт, иначе результат зависит от того, что модель
    # делала минуту назад.
    try:
        text, _warmup_seconds = run(SAMPLE)
    except Exception as error:  # noqa: BLE001
        line(NO, "Распознавание", str(error)[:200])
        return False

    if not text:
        line(NO, "Распознавание", "модель вернула пустой текст")
        return False
    line(OK, "Распознано", f"«{text[:60]}…»" if len(text) > 60 else f"«{text}»")

    import tempfile

    measured = ""
    with tempfile.TemporaryDirectory() as workspace:
        sample = long_sample(Path(workspace) / "speed.wav")
        if sample is None:
            line(WARN, "Скорость", "не удалось подготовить отрезок для замера")
            return True
        import wave

        with wave.open(str(sample), "rb") as handle:
            audio_seconds = handle.getnframes() / float(handle.getframerate())
        try:
            measured, run_seconds = run(sample)
        except Exception as error:  # noqa: BLE001
            line(WARN, "Скорость", f"замер не удался: {str(error)[:120]}")
            return True

    del measured
    speed = (audio_seconds / run_seconds) if run_seconds > 0 else 0.0
    line(
        OK,
        "Скорость",
        f"{speed:.1f}x реального времени ({where}), "
        f"замер на {audio_seconds:.0f} с звука",
    )
    if speed > 0:
        line(OK, "Час записи займёт", f"около {60.0 / speed:.0f} мин")
    return True


def check_diarization() -> None:
    section("Разметка спикеров")
    from backend import diarization

    state = diarization.status()
    nemotron = state["engines"]["nemotron"]
    if nemotron["available"]:
        where = "видеокарте" if nemotron["device"] == "cuda" else "процессоре"
        line(OK, "Nemotron 3 готов", f"считает на {where}")
    elif not nemotron["package_installed"]:
        line(
            WARN,
            "Nemotron 3 не установлен",
            "повторите «Установить Whisper.bat», спикеров размечает Pyannote",
        )
    else:
        line(
            WARN,
            "Модель Nemotron 3 не скачана",
            "Настройки → Спикеры → «Скачать модель», токен не нужен",
        )

    if state["available"]:
        where = "видеокарте" if state["device"] == "cuda" else "процессоре"
        line(OK, "Pyannote готов", f"считает на {where}")
        line(OK, "Среда", state["runtime"])
        if state["device"] == "cpu":
            line(
                WARN,
                "На процессоре разметка идёт медленно",
                "примерно половина длительности записи или дольше",
            )
        return

    if not state["package_installed"]:
        line(NO, "Pyannote не установлен", "разделение по спикерам недоступно")
    elif not state["model_ready"]:
        line(
            NO,
            "Модель Pyannote не скачана",
            "откройте Настройки → Спикеры и введите token Hugging Face",
        )
    else:
        line(NO, "Pyannote не активирован", "проверьте Настройки → Спикеры")


def main() -> int:
    print("Проверка установки локального Whisper", flush=True)
    print(f"Каталог: {ROOT}", flush=True)

    missing_required, missing_optional = check_components()
    check_diarization()
    works = check_speed()

    section("Итог")
    if missing_required:
        line(NO, "Не хватает обязательного", ", ".join(missing_required))
    if missing_optional:
        line(WARN, "Необязательное отсутствует", ", ".join(missing_optional))
    if works is False:
        line(NO, "Распознавание не работает", "см. сообщения выше")
        return 1
    if missing_required:
        return 1
    if works is None:
        line(
            WARN,
            "Обязательные компоненты на месте",
            "распознавание и скорость ещё не проверены; запустите сервис "
            "и расшифруйте короткую запись",
        )
        return 0
    line(OK, "Сервис готов к работе", "запускайте «Запустить Whisper.bat»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
