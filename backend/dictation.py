from __future__ import annotations

from difflib import SequenceMatcher
import re
import time
from dataclasses import dataclass
from typing import Any

from . import cancellation, gpu_turns, hallucinations, storage
from .cancellation import CancelledJob
from .transcriber import _load_model, punctuation_prompt


@dataclass(frozen=True)
class DictationCandidate:
    text: str
    language: str
    score: float
    words: int = 0
    repetition: float = 0.0
    tail_text: str = ""
    tail_overrun: float = 0.0
    tail_score: float | None = None


# Доля слов, ниже которой кандидат считается недорасшифровавшим запись.
# Разница между жадным и лучевым поиском на одном аудио обычно в пределах
# нескольких процентов; провал на 15% и больше означает потерю содержимого.
_COVERAGE_FLOOR = 0.85

# Диагностический порог для измерения явного цикла. Сам по себе он не делает
# кандидата непригодным: повторяемость может быть частью реальной речи.
_REPETITION_LIMIT = 0.35

# Два прохода используют разные декодеры, поэтому совсем малая разница
# avg_logprob сама по себе ещё не доказывает улучшение. Порог 0.015 пропускает
# измеренное улучшение на повторяющейся речи (+0.0195); отдельные length,
# agreement и tail-защиты по-прежнему отсекают недорасшифровку и галлюцинации.
_MIN_BEAM_SCORE_GAIN = 0.015

# Если два декодера услышали принципиально разный текст, метаданные Whisper
# не позволяют надёжно угадать правильный. В этом случае сохраняется baseline,
# который буквально повторяет быстрый режим.
_MIN_WORD_AGREEMENT = 0.75

# Конечный сегмент, который заметно вылез за реальную длительность и имеет
# сильно худшую уверенность, — характерный хвост вроде «Продолжение следует».
_TAIL_OVERRUN_SECONDS = 0.75
_TAIL_SCORE_DROP = 0.20

# Короткие фрагменты второй проход не меняет: на замерах до 8 секунд оба
# прохода давали посимвольно одинаковый текст, поэтому платить за него
# двойным временем незачем. Ноль отключает пропуск.
_SECOND_PASS_MIN_SECONDS = 8.0

# Хвост предыдущего текста. 300 символов укладываются в лимит Whisper на
# контекст (max_length // 2 - 1 = 223 токена); 600 символов давали 268
# токенов и вытесняли инструкцию о пунктуации.
_CONTEXT_TAIL_CHARS = 300


_FAST_PROFILE: dict[str, Any] = {
    "beam_size": 1,
    "best_of": 1,
    "patience": 1.0,
    "condition_on_previous_text": False,
    "temperature": 0.0,
    "vad_parameters": {"min_silence_duration_ms": 450},
    "compression_ratio_threshold": 2.4,
    "log_prob_threshold": -1.0,
    "no_speech_threshold": 0.7,
}

# Первый точный проход служит надёжной контрольной версией. Это защищает от
# случаев, когда широкий поиск звучит правдоподобно, но добавляет лишние слова.
# Контекст предыдущего записанного фрагмента всё равно передаётся через prompt.
_ACCURATE_BASELINE_PROFILE: dict[str, Any] = {**_FAST_PROFILE}

# Второй проход ищет альтернативу чуть шире и осторожнее сохраняет тихие
# начала/окончания. Луч 3 оказался лучшим балансом на GTX 1060: луч 8 заметно
# превысил допустимое время, но не дал устойчивого прироста качества.
_ACCURATE_BEAM_PROFILE: dict[str, Any] = {
    "beam_size": 3,
    "best_of": 3,
    "patience": 1.15,
    "condition_on_previous_text": True,
    "temperature": [0.0, 0.2, 0.4],
    "vad_parameters": {
        "threshold": 0.4,
        "min_speech_duration_ms": 80,
        "min_silence_duration_ms": 350,
        "speech_pad_ms": 500,
    },
    "compression_ratio_threshold": 2.5,
    "log_prob_threshold": -1.15,
    "no_speech_threshold": 0.6,
    # hallucination_silence_threshold сюда не добавляется намеренно:
    # faster-whisper обрабатывает его только внутри `if options.word_timestamps`
    # (transcribe.py), а мы просим word_timestamps=False, так что параметр был
    # бы мёртвым. Включение word_timestamps ради него стоило 1.44x времени на
    # длинном фрагменте и WER при этом остался 0.76 против 0.44 у первого
    # прохода — защиту от галлюцинаций даёт _select_candidate, а не он.
}


def _dictation_initial_prompt(language: str) -> str | None:
    # Текст живёт в transcriber.punctuation_prompt: та же инструкция теперь
    # нужна и транскрибации файлов, а две копии одной формулировки разошлись бы.
    return punctuation_prompt(language)


def _accurate_prompt(fragment: dict) -> str | None:
    instruction = _dictation_initial_prompt(fragment["language"])
    try:
        previous_text = storage.get_dictation_message(
            fragment["message_id"]
        )["text"].strip()
    except (KeyError, TypeError):
        previous_text = ""
    if not previous_text:
        return instruction

    # Whisper оставляет из контекста ПОСЛЕДНИЕ max_length // 2 - 1 токенов,
    # отрезая начало. Поэтому инструкция о пунктуации идёт после хвоста
    # предыдущего текста: иначе при продолжении диктовки она вылетает первой
    # и точный режим теряет форматирование, которое у быстрого есть всегда.
    previous_tail = previous_text[-_CONTEXT_TAIL_CHARS:]
    return f"Предыдущая часть этой же диктовки: {previous_tail}\n{instruction}"


def _candidate_score(segments: list[Any], text: str) -> float:
    if not segments or not text:
        return float("-inf")
    weighted_seconds = 0.0
    weighted_logprob = 0.0
    compression_penalty = 0.0
    for segment in segments:
        seconds = max(0.01, float(segment.end) - float(segment.start))
        weighted_seconds += seconds
        weighted_logprob += float(segment.avg_logprob) * seconds
        compression_penalty += max(
            0.0,
            float(getattr(segment, "compression_ratio", 0.0)) - 2.4,
        ) * seconds

    score = weighted_logprob / weighted_seconds
    score -= 0.12 * compression_penalty / weighted_seconds
    # Иногда широкий поиск вставляет нерусскую пунктуацию. Уверенность модели
    # при этом может выглядеть высокой, поэтому такой кандидат штрафуется.
    if re.search(r"[\u3000-\u303f\uff00-\uffef]", text):
        score -= 0.25
    return score


def _repetition_ratio(text: str) -> float:
    """Доля повторяющихся 4-грамм. Ловит зацикливание модели.

    avg_logprob у зациклившегося кандидата высокий: повторять уже сказанное
    модели легко. Поэтому одной уверенности для выбора недостаточно.
    """
    words = re.findall(r"\w+", text.lower())
    if len(words) < 8:
        return 0.0
    grams = [tuple(words[index:index + 4]) for index in range(len(words) - 3)]
    return 1.0 - len(set(grams)) / len(grams)


def _normalised_words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _word_agreement(left: str, right: str) -> float:
    left_words = _normalised_words(left)
    right_words = _normalised_words(right)
    if not left_words and not right_words:
        return 1.0
    if not left_words or not right_words:
        return 0.0
    return SequenceMatcher(
        None,
        left_words,
        right_words,
        autojunk=False,
    ).ratio()


def _net_added_words(baseline: str, beam: str) -> int:
    """Сколько слов beam добавил сверх удалённых относительно baseline."""
    inserted = 0
    deleted = 0
    matcher = SequenceMatcher(
        None,
        _normalised_words(baseline),
        _normalised_words(beam),
        autojunk=False,
    )
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            inserted += right_end - right_start
        if tag in {"delete", "replace"}:
            deleted += left_end - left_start
    return inserted - deleted


def _text_defect_count(text: str) -> int:
    """Явные артефакты декодирования, не требующие внешнего словаря."""
    defects = 0
    raw_words = re.findall(r"\w+", text)
    for word in raw_words:
        has_latin = bool(re.search(r"[A-Za-z]", word))
        has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", word))
        if has_latin and has_cyrillic:
            defects += 1

    normalised = [word.casefold() for word in raw_words]
    # Фрагмент вроде «п представить» возникает, когда декодер сначала выдаёт
    # первую букву, а затем повторяет полное слово. Обычные односимвольные
    # русские предлоги/союзы исключены из проверки.
    common_singletons = {"а", "б", "в", "ж", "и", "к", "о", "с", "у", "я"}
    for current, following in zip(normalised, normalised[1:]):
        if (
            len(current) == 1
            and current not in common_singletons
            and len(following) >= 4
            and following.startswith(current)
        ):
            defects += 1
    return defects


def _has_suspicious_beam_tail(
    baseline: DictationCandidate,
    beam: DictationCandidate,
) -> bool:
    tail_words = _normalised_words(beam.tail_text)
    if (
        len(tail_words) < 2
        or beam.tail_overrun <= _TAIL_OVERRUN_SECONDS
        or beam.tail_score is None
        or beam.tail_score > beam.score - _TAIL_SCORE_DROP
    ):
        return False

    baseline_words = _normalised_words(baseline.text)
    return baseline_words[-len(tail_words):] != tail_words


def _candidate_is_usable(candidate: DictationCandidate) -> bool:
    # Общая повторяемость не доказывает зацикливание модели: пользователь мог
    # действительно повторять формулировку, читать припев или перечисление.
    # Непригодным считается только пустой результат; сравнительные защиты от
    # нового beam-only цикла находятся в _select_candidate().
    return bool(candidate.text.strip())


def _unusable_candidate_error(candidate: DictationCandidate) -> str:
    return (
        "Whisper не распознал речь. Исходное аудио сохранено; "
        "фрагмент можно запустить повторно."
    )


def _select_candidate(
    baseline: DictationCandidate,
    beam: DictationCandidate,
) -> DictationCandidate:
    """Выбрать победителя из двух проходов.

    Baseline асимметрично считается безопасным якорем: его профиль и prompt
    совпадают с быстрым режимом. Beam заменяет его только когда оба текста
    согласуются по содержанию, отличаются по длине в разумных пределах и
    выигрыш уверенности больше измеренного шума между декодерами.
    """
    if not beam.text:
        return baseline
    if not baseline.text:
        return beam
    if baseline.text == beam.text:
        return baseline

    # 1. Длина — только защитный ограничитель. Более длинный beam не считается
    # автоматически «полнее»: именно так в текст попадали галлюцинации.
    if baseline.words:
        ratio = beam.words / baseline.words
        if ratio < _COVERAGE_FLOOR or ratio > 1.0 / _COVERAGE_FLOOR:
            return baseline

    # 2. Отдельный низкоуверенный хвост за концом аудио — галлюцинация.
    if _has_suspicious_beam_tail(baseline, beam):
        return baseline

    # 3. При сильном расхождении обычно угадывать по avg_logprob нельзя.
    # Узкое исключение — beam одновременно убрал объективный артефакт текста
    # и сам имеет измеримо лучшую уверенность. На repeated-RU baseline
    # заканчивался обрывком «П проверка», тогда как beam давал чистые фразы;
    # без этого исключения селектор сохранял вариант с худшим WER.
    agreement = _word_agreement(baseline.text, beam.text)
    baseline_defects = _text_defect_count(baseline.text)
    beam_defects = _text_defect_count(beam.text)
    if agreement < _MIN_WORD_AGREEMENT:
        if (
            beam_defects < baseline_defects
            and beam.score - baseline.score >= _MIN_BEAM_SCORE_GAIN
        ):
            return beam
        return baseline

    allowed_added = max(3, round(0.05 * max(1, baseline.words)))
    net_added = _net_added_words(baseline.text, beam.text)
    if net_added > allowed_added:
        return baseline

    # Глобальная repetition может быть реальной речью. Штрафуем только новый
    # цикл, который появился в более длинном beam: заметно больше повторных
    # 4-грамм плюс как минимум восемь добавленных слов.
    if (
        beam.words >= baseline.words
        and beam.repetition - baseline.repetition > 0.15
        and net_added >= 8
    ):
        return baseline

    # Исправление объективного артефакта (смешение латиницы и кириллицы
    # внутри слова, обрывок первой буквы перед полным словом) допускается даже
    # при небольшом ухудшении avg_logprob: на размеченной записи это снизило
    # WER с 3.57% до 2.98%, хотя score beam был хуже на 0.013.
    if beam_defects < baseline_defects and beam.score >= baseline.score - 0.03:
        return beam

    # 4. Малый выигрыш — шум разных профилей декодирования, а не доказанное
    # улучшение. При равенстве и сомнении всегда остаётся baseline.
    if beam.score - baseline.score >= _MIN_BEAM_SCORE_GAIN:
        return beam
    return baseline


def _transcribe_candidate(
    fragment_id: str,
    model: Any,
    fragment: dict,
    forced_language: str | None,
    prompt: str | None,
    profile: dict[str, Any],
) -> DictationCandidate:
    generator, info = model.transcribe(
        fragment["audio_path"],
        language=forced_language,
        task="transcribe",
        initial_prompt=prompt,
        vad_filter=True,
        word_timestamps=False,
        repetition_penalty=1.12,
        no_repeat_ngram_size=3,
        **profile,
    )
    segments = []
    parts: list[str] = []
    for segment in generator:
        cancellation.check(fragment_id)
        segments.append(segment)
        if segment.text.strip():
            parts.append(segment.text.strip())
    text = " ".join(parts).strip()
    tail_text = ""
    tail_overrun = 0.0
    tail_score: float | None = None
    text_segments = [segment for segment in segments if segment.text.strip()]
    if text_segments:
        last_segment = text_segments[-1]
        tail_text = last_segment.text.strip()
        audio_duration = float(
            getattr(info, "duration", 0.0)
            or fragment.get("duration_seconds")
            or 0.0
        )
        if audio_duration:
            tail_overrun = max(
                0.0,
                float(last_segment.end) - audio_duration,
            )
        tail_score = float(last_segment.avg_logprob)
    return DictationCandidate(
        text=text,
        language=info.language,
        score=_candidate_score(segments, text),
        words=len(re.findall(r"\w+", text)),
        repetition=_repetition_ratio(text),
        tail_text=tail_text,
        tail_overrun=tail_overrun,
        tail_score=tail_score,
    )


def transcribe_dictation_fragment(fragment_id: str) -> None:
    fragment = storage.get_dictation_fragment(fragment_id)
    started = time.perf_counter()
    storage.update_dictation_fragment(
        fragment_id,
        status="processing",
        started_at=storage.utc_now(),
        error=None,
    )
    try:
        cancellation.check(fragment_id)
        # Интерактивный слот видеокарты. Транскрибация файла уступит его на
        # ближайшем сегменте — около 2.5 с на GPU — вместо того, чтобы держать
        # диктовку до конца файла, как было раньше (до часа на реальных
        # записях). Подробности в backend/gpu_turns.py.
        with gpu_turns.interactive():
            model, _ = _load_model(fragment["language"])
            forced_language = (
                None if fragment["language"] == "auto" else fragment["language"]
            )
            if fragment["quality"] == "fast":
                selected = _transcribe_candidate(
                    fragment_id,
                    model,
                    fragment,
                    forced_language,
                    _dictation_initial_prompt(fragment["language"]),
                    _FAST_PROFILE,
                )
            else:
                # Первый точный проход обязан быть тем же безопасным
                # результатом, что и fast. Накопленный контекст получает только
                # уточняющий beam: так он может улучшить продолжение, но не
                # портит контрольную копию.
                selected = _transcribe_candidate(
                    fragment_id,
                    model,
                    fragment,
                    forced_language,
                    _dictation_initial_prompt(fragment["language"]),
                    _ACCURATE_BASELINE_PROFILE,
                )
                cancellation.check(fragment_id)
                duration = float(fragment.get("duration_seconds") or 0.0)
                run_second_pass = (
                    not _SECOND_PASS_MIN_SECONDS
                    or duration >= _SECOND_PASS_MIN_SECONDS
                    or not _candidate_is_usable(selected)
                )
                if run_second_pass:
                    try:
                        beam = _transcribe_candidate(
                            fragment_id,
                            model,
                            fragment,
                            forced_language,
                            _accurate_prompt(fragment),
                            _ACCURATE_BEAM_PROFILE,
                        )
                    except CancelledJob:
                        raise
                    except Exception:
                        # Второй проход — уточнение, а не обязательный этап.
                        # Его сбой не повод выбрасывать уже готовую расшифровку
                        # первого прохода и заставлять пользователя всё
                        # повторять.
                        if not _candidate_is_usable(selected):
                            raise
                        beam = None
                    if beam is not None:
                        selected = _select_candidate(selected, beam)

        if not _candidate_is_usable(selected):
            raise RuntimeError(_unusable_candidate_error(selected))
        # Между завершением второго прохода и записью результата пользователь
        # всё ещё может нажать «Остановить». Без этой проверки отменённый
        # фрагмент иногда успевал сохраниться как completed.
        # Диктовка страдает от галлюцинаций сильнее длинных файлов: фрагмент
        # короткий, и дописанная в тишине подпись занимает в нём заметную долю.
        # Пустой результат после чистки — честный ответ «речи не было»,
        # он лучше подписи автора субтитров в тексте сообщения.
        #
        # Чистка стоит до проверки отмены, а не после: проверка обязана быть
        # последним действием перед записью, это отдельно закреплено мутацией
        # в tests/test_dictation_mutations.py.
        cleaned_text, _ = hallucinations.clean_text(selected.text)
        cancellation.check(fragment_id)
        storage.complete_dictation_fragment(
            fragment_id,
            cleaned_text,
            selected.language,
            time.perf_counter() - started,
        )
    except CancelledJob:
        # Аудио фрагмента остаётся на диске, доступна кнопка «Повторить».
        storage.update_dictation_fragment(
            fragment_id,
            status="cancelled",
            error=None,
            processing_seconds=time.perf_counter() - started,
            finished_at=storage.utc_now(),
        )
    except Exception as exc:
        storage.update_dictation_fragment(
            fragment_id,
            status="failed",
            error=str(exc),
            processing_seconds=time.perf_counter() - started,
            finished_at=storage.utc_now(),
        )
