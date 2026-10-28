"""Отсев галлюцинаций Whisper: что обязано удаляться и что обязано выжить.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_hallucinations

Негативная часть здесь важнее позитивной. Удалить лишнее хуже, чем оставить
мусор: мусор видно глазами, а потерянную реплику восстановить нельзя.
"""
from __future__ import annotations

import unittest

from backend import hallucinations


def _segment(text: str, start: float = 0.0, end: float = 2.0) -> dict:
    return {"start": start, "end": end, "text": text, "words": None}


class CreditsTests(unittest.TestCase):
    """Первый уровень: подписи авторов субтитров."""

    # Варианты собраны из датасета sachaarbonel/whisper-hallucinations,
    # гиста waveletdeboshir и найденного в data/sessions.
    JUNK = [
        "Субтитры сделал DimaTorzok",
        "Субтитры делал DimaTorzok",
        "Субтитры добавил DimaTorzok",
        "Субтитры создавал DimaTorzok",
        "Субтитры подогнал «Симон»!",
        "Спасибо за субтитры!",
        "Спасибо за субтитры Алексею Дубровскому!",
        "Редактор субтитров А.Синецкая Корректор А.Егорова",
        "Редактор субтитров М.Лосева Корректор А.Егорова",
        "Редактор субтитров Т.Горелова Корректор А.Кулакова",
        "Корректор А.Егорова",
        "Субтитры и перевод: DimaTorzok",
        "Перевод и субтитры DimaTorzok",
        "Subtitles by the Amara.org community",
        "Subs by www.zeoranger.co.uk",
        "Transcription by CastingWords",
        "Captions by NicoSubs",
        "Closed captioning provided by Muhsen",
        "Translated by Amara.org Community",
        "Satsang with Mooji",
        "Altyazı M.K.",
        "Titulky vytvořil JohnyX",
    ]

    def test_junk_removed(self) -> None:
        for text in self.JUNK:
            with self.subTest(text=text):
                kept, removed = hallucinations.clean_segments([_segment(text)])
                self.assertEqual(kept, [], f"не удалено: {text!r}")
                self.assertEqual(len(removed), 1)
                self.assertTrue(removed[0]["reason"])

    def test_removed_record_keeps_evidence(self) -> None:
        """Удалённое возвращается целиком: должно быть видно, что сняли."""
        kept, removed = hallucinations.clean_segments(
            [_segment("Субтитры делал DimaTorzok", 24111.6, 24112.18)]
        )
        self.assertEqual(kept, [])
        self.assertEqual(removed[0]["text"], "Субтитры делал DimaTorzok")
        self.assertAlmostEqual(removed[0]["start"], 24111.6)
        self.assertAlmostEqual(removed[0]["end"], 24112.18)


class GluedTailTests(unittest.TestCase):
    """Подпись, приклеенная хвостом к настоящей фразе."""

    def test_tail_cut_and_speech_kept(self) -> None:
        kept, removed = hallucinations.clean_segments(
            [_segment("Всем спасибо за работу. Субтитры сделал DimaTorzok")]
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["text"], "Всем спасибо за работу.")
        self.assertEqual(len(removed), 1)

    def test_words_and_end_follow_the_cut(self) -> None:
        segment = {
            "start": 10.0,
            "end": 12.0,
            "text": " Хорошо. Субтитры делал DimaTorzok",
            "words": [
                {"start": 10.0, "end": 10.4, "word": " Хорошо."},
                {"start": 11.0, "end": 11.3, "word": " Субтитры"},
                {"start": 11.3, "end": 11.6, "word": " делал"},
                {"start": 11.6, "end": 12.0, "word": " DimaTorzok"},
            ],
        }
        kept, _ = hallucinations.clean_segments([segment])
        self.assertEqual(kept[0]["text"], " Хорошо.")
        self.assertEqual([w["word"] for w in kept[0]["words"]], [" Хорошо."])
        self.assertAlmostEqual(kept[0]["end"], 10.4)

    def test_split_name_words_all_removed(self) -> None:
        """Whisper иногда режет DimaTorzok на куски — уйти должны все."""
        segment = {
            "start": 0.0,
            "end": 3.0,
            "text": " Готово. Субтитры сделал DimaTorzok",
            "words": [
                {"start": 0.0, "end": 0.5, "word": " Готово."},
                {"start": 1.0, "end": 1.3, "word": " Субтитры"},
                {"start": 1.3, "end": 1.6, "word": " сделал"},
                {"start": 1.6, "end": 2.3, "word": " Dima"},
                {"start": 2.3, "end": 3.0, "word": "Torzok"},
            ],
        }
        kept, _ = hallucinations.clean_segments([segment])
        self.assertEqual([w["word"] for w in kept[0]["words"]], [" Готово."])

    def test_original_segment_not_mutated(self) -> None:
        segment = _segment("Ага. Субтитры сделал DimaTorzok")
        hallucinations.clean_segments([segment])
        self.assertEqual(segment["text"], "Ага. Субтитры сделал DimaTorzok")


class SoundTagTests(unittest.TestCase):
    """Теги звука вместо речи."""

    JUNK = [
        "ВЕСЕЛАЯ МУЗЫКА",
        "СПОКОЙНАЯ МУЗЫКА",
        "ТРЕВОЖНАЯ МУЗЫКА",
        "МУЗЫКАЛЬНАЯ ЗАСТАВКА",
        "АПЛОДИСМЕНТЫ",
        "СМЕХ",
        "ГУДОК ПОЕЗДА",
        "ЛАЙ СОБАКИ",
        "ПОЛИЦЕЙСКАЯ СИРЕНА",
        "ААААААААААААА",
        "[музыка]",
        "(аплодисменты)",
        "[BLANK_AUDIO]",
        "[Music]",
        "♪♪♪",
        "***",
    ]

    def test_tags_removed(self) -> None:
        for text in self.JUNK:
            with self.subTest(text=text):
                kept, removed = hallucinations.clean_segments([_segment(text)])
                self.assertEqual(kept, [], f"не удалено: {text!r}")
                self.assertEqual(len(removed), 1)


class WholeSegmentTests(unittest.TestCase):
    """Второй уровень: только когда это весь сегмент."""

    def test_removed_alone(self) -> None:
        for text in (
            "Продолжение следует...",
            "Смотрите продолжение в следующей части.",
            "Смотрите продолжение во второй части видео.",
            "Субтитры",
            "Титры",
            "To be continued",
        ):
            with self.subTest(text=text):
                kept, _ = hallucinations.clean_segments([_segment(text)])
                self.assertEqual(kept, [], f"не удалено: {text!r}")

    def test_kept_inside_a_sentence(self) -> None:
        """Внутри живой фразы второй уровень не трогает ничего."""
        text = "Мы решили, что продолжение следует делать в том же формате."
        kept, removed = hallucinations.clean_segments([_segment(text)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["text"], text)
        self.assertEqual(removed, [])


class RealSpeechSurvivesTests(unittest.TestCase):
    """Негативная часть: живая речь обязана дойти до результата целой.

    Фразы взяты из настоящих расшифровок пользователя и из типового набора
    того, что реально звучит в докладах и на YouTube.
    """

    KEEP = [
        # доклады: так заканчивается каждое второе выступление
        "Спасибо за внимание!",
        "Спасибо за просмотр.",
        "Всем спасибо, до встречи!",
        # YouTube-материал, который сервис расшифровывает как контент
        "Подписывайтесь на канал, ставьте лайки.",
        "Не забудьте подписаться на наш канал.",
        "Поехали!",
        # речь про сами субтитры и расшифровки — законный разговор
        "Мы включили субтитры на всех видео.",
        "Субтитры можно скачать отдельным файлом.",
        "Он работает редактором субтитров уже пять лет.",
        "Корректор нашёл три опечатки.",
        "Здесь нужен хороший корректор и редактор.",
        # реальные сегменты из data/sessions
        "Ну что же делать?",
        "Спасибо.",
        "Да?",
        "Вот прямо я так говорю.",
        "Но мы сейчас тоже немножко про это поговорим.",
        # аббревиатуры капсом не должны считаться тегом звука
        "ИИ",
        "США",
    ]

    def test_speech_kept(self) -> None:
        for text in self.KEEP:
            with self.subTest(text=text):
                kept, removed = hallucinations.clean_segments([_segment(text)])
                self.assertEqual(len(kept), 1, f"удалено зря: {text!r}")
                self.assertEqual(kept[0]["text"], text)
                self.assertEqual(removed, [], f"удалено зря: {text!r}")

    def test_ambiguous_outro_disabled_by_default(self) -> None:
        self.assertFalse(hallucinations.REMOVE_AMBIGUOUS_OUTRO)

    def test_ambiguous_outro_works_when_enabled(self) -> None:
        """Флаг существует и действительно включает третий уровень."""
        original = hallucinations.REMOVE_AMBIGUOUS_OUTRO
        hallucinations.REMOVE_AMBIGUOUS_OUTRO = True
        try:
            kept, removed = hallucinations.clean_segments(
                [_segment("Спасибо за просмотр!")]
            )
            self.assertEqual(kept, [])
            self.assertEqual(len(removed), 1)
        finally:
            hallucinations.REMOVE_AMBIGUOUS_OUTRO = original


class PromptEchoTests(unittest.TestCase):
    PROMPT = "Русская устная речь. Оформляй результат как обычный русский текст."

    def segment(self, text: str = "Русская устная речь.", probability=0.05) -> dict:
        result = _segment(text, 314.0, 315.0)
        result["words"] = [
            {"word": word, "probability": probability}
            for word in text.split()
        ]
        return result

    def test_low_confidence_echo_is_removed_and_recorded(self) -> None:
        source = self.segment()
        kept, removed = hallucinations.clean_segments([source], prompt=self.PROMPT)
        self.assertEqual(kept, [])
        self.assertEqual(removed[0]["text"], source["text"])
        self.assertEqual(removed[0]["start"], 314.0)
        self.assertIn("эхо подсказки", removed[0]["reason"])

    def test_real_speech_and_uncertain_lyrics_survive(self) -> None:
        sources = [self.segment(probability=0.9), self.segment("Пацаны вернулись.")]
        kept, removed = hallucinations.clean_segments(sources, prompt=self.PROMPT)
        self.assertEqual(kept, sources)
        self.assertEqual(removed, [])

    def test_missing_or_partial_word_confidence_is_not_deleted(self) -> None:
        for words in (None, [{"word": "Русская"}], [{"word": "Русская", "probability": 0.01}]):
            source = self.segment()
            source["words"] = words
            kept, removed = hallucinations.clean_segments([source], prompt=self.PROMPT)
            self.assertEqual(kept, [source])
            self.assertEqual(removed, [])

    def test_echo_rule_requires_the_actual_prompt(self) -> None:
        source = self.segment()
        kept, removed = hallucinations.clean_segments([source])
        self.assertEqual(kept, [source])
        self.assertEqual(removed, [])

    def test_word_boundaries_and_single_words_are_preserved(self) -> None:
        sources = [self.segment("Речь."), self.segment("Русская устная речь нужна.")]
        kept, removed = hallucinations.clean_segments(sources, prompt=self.PROMPT)
        self.assertEqual(kept, sources)
        self.assertEqual(removed, [])

    def test_repeated_prompt_echo_requires_verified_silence(self) -> None:
        source = self.segment("Русская устная речь. Русская устная речь.", probability=0.7)
        for silent in (None, lambda segment: False):
            kept, removed = hallucinations.clean_segments([source], prompt=self.PROMPT, is_silent=silent)
            self.assertEqual(kept, [source])
            self.assertEqual(removed, [])
        kept, removed = hallucinations.clean_segments([source], prompt=self.PROMPT, is_silent=lambda segment: True)
        self.assertEqual(kept, [])
        self.assertIn("цифровой тишине", removed[0]["reason"])

    def test_silence_does_not_delete_unrelated_or_partially_matching_text(self) -> None:
        sources = [self.segment("Русская устная речь нужна. Продолжаем.", probability=.7),
                   self.segment("Настоящий припев. Настоящий припев.", probability=.7)]
        kept, removed = hallucinations.clean_segments(sources, prompt=self.PROMPT, is_silent=lambda segment: True)
        self.assertEqual(kept, sources)
        self.assertEqual(removed, [])


class DictationTests(unittest.TestCase):
    """Диктовка: то же самое, но на готовом тексте без таймкодов."""

    def test_tail_removed(self) -> None:
        text, reasons = hallucinations.clean_text(
            "Напомни завтра про договор. Субтитры сделал DimaTorzok"
        )
        self.assertEqual(text, "Напомни завтра про договор.")
        self.assertEqual(len(reasons), 1)

    def test_whole_message_removed(self) -> None:
        text, reasons = hallucinations.clean_text("Продолжение следует...")
        self.assertEqual(text, "")
        self.assertEqual(len(reasons), 1)

    def test_clean_text_untouched(self) -> None:
        source = "Позвонить в бухгалтерию и уточнить сумму."
        text, reasons = hallucinations.clean_text(source)
        self.assertEqual(text, source)
        self.assertEqual(reasons, [])


if __name__ == "__main__":
    unittest.main()
