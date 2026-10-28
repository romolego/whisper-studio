"""Очередь доступа к видеокарте: диктовка идёт вперёд длинных файлов.

Диктовка — интерактивная работа: человек сидит и ждёт текст, счёт занимает
секунды. Транскрибация файла — фоновая: шестичасовая запись считается 54
минуты. Раньше и то, и другое шло через один рабочий поток, и приоритет 0
против 1 помогал только в ожидании: уже запущенный файл досчитывался до конца.
На реальных записях пользователя это означало ждать диктовку до часа.

Здесь два класса работы делят видеокарту по приоритету, а не поровну:

* `interactive()` — диктовка. Заявка регистрируется сразу; как только батч
  дойдёт до ближайшего чекпоинта, слот достаётся ей.
* `batch()` — транскрибация файла или разметка спикеров. Держит слот, но
  обязана вызывать `checkpoint()` там, где работу можно приостановить без
  потерь.

Одновременно считает только один класс, и это выбрано намеренно, по замеру на
GTX 1060 6 ГБ: две модели Whisper в памяти занимают 1734 МиБ, а пик разметки
спикеров — 5360 МиБ из 6144. Параллельный счёт в такую карту не поместился бы.

Почему уступать получается дёшево: генератор faster-whisper ленивый, сегменты
считаются по требованию. Пока цикл транскрибации стоит в `checkpoint()`,
видеокарта не загружена вообще. Один сегмент на GPU считается около 2.5 с —
столько и ждёт диктовка вместо часа.

Чего этот механизм не умеет: у разметки спикеров чекпоинтов нет, это отдельный
процесс Pyannote, и прервать его посередине нельзя. Пока идёт разметка,
диктовка ждёт её окончания — на 44-минутной записи это около 5 минут.

Голодание батча возможно и допустимо: диктовка коротка и приходит по воле
человека, а файл всё равно досчитается.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator


_condition = threading.Condition()
_interactive_waiting = 0
_interactive_active = 0
_batch_active = 0

# Метка «этот поток держит слот батча». Без неё вызов checkpoint() из кода,
# который батчем не обёрнут (например прямой вызов transcribe_job в тестах),
# уводил бы счётчик в минус.
_local = threading.local()


def _interactive_pending() -> bool:
    return bool(_interactive_waiting or _interactive_active)


@contextmanager
def interactive() -> Iterator[None]:
    """Слот для диктовки: получает видеокарту при первой возможности."""
    global _interactive_waiting, _interactive_active
    with _condition:
        _interactive_waiting += 1
        # Разбудить батч: он мог стоять в checkpoint() до этой заявки.
        _condition.notify_all()
        while _batch_active:
            _condition.wait()
        _interactive_waiting -= 1
        _interactive_active += 1
    try:
        yield
    finally:
        with _condition:
            _interactive_active -= 1
            _condition.notify_all()


@contextmanager
def batch(cancel_check: Callable[[], None] | None = None) -> Iterator[None]:
    """Слот для фоновой работы: уступает диктовке в checkpoint()."""
    global _batch_active
    if getattr(_local, "batch", False):
        yield
        return
    with _condition:
        while _interactive_pending() or _batch_active:
            if cancel_check:
                cancel_check()
            _condition.wait(0.5 if cancel_check else None)
        if cancel_check:
            cancel_check()
        _batch_active += 1
    previous = getattr(_local, "batch", False)
    _local.batch = True
    try:
        yield
    finally:
        _local.batch = previous
        with _condition:
            _batch_active -= 1
            _condition.notify_all()


def checkpoint() -> None:
    """Уступить видеокарту, если диктовка ждёт. Иначе — ничего не делать.

    Вызывается между сегментами Whisper. Вне слота батча это пустая операция,
    поэтому вызов безопасен из любого места.
    """
    global _batch_active
    if not getattr(_local, "batch", False):
        return
    with _condition:
        if not _interactive_pending():
            return
        _batch_active -= 1
        _condition.notify_all()
        while _interactive_pending() or _batch_active:
            _condition.wait()
        _batch_active += 1


def state() -> dict[str, int]:
    """Снимок счётчиков — для диагностики и тестов."""
    with _condition:
        return {
            "interactive_waiting": _interactive_waiting,
            "interactive_active": _interactive_active,
            "batch_active": _batch_active,
        }
