"""Отсев мусора, который Whisper дописывает в тишине, шуме и музыке.

Модель обучали на видео с YouTube вместе с их субтитрами, поэтому на паузах и
неразборчивых участках она не молчит, а продолжает самый вероятный шаблон из
обучающих данных: подпись автора субтитров, титры канала или тег звука. Такие
фразы физически не звучали в записи, и в расшифровке им не место.

Проверено на реальных данных: последний сегмент шестичасовой записи Selectel —
`Субтитры делал DimaTorzok`. Именно поэтому здесь шаблоны, а не список строк:
у той же подписи ходят варианты «сделал», «делал», «создавал», «добавил»,
«подогнал», и заранее все не выпишешь.

Три уровня строгости, по убыванию уверенности:

1. `_CREDITS` — подписи авторов субтитров и расшифровок. В живой речи не
   встречаются никогда, поэтому режутся в любом месте сегмента: подпись
   обычно приклеена хвостом к последней настоящей фразе.
2. `_WHOLE_SEGMENT` — шаблонные вставки вроде «Продолжение следует». В речи
   теоретически возможны, поэтому удаляются только когда составляют сегмент
   целиком.
3. `_AMBIGUOUS` — прощания из титров: «спасибо за просмотр», «подписывайтесь
   на канал». По умолчанию **выключены**, см. `REMOVE_AMBIGUOUS_OUTRO`.

Плюс теги звука: `[музыка]`, `(аплодисменты)`, `ВЕСЕЛАЯ МУЗЫКА`, `♪♪♪`.
"""
from __future__ import annotations

import re
from typing import Any


# Фразы третьего уровня в реальной речи звучат постоянно: «спасибо за
# внимание» говорят в конце каждого доклада, «подписывайтесь на канал» — в
# каждом видео с YouTube, а сервис как раз расшифровывает и то, и другое.
# Отличить настоящее прощание от галлюцинации по одному тексту невозможно,
# поэтому по умолчанию они остаются. True здесь начнёт стирать живую речь.
REMOVE_AMBIGUOUS_OUTRO = False


# Символы обрамления тега. Кавычки сюда намеренно не входят: «Да» в кавычках —
# это нормальная речь, а `«Симон»` и так ловится шаблоном подписи.
_WRAPPERS = "[](){}<>*#♪♫~"

# Односложные теги звука. Нужны, чтобы капсом из одного слова
# (`АПЛОДИСМЕНТЫ`) не пришлось удалять аббревиатуры вроде `ИИ`.
_TAG_WORDS = {
    # русский
    "музыка", "мелодия", "заставка", "аплодисменты", "смех", "хохот",
    "кашель", "чихание", "выстрел", "выстрелы", "перестрелка", "взрыв",
    "шум", "гудок", "рев", "рычание", "лай", "мяуканье", "пение", "песня",
    "свист", "крик", "вопль", "стук", "скрип", "звон", "звонок", "сирена",
    "плеск", "шаги", "дождь", "гром", "ветер", "треск", "храп", "вздох",
    "всхлип", "рыдания", "шепот", "бормотание", "неразборчиво", "тишина",
    "пауза", "сигнал", "гул", "грохот", "шипение", "щелчок", "топот",
    "субтитры", "титры",
    # английский
    "music", "applause", "laughter", "laughs", "laughing", "silence",
    "inaudible", "crosstalk", "sighs", "sigh", "coughs", "cough",
    "footsteps", "beep", "beeping", "noise", "static", "chuckles",
    "clapping", "ringing", "knocking", "thunder", "wind", "rain", "engine",
    "siren", "gunshot", "gunshots", "explosion", "whistle", "singing",
    "humming", "breathing", "blank_audio",
}

# Кто «сделал» субтитры. Список глаголов открытый: в данных встречались
# «сделал», «делал», «создавал», «добавил», «подогнал».
_CREDIT_VERBS = (
    r"сделал|сделала|делал|делала|создал|создала|создавал|создавала|"
    r"добавил|добавила|подготовил|подготовила|подогнал|подогнала|"
    r"наложил|наложила|перевел|перевела|переводил|переводила|"
    r"предоставил|предоставила|редактировал|редактировала|правил|правила|"
    r"выполнил|выполнила|готовил|готовила|набрал|набрала|печатал|печатала"
)

# Шаблоны первого уровня. Ищутся в любом месте сегмента и режут всё до конца:
# подпись — это всегда последнее, что модель дописывает.
_CREDIT_SOURCES = (
    # русские подписи к субтитрам и расшифровкам
    rf"субтитр\w*\s+(?:{_CREDIT_VERBS})\b",
    rf"субтитр\w*\s+\S+\s+(?:{_CREDIT_VERBS})\b",
    r"субтитр\w*\s+и\s+перевод\b",
    r"перевод\s+и\s+субтитр\w*",
    r"спасибо\s+за\s+субтитр\w*",
    r"расшифровка\s+(?:текста\s+)?выполнена\b",
    # уникальные имена из обучающих данных — сами по себе приговор
    r"dimatorzok",
    r"дубровском?у?\s+за\s+субтитр\w*",
    # английские подписи
    r"subtitle[sd]?\s+by\b",
    r"subs\s+by\b",
    r"caption(?:s|ed|ing)?\s+(?:by|provided\s+by|courtesy\s+of)\b",
    r"closed\s+caption(?:s|ing)?\s+(?:by|provided)\b",
    r"transcri(?:ption|pt|bed)\s+(?:by|provided\s+by)\b",
    r"translat(?:ion|ed)\s+by\b",
    r"amara\s*org",
    r"castingwords",
    r"zeoranger",
    r"audiojungle",
    r"satsang\s+with\s+mooji",
    r"mooji\s*org",
    # прочие языки: в русской и английской речи эти слова не встречаются,
    # а в режиме «автоопределение языка» модель их выдаёт
    r"sous\s*titres?\s+(?:realises?|par|traduction)\b",
    r"altyaz[iı]\b",
    r"titulky\s+vytvo\w+",
    r"untertitel\s+(?:von|im\s+auftrag)\b",
    r"ondertiteling\s+door\b",
    r"subtitulos?\s+(?:por|realizados)\b",
    r"sottotitoli\s+(?:e\s+revisione\s+)?a\s+cura\s+di\b",
    r"legendas?\s+por\b",
    r"字幕",
)

# «Редактор субтитров А.Синецкая Корректор А.Егорова» и её родня.
#
# Эту семью нельзя ловить по нормализованному тексту: там «А.Егорова»
# превращается в «а егорова», и шаблон `корректор [а-я] \w+` начинает съедать
# живую речь — «здесь нужен хороший корректор и редактор» разбирается точно так
# же («и» вместо инициала). Отличает подпись именно инициал с точкой, поэтому
# проверяем исходный текст с регистром и знаками.
_RAW_CREDIT_SOURCES = (
    r"(?:Редактор\s+субтитров|Корректор)\s+[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+",
)

# Шаблоны второго уровня: удаляются только если это весь сегмент целиком.
_WHOLE_SEGMENT_SOURCES = (
    r"продолжение следует",
    r"смотрите продолжение (?:в|во)\s.*",
    r"продолжение (?:в|во) (?:следующ\w+|втор\w+|\d+)\s.*",
    r"смотрите (?:другие|следующие) видео",
    r"субтитры",
    r"титры",
    r"редактор субтитров",
    r"корректор",
    r"конец (?:фильма|первой части|второй части|видео)",
    r"the end",
    r"to be continued",
)

# Шаблоны третьего уровня: включаются флагом REMOVE_AMBIGUOUS_OUTRO.
_AMBIGUOUS_SOURCES = (
    r"спасибо за просмотр\W*",
    r"спасибо за внимание\W*",
    r"подписывайтесь на (?:мой |наш )?канал\W*",
    r"подпишитесь на (?:мой |наш )?канал\W*",
    r"подпишись(?: на канал)?(?: ставь(?:те)? лайк\w*)?\W*",
    r"не забудьте подписаться\W*",
    r"ставьте лайки(?: и подписывайтесь.*)?",
    r"жмите на колокольчик.*",
    r"всем пока|пока пока|до встречи|до новых встреч|всего доброго",
    r"поехали",
    r"thanks? for watching.*",
    r"thank you for watching.*",
    r"please subscribe.*",
    r"don t forget to (?:like and )?subscribe.*",
    r"(?:and )?i ll see you (?:in the )?next \w+",
    r"bye bye",
)


def _compile_credits() -> tuple[re.Pattern[str], ...]:
    # Слева — граница слова, чтобы «субтитры» не сработали внутри другого
    # слова; справа — всё до конца строки, потому что режем хвост.
    return tuple(
        re.compile(rf"(?:^|(?<= ))(?:{source}).*$")
        for source in _CREDIT_SOURCES
    )


_CREDITS = _compile_credits()
_RAW_CREDITS = tuple(
    re.compile(rf"(?:{source}).*$") for source in _RAW_CREDIT_SOURCES
)
_WHOLE_SEGMENT = tuple(
    re.compile(source) for source in _WHOLE_SEGMENT_SOURCES
)
_AMBIGUOUS = tuple(re.compile(source) for source in _AMBIGUOUS_SOURCES)

_REPEATED_LETTER = re.compile(r"(\w)\1{4,}")


def _normalise_with_map(text: str) -> tuple[str, list[int]]:
    """Нормализованный текст и позиция каждого его символа в исходном.

    Карта нужна, чтобы найденный по нормализованному тексту мусор вырезать из
    исходного, сохранив в оставшейся части регистр и знаки препинания.
    """
    chars: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for index, char in enumerate(text):
        lowered = "е" if char in "ёЁ" else (char.lower()[:1] or char)
        if lowered.isalnum():
            if pending_space and chars:
                chars.append(" ")
                offsets.append(index)
            pending_space = False
            chars.append(lowered)
            offsets.append(index)
        else:
            pending_space = True
    return "".join(chars), offsets


def _normalise(text: str) -> str:
    return _normalise_with_map(text)[0]


def _sound_tag_reason(text: str) -> str | None:
    """Причина, по которой текст — тег звука, а не речь."""
    stripped = text.strip()
    if not stripped:
        return None
    if not any(char.isalnum() for char in stripped):
        # ♪♪♪, ***, «— — —»: маркер музыки или просто мусор
        return "нетекстовый маркер"
    words = _normalise(stripped).split()
    if not words:
        return None
    if stripped[0] in _WRAPPERS and stripped[-1] in _WRAPPERS and len(words) <= 5:
        return "тег в скобках"
    if not any(char.islower() for char in stripped):
        # Капс Whisper выдаёт только для таких тегов; настоящую речь, даже
        # крик, он оформляет обычным регистром.
        if len(words) >= 2:
            return "тег звука капсом"
        if words[0] in _TAG_WORDS:
            return "тег звука капсом"
        if _REPEATED_LETTER.fullmatch(words[0]):
            return "растянутый звук"
    return None


def _trim_words(
    words: list[dict[str, Any]],
    junk: str,
) -> list[dict[str, Any]]:
    """Снять с конца слова, которые составляют вырезанный хвост.

    Слова снимаются по одному, и каждое снятое отрезается от конца `junk`.
    Так это работает и когда Whisper разбил `DimaTorzok` на несколько слов.
    """
    kept = list(words)
    rest = junk
    while kept and rest:
        candidate = _normalise(kept[-1].get("word") or "")
        if candidate and rest.endswith(candidate):
            rest = rest[: len(rest) - len(candidate)].strip()
            kept.pop()
            continue
        break
    return kept


def _earliest_credit(
    text: str,
    normalised: str,
    offsets: list[int],
) -> tuple[int, str] | None:
    """Самое раннее начало подписи: позиция в исходном тексте и сам мусор.

    Побеждает именно самое раннее совпадение, а не первый подошедший шаблон.
    Иначе порядок шаблонов начинает решать результат: в
    `Closed captioning provided by Muhsen` общий шаблон `caption... by` попадает
    на седьмой символ и оставляет висеть обрубок `Closed`, хотя более точный
    шаблон `closed caption... provided` покрывает строку с самого начала.
    """
    best: tuple[int, str] | None = None
    for pattern in _CREDITS:
        match = pattern.search(normalised)
        if match and (best is None or offsets[match.start()] < best[0]):
            best = (offsets[match.start()], normalised[match.start():])
    for pattern in _RAW_CREDITS:
        match = pattern.search(text)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), _normalise(text[match.start():]))
    return best


def _clean_segment(
    segment: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Очищенный сегмент (или None, если удалён целиком) и причина."""
    text = segment.get("text") or ""
    reason = _sound_tag_reason(text)
    if reason:
        return None, reason

    normalised, offsets = _normalise_with_map(text)
    if not normalised:
        return None, "пустой сегмент"

    cut = _earliest_credit(text, normalised, offsets)
    if cut is not None:
        index, junk = cut
        head = text[:index].rstrip(" \t —–-,;:")
        if not _normalise(head):
            return None, f"подпись автора: {junk[:70]}"
        cleaned = dict(segment)
        cleaned["text"] = head
        words = segment.get("words")
        if words:
            kept = _trim_words(words, junk)
            cleaned["words"] = kept
            if kept and kept[-1].get("end") is not None:
                cleaned["end"] = float(kept[-1]["end"])
        return cleaned, f"приклеенная подпись: {junk[:70]}"

    for pattern in _WHOLE_SEGMENT:
        if pattern.fullmatch(normalised):
            return None, f"шаблонная вставка: {normalised[:70]}"

    if REMOVE_AMBIGUOUS_OUTRO:
        for pattern in _AMBIGUOUS:
            if pattern.fullmatch(normalised):
                return None, f"прощание из титров: {normalised[:70]}"

    return segment, None


def _prompt_echo_reason(segment: dict[str, Any], prompt: str) -> str | None:
    """Неуверенное дословное эхо подсказки на музыке или тишине.

    Без VAD модель иногда дописывает «Русская устная речь» из hotwords.
    Удаляем только целую фразу из переданной подсказки, если ВСЕ её слова
    имеют вероятность ниже 0.2. Настоящие реплики и обычные неуверенные
    слова этим правилом не фильтруются; без меток уверенности не удаляем.
    """
    text = _normalise(segment.get("text") or "")
    normalised_prompt = _normalise(prompt)
    if len(text.split()) < 2 or f" {text} " not in f" {normalised_prompt} ":
        return None
    words = segment.get("words") or []
    if not words:
        return None
    # Все слова сегмента должны быть размечены, а не только его часть.
    if _normalise(" ".join(word.get("word") or "" for word in words)) != text:
        return None
    try:
        probabilities = [float(word["probability"]) for word in words]
    except (KeyError, TypeError, ValueError):
        return None
    if all(0 <= value < 0.2 for value in probabilities):
        return f"неуверенное эхо подсказки: {text[:70]}"
    return None


def clean_segments(
    segments: list[dict[str, Any]],
    *,
    prompt: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Сегменты без мусора и список удалённого.

    Удалённое возвращается, а не выбрасывается: оно уходит в JSON рядом с
    результатом, чтобы всегда можно было проверить, что именно сняли.
    """
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for segment in segments:
        reason = _prompt_echo_reason(segment, prompt) if prompt else None
        if reason:
            cleaned = None
        else:
            cleaned, reason = _clean_segment(segment)
        if reason:
            removed.append(
                {
                    "start": float(segment.get("start") or 0),
                    "end": float(segment.get("end") or 0),
                    "text": (segment.get("text") or "").strip(),
                    "reason": reason,
                }
            )
        if cleaned is not None:
            kept.append(cleaned)
    return kept, removed


def clean_text(text: str) -> tuple[str, list[str]]:
    """То же для готового текста без таймкодов — для диктовки."""
    cleaned, reason = _clean_segment({"text": text})
    reasons = [reason] if reason else []
    if cleaned is None:
        return "", reasons
    return cleaned["text"], reasons
