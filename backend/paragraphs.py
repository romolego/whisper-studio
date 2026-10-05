"""Сборка сегментов Whisper в абзацы.

Whisper режет речь не по смыслу, а по своему окну: сегмент — это 2–10 секунд
звука, 30–150 знаков, и граница между ними приходится куда попало, часто
посреди мысли. Для субтитров это правильно, для чтения и для передачи текста
дальше — нет.

Раньше результат выходил в двух одинаково неудобных видах. Без таймкодов все
сегменты склеивались одной строкой в несколько десятков тысяч знаков: сплошная
стена, в которой глазом не найти начало темы. С таймкодами каждый сегмент
получал свою строку — рваная лесенка по пять слов. Оба вида одинаково плохо
читаются человеком и одинаково плохо разбираются моделью, которой этот текст
отдают дальше: связность абзаца — сигнал структуры, а без него модель видит
поток слов и хуже держит нить рассуждения.

Здесь сегменты собираются обратно в абзацы. Границу ставим там, где она есть
в самой речи, а не по счётчику знаков:

- смена спикера — всегда новый абзац, это разные реплики;
- длинная пауза (3 с и больше) — говорящий закончил мысль и молчал;
- после 400 знаков — первая же точка или заметная пауза;
- после 1200 знаков — ближайшая граница сегмента, даже если человек говорит
  без единой точки полторы минуты подряд.

Порог в 400 знаков выбран по типичному абзацу устной речи: три-пять фраз,
20–40 секунд. Жёсткий предел в 1200 нужен для записей, где модель не
расставила знаков совсем: без него такой файл снова превратился бы в стену.

Считать границы надо **до** снятия пунктуации. Режим «Без знаков» удаляет
точки, по которым абзац и определяется, поэтому группировка всегда работает по
исходному тексту модели, а очистка применяется уже к готовым абзацам.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Абзац доращивается до этого размера, и только потом ищется первая точка.
SOFT_LIMIT_CHARS = 400
# Предел, после которого абзац рвётся на ближайшей границе сегмента.
HARD_LIMIT_CHARS = 1200
# Пауза, которая годится в границу абзаца после SOFT_LIMIT_CHARS.
PAUSE_BREAK_SECONDS = 1.2
# Пауза, которая сама по себе означает конец мысли.
LONG_PAUSE_SECONDS = 3.0
# Ниже этого абзац не рвём даже по длинной паузе: иначе «Да.» и «Угу.»
# станут отдельными абзацами.
MIN_PARAGRAPH_CHARS = 150

# Конец предложения: знак, за которым могут стоять кавычка или скобка.
_SENTENCE_END = re.compile(r"[.!?…]+[\"'»)\]]*\s*$")


def segment_speaker(segment: dict[str, Any]) -> Any:
    """Спикер сегмента: свой, а если его нет — первого слова с меткой."""
    if segment.get("speaker") is not None:
        return segment["speaker"]
    for word in segment.get("words") or []:
        if word.get("speaker") is not None:
            return word["speaker"]
    return None


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END.search(text.strip()))


def _breaks_here(
    length: int,
    gap: float,
    previous_text: str,
    speaker_changed: bool,
) -> bool:
    if speaker_changed:
        return True
    if length >= HARD_LIMIT_CHARS:
        return True
    if length >= SOFT_LIMIT_CHARS and (
        _ends_sentence(previous_text) or gap >= PAUSE_BREAK_SECONDS
    ):
        return True
    return length >= MIN_PARAGRAPH_CHARS and gap >= LONG_PAUSE_SECONDS


def group(segments: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Разложить сегменты по абзацам, сохранив их самих нетронутыми.

    Возвращается список абзацев, каждый — список исходных сегментов. Ничего не
    склеивается и не переписывается: вызывающий сам решает, что делать с
    текстом внутри абзаца, потому что вид зависит от режима таймкодов.
    """
    paragraphs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    length = 0
    previous_end = 0.0
    previous_text = ""
    previous_speaker: Any = None

    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment.get("start") or 0)
        speaker = segment_speaker(segment)
        if current and _breaks_here(
            length,
            max(0.0, start - previous_end),
            previous_text,
            speaker != previous_speaker,
        ):
            paragraphs.append(current)
            current = []
            length = 0
        current.append(segment)
        length += len(text) + 1
        previous_end = max(start, float(segment.get("end") or start))
        previous_text = text
        previous_speaker = speaker

    if current:
        paragraphs.append(current)
    return paragraphs
