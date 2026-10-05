from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import dictation
from backend.cancellation import CancelledJob


def candidate(text: str, score: float, words: int | None = None,
              repetition: float | None = None,
              **metadata) -> dictation.DictationCandidate:
    return dictation.DictationCandidate(
        text=text,
        language="ru",
        score=score,
        words=len(text.split()) if words is None else words,
        repetition=(dictation._repetition_ratio(text)
                    if repetition is None else repetition),
        **metadata,
    )


class CandidateScoreTests(unittest.TestCase):
    def test_candidate_score_rejects_foreign_punctuation(self) -> None:
        normal = SimpleNamespace(start=0.0, end=5.0, avg_logprob=-0.2,
                                 compression_ratio=1.1)
        suspicious = SimpleNamespace(start=0.0, end=5.0, avg_logprob=-0.1,
                                     compression_ratio=1.1)
        self.assertGreater(
            dictation._candidate_score([normal], "Обычный текст."),
            dictation._candidate_score([suspicious], "Текст с чужой пунктуацией、"),
        )

    def test_empty_candidate_scores_worst(self) -> None:
        segment = SimpleNamespace(start=0.0, end=5.0, avg_logprob=-0.2,
                                  compression_ratio=1.1)
        self.assertEqual(float("-inf"), dictation._candidate_score([segment], ""))
        self.assertEqual(float("-inf"), dictation._candidate_score([], "текст"))


class RepetitionRatioTests(unittest.TestCase):
    def test_clean_text_is_not_flagged(self) -> None:
        text = ("Это короткая проверка локальной системы распознавания "
                "русской речи и ещё немного разного текста для длины.")
        self.assertLess(dictation._repetition_ratio(text),
                        dictation._REPETITION_LIMIT)

    def test_looping_text_is_flagged(self) -> None:
        text = "да я думаю что так " * 12
        self.assertGreater(dictation._repetition_ratio(text),
                           dictation._REPETITION_LIMIT)

    def test_short_text_is_never_flagged(self) -> None:
        self.assertEqual(0.0, dictation._repetition_ratio("да да да"))


class TextDefectTests(unittest.TestCase):
    def test_mixed_alphabet_inside_word_is_a_defect(self) -> None:
        self.assertGreater(dictation._text_defect_count("комpания"), 0)

    def test_normal_russian_text_has_no_defects(self) -> None:
        self.assertEqual(0, dictation._text_defect_count(
            "Компания предоставит результат в четверг."
        ))

    def test_stuttered_initial_before_full_word_is_a_defect(self) -> None:
        self.assertGreater(dictation._text_defect_count("п представить"), 0)


class CandidateSelectionTests(unittest.TestCase):
    """Регрессия на дефект P0, найденный замером на 140-секундной записи."""

    def test_under_transcribed_candidate_loses_despite_higher_score(self) -> None:
        # Реальные числа замера: эталон 200 слов.
        # baseline WER 0.440, beam WER 0.815 — но beam выигрывал по avg_logprob.
        baseline = candidate("слово " * 134, -0.3393, words=134, repetition=0.0)
        beam = candidate("слово " * 40, -0.2357, words=40, repetition=0.0)

        selected = dictation._select_candidate(baseline, beam)

        self.assertIs(baseline, selected,
                      "кандидат, потерявший 70% слов, не должен побеждать "
                      "только потому, что его avg_logprob выше")

    def test_confidence_decides_when_both_candidates_are_complete(self) -> None:
        baseline = candidate("мы обсуждали важный проект сегодня", -0.30,
                             words=100, repetition=0.0)
        beam = candidate("Мы обсуждали важный проект сегодня.", -0.20,
                         words=98, repetition=0.0)
        self.assertIs(beam, dictation._select_candidate(baseline, beam))

    def test_small_word_difference_does_not_trigger_coverage_rule(self) -> None:
        # 95% слов — нормальная разница между жадным и лучевым поиском
        baseline = candidate("мы обсуждали важный проект сегодня", -0.30,
                             words=100, repetition=0.0)
        beam = candidate("Мы обсуждали важный проект сегодня.", -0.20,
                         words=95, repetition=0.0)
        self.assertIs(beam, dictation._select_candidate(baseline, beam))

    def test_longer_beam_is_not_assumed_more_complete(self) -> None:
        """Лишние правдоподобные слова не должны выигрывать одной длиной."""
        baseline = candidate(
            "Надёжный первый вариант.", -0.35,
            words=100, repetition=0.0,
        )
        hallucinated_beam = candidate(
            "Надёжный первый вариант и длинное выдуманное продолжение.", -0.10,
            words=125, repetition=0.0,
        )

        self.assertIs(
            baseline,
            dictation._select_candidate(baseline, hallucinated_beam),
        )

    def test_small_score_gain_does_not_replace_safe_baseline(self) -> None:
        """Разные декодеры нельзя сравнивать по малой разнице avg_logprob."""
        baseline = candidate("Мы обсудили новый проект сегодня.", -0.30,
                             words=100, repetition=0.0)
        beam = candidate("Мы обсудили новый проект сегодня!", -0.29,
                         words=100, repetition=0.0)
        self.assertIs(baseline,
                      dictation._select_candidate(baseline, beam))

    def test_strongly_divergent_beam_falls_back_to_baseline(self) -> None:
        baseline = candidate(
            "Сегодня мы обсудили сроки поставки оборудования.", -0.30,
            words=100, repetition=0.0,
        )
        divergent = candidate(
            "Совершенно другая правдоподобная история про отпуск.", -0.10,
            words=100, repetition=0.0,
        )
        self.assertIs(baseline,
                      dictation._select_candidate(baseline, divergent))

    def test_low_confidence_tail_past_audio_end_is_rejected(self) -> None:
        baseline = candidate(
            "Основной текст закончился здесь.", -0.07,
            words=100, repetition=0.0,
        )
        beam = candidate(
            "Основной текст закончился здесь. Продолжение следует.", -0.04,
            words=104, repetition=0.0,
            tail_text="Продолжение следует.",
            tail_overrun=1.29,
            tail_score=-0.53,
        )
        self.assertIs(baseline,
                      dictation._select_candidate(baseline, beam))

    def test_small_timestamp_overrun_is_not_a_tail_hallucination(self) -> None:
        baseline = candidate(
            "Мы закончили обсуждение сегодня", -0.20,
            words=100, repetition=0.0,
        )
        beam = candidate(
            "Мы закончили обсуждение сегодня.", -0.10,
            words=101, repetition=0.0,
            tail_text="обсуждение сегодня.",
            tail_overrun=0.20,
            tail_score=-0.50,
        )
        self.assertIs(beam, dictation._select_candidate(baseline, beam))

    def test_beam_can_fix_obvious_text_artifact_with_slightly_worse_score(
        self,
    ) -> None:
        baseline = candidate(
            "Нужно сравнить комpанию и п представить результат.", -0.100,
            words=100, repetition=0.0,
        )
        beam = candidate(
            "Нужно сравнить компанию и представить результат.", -0.113,
            words=98, repetition=0.0,
        )
        self.assertIs(beam, dictation._select_candidate(baseline, beam))

    def test_long_repetitive_beam_addition_does_not_win(self) -> None:
        clean = candidate("нормальный разборчивый текст без повторов вообще",
                          -0.40, words=40, repetition=0.0)
        looping = candidate("да я думаю что так " * 12, -0.10, words=60,
                            repetition=0.9)
        self.assertIs(clean, dictation._select_candidate(clean, looping))

    def test_extreme_repetitive_length_difference_keeps_baseline(self) -> None:
        """Сильно раздутый повтором beam не считается более полным."""
        short_clean = candidate("короткий чистый текст", -0.40, words=20,
                                repetition=0.0)
        long_looping = candidate("повтор " * 100, -0.10, words=100,
                                 repetition=0.95)
        self.assertIs(short_clean,
                      dictation._select_candidate(short_clean, long_looping))

    def test_global_repetition_does_not_reject_possible_real_speech(self) -> None:
        """Реальные числа repeated-RU: beam имел немного лучший WER."""
        baseline = candidate(
            ("да я думаю что так " * 11) + "да я думаю иначе",
            -0.2341, words=59, repetition=0.2857,
        )
        beam = candidate(
            ("да я думаю что так " * 10) + "да я думаю немного иначе",
            -0.2146, words=51, repetition=0.7083,
        )
        self.assertIs(beam, dictation._select_candidate(baseline, beam))

    def test_low_agreement_structural_repair_selects_real_repeated_beam(self) -> None:
        """Фактические тексты repeated-RU: чистый beam не теряется из-за agreement."""
        baseline = candidate(
            "Это короткая проверка локальной системы распознавания русской речи. "
            "Это короткая проверка локальной системы распознавания русской речи. "
            "Проверка локальной системы распознавания русской речи. распознавание "
            "русской речи. Это короткая проверка локальной системы распознавания "
            "русской речью. Это Короткая Проверка Локальной Системы Распознавать "
            "Русской Речи Это Короркая Поверка логальной системой распознования "
            "русско-речи Это Корорткая П проверка ЛОКАЛЬНОЙ СИСТЕМЫ "
            "Расповзнавания Русско и Рече",
            -0.2341, words=59, repetition=0.2857,
        )
        beam = candidate(
            "Это короткая проверка локальной системы распознавания русской речи. "
            "проверка локальной системы распознавания русской речи. локальной "
            "системы распознавания русской речи. Это короткая проверка локальной "
            "системы распознавания русской речи. Это короткая проверка локальной "
            "системы распознавания русской речи. Это короткая проверка локальной "
            "системы распознавания русской речи. Это короткая проверка локальной "
            "системы распознавания русской речи.",
            -0.2146, words=51, repetition=0.7083,
        )
        self.assertLess(
            dictation._word_agreement(baseline.text, beam.text),
            dictation._MIN_WORD_AGREEMENT,
        )
        self.assertGreater(
            dictation._text_defect_count(baseline.text),
            dictation._text_defect_count(beam.text),
        )
        self.assertIs(beam, dictation._select_candidate(baseline, beam))

    def test_empty_beam_falls_back_to_baseline(self) -> None:
        baseline = candidate("нормальный текст", -0.9, words=2, repetition=0.0)
        empty = dictation.DictationCandidate("", "ru", float("-inf"), 0, 0.0)
        self.assertIs(baseline, dictation._select_candidate(baseline, empty))

    def test_empty_baseline_yields_beam(self) -> None:
        empty = dictation.DictationCandidate("", "ru", float("-inf"), 0, 0.0)
        beam = candidate("нормальный текст", -0.9, words=2, repetition=0.0)
        self.assertIs(beam, dictation._select_candidate(empty, beam))

    def test_tie_prefers_baseline(self) -> None:
        baseline = candidate("вариант первый", -0.2, words=2, repetition=0.0)
        beam = candidate("вариант второй", -0.2, words=2, repetition=0.0)
        self.assertIs(baseline, dictation._select_candidate(baseline, beam))


class ProfileTests(unittest.TestCase):
    def test_second_pass_profile_actually_differs_from_first(self) -> None:
        """Ловит подмену, при которой второй проход перестаёт быть вторым."""
        self.assertNotEqual(dictation._ACCURATE_BASELINE_PROFILE,
                            dictation._ACCURATE_BEAM_PROFILE)
        self.assertGreater(dictation._ACCURATE_BEAM_PROFILE["beam_size"],
                           dictation._ACCURATE_BASELINE_PROFILE["beam_size"])
        self.assertTrue(
            dictation._ACCURATE_BEAM_PROFILE["condition_on_previous_text"])
        self.assertFalse(
            dictation._ACCURATE_BASELINE_PROFILE["condition_on_previous_text"])

    def test_beam_profile_has_no_parameter_faster_whisper_ignores(self) -> None:
        """word_timestamps=False, поэтому hallucination_silence_threshold мёртв."""
        self.assertNotIn("hallucination_silence_threshold",
                         dictation._ACCURATE_BEAM_PROFILE)

    def test_fast_profile_is_greedy(self) -> None:
        self.assertEqual(1, dictation._FAST_PROFILE["beam_size"])
        self.assertEqual(0.0, dictation._FAST_PROFILE["temperature"])


class AccuratePromptTests(unittest.TestCase):
    FRAGMENT = {"language": "ru", "message_id": "m1"}

    def _prompt_with_previous(self, previous: str) -> str:
        with patch.object(dictation.storage, "get_dictation_message",
                          return_value={"text": previous}):
            return dictation._accurate_prompt(self.FRAGMENT)

    def test_previous_text_reaches_the_prompt(self) -> None:
        prompt = self._prompt_with_previous("Обсуждали сроки поставки.")
        self.assertIn("сроки поставки", prompt)

    def test_instruction_comes_after_context(self) -> None:
        """Whisper режет контекст с начала — инструкция обязана быть в конце."""
        prompt = self._prompt_with_previous("Обсуждали сроки поставки.")
        instruction = dictation._dictation_initial_prompt("ru")
        self.assertTrue(prompt.endswith(instruction),
                        "инструкция о пунктуации должна стоять последней, "
                        "иначе она вылетает при обрезке контекста")
        self.assertLess(prompt.index("сроки поставки"), prompt.index(instruction))

    def test_context_tail_is_capped(self) -> None:
        previous = "слово " * 500
        prompt = self._prompt_with_previous(previous)
        carried = prompt.split("\n")[0]
        self.assertLessEqual(len(carried),
                             dictation._CONTEXT_TAIL_CHARS + 60)

    def test_prompt_fits_whisper_context_window(self) -> None:
        """Реальная токенизация: промпт должен влезать в max_length//2 - 1."""
        tokenizer_path = (Path(__file__).resolve().parents[1] / "models"
                          / "turbo-russian" / "tokenizer.json")
        if not tokenizer_path.exists():
            self.skipTest("модель не установлена")
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        prompt = self._prompt_with_previous("Мы обсуждали сроки поставки "
                                            "оборудования и бюджет. " * 20)
        tokens = tokenizer.encode(" " + prompt.strip()).ids
        self.assertLessEqual(len(tokens), 448 // 2 - 1,
                             f"промпт из {len(tokens)} токенов будет обрезан")

    def test_no_previous_text_gives_plain_instruction(self) -> None:
        self.assertEqual(dictation._dictation_initial_prompt("ru"),
                         self._prompt_with_previous(""))


class DictationRunTests(unittest.TestCase):
    """Поведение transcribe_dictation_fragment целиком."""

    def _run(self, fragment: dict, side_effect):
        completed, updated = [], []
        with patch.object(dictation.storage, "get_dictation_fragment",
                          return_value=fragment), \
             patch.object(dictation.storage, "get_dictation_message",
                          return_value={"text": "Предыдущая часть."}), \
             patch.object(dictation.storage, "update_dictation_fragment",
                          side_effect=lambda fid, **kw: updated.append(kw)), \
             patch.object(dictation.storage, "complete_dictation_fragment",
                          side_effect=lambda *a, **kw: completed.append(a)), \
             patch.object(dictation, "_load_model",
                          return_value=(Mock(), "cuda")), \
             patch.object(dictation, "_transcribe_candidate",
                          side_effect=side_effect) as transcribe:
            dictation.transcribe_dictation_fragment(fragment["id"])
        return transcribe, completed, updated

    @staticmethod
    def _fragment(quality="accurate", duration=30.0):
        return {"id": "f1", "message_id": "m1", "audio_path": "a.webm",
                "language": "ru", "quality": quality,
                "duration_seconds": duration}

    def test_fast_mode_remains_single_pass(self) -> None:
        transcribe, completed, _ = self._run(
            self._fragment(quality="fast"),
            lambda *a, **k: candidate("Быстрый вариант.", -0.2))
        transcribe.assert_called_once()
        self.assertIs(dictation._FAST_PROFILE,
                      transcribe.call_args.args[-1])
        self.assertEqual(1, len(completed))

    def test_accurate_mode_runs_two_passes_with_different_profiles(self) -> None:
        results = [candidate("мы обсуждали важный проект сегодня", -0.3),
                   candidate("Мы обсуждали важный проект сегодня.", -0.1)]
        transcribe, completed, _ = self._run(
            self._fragment(), lambda *a, **k: results.pop(0))
        self.assertEqual(2, transcribe.call_count)
        used = [call.args[-1] for call in transcribe.call_args_list]
        self.assertIs(dictation._ACCURATE_BASELINE_PROFILE, used[0])
        self.assertIs(dictation._ACCURATE_BEAM_PROFILE, used[1])
        self.assertEqual("Мы обсуждали важный проект сегодня.", completed[0][1])

    def test_baseline_matches_fast_prompt_and_beam_receives_context(self) -> None:
        """Первый проход — точная копия fast, контекст получает только beam."""
        results = [candidate("Один.", -0.3), candidate("Два.", -0.1)]
        transcribe, _, _ = self._run(
            self._fragment(), lambda *a, **k: results.pop(0))
        prompts = [call.args[4] for call in transcribe.call_args_list]
        self.assertEqual(dictation._dictation_initial_prompt("ru"), prompts[0])
        self.assertNotIn("Предыдущая часть.", prompts[0])
        self.assertIn("Предыдущая часть.", prompts[1])

    def test_short_fragment_skips_the_second_pass(self) -> None:
        transcribe, completed, _ = self._run(
            self._fragment(duration=dictation._SECOND_PASS_MIN_SECONDS - 1),
            lambda *a, **k: candidate("Короткий.", -0.2))
        transcribe.assert_called_once()
        self.assertEqual("Короткий.", completed[0][1])

    def test_long_fragment_runs_the_second_pass(self) -> None:
        results = [candidate("Один.", -0.3), candidate("Два.", -0.1)]
        transcribe, _, _ = self._run(
            self._fragment(duration=dictation._SECOND_PASS_MIN_SECONDS + 1),
            lambda *a, **k: results.pop(0))
        self.assertEqual(2, transcribe.call_count)

    def test_second_pass_error_keeps_the_first_result(self) -> None:
        """Дефект P1: сбой уточнения не должен уничтожать готовую расшифровку."""
        calls = []

        def side_effect(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return candidate("Хороший первый результат.", -0.15)
            raise RuntimeError("CUDA out of memory")

        _, completed, updated = self._run(self._fragment(), side_effect)
        self.assertEqual(2, len(calls))
        self.assertEqual(1, len(completed), "результат первого прохода потерян")
        self.assertEqual("Хороший первый результат.", completed[0][1])
        self.assertNotIn("failed",
                         [u.get("status") for u in updated])

    @staticmethod
    def _empty_candidate() -> dictation.DictationCandidate:
        return dictation.DictationCandidate(
            "", "ru", float("-inf"), 0, 0.0,
        )

    def test_fast_empty_result_marks_fragment_failed(self) -> None:
        _, completed, updated = self._run(
            self._fragment(quality="fast"),
            lambda *args, **kwargs: self._empty_candidate(),
        )
        self.assertEqual([], completed)
        self.assertIn("failed", [item.get("status") for item in updated])

    def test_empty_baseline_and_second_pass_error_marks_fragment_failed(
        self,
    ) -> None:
        calls = []

        def side_effect(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return self._empty_candidate()
            raise RuntimeError("CUDA out of memory")

        _, completed, updated = self._run(self._fragment(), side_effect)
        self.assertEqual([], completed)
        failures = [item for item in updated
                    if item.get("status") == "failed"]
        self.assertEqual(1, len(failures))
        self.assertIn("CUDA out of memory", failures[0]["error"])

    def test_both_empty_passes_mark_fragment_failed(self) -> None:
        _, completed, updated = self._run(
            self._fragment(),
            lambda *args, **kwargs: self._empty_candidate(),
        )
        self.assertEqual([], completed)
        self.assertIn("failed", [item.get("status") for item in updated])

    def test_repeated_nonempty_passes_can_be_saved(self) -> None:
        results = [
            candidate("да я думаю что так " * 12, -0.40,
                      words=60, repetition=0.70),
            candidate(("да я думаю что так " * 11) + "да я думаю иначе",
                      -0.38, words=59, repetition=0.68),
        ]
        _, completed, updated = self._run(
            self._fragment(),
            lambda *args, **kwargs: results.pop(0),
        )
        self.assertEqual(1, len(completed))
        self.assertNotIn("failed", [item.get("status") for item in updated])

    def test_fast_repeated_nonempty_result_is_not_marked_failed(self) -> None:
        repeated = candidate("да я думаю что так " * 12, -0.40,
                             words=60, repetition=0.70)
        _, completed, updated = self._run(
            self._fragment(quality="fast"),
            lambda *args, **kwargs: repeated,
        )
        self.assertEqual(1, len(completed))
        self.assertNotIn("failed", [item.get("status") for item in updated])

    def test_short_fragment_retries_when_baseline_is_empty(self) -> None:
        results = [
            self._empty_candidate(),
            candidate("Восстановленный второй результат.", -0.20),
        ]
        transcribe, completed, _ = self._run(
            self._fragment(
                duration=dictation._SECOND_PASS_MIN_SECONDS - 1,
            ),
            lambda *args, **kwargs: results.pop(0),
        )
        self.assertEqual(2, transcribe.call_count)
        self.assertEqual("Восстановленный второй результат.",
                         completed[0][1])

    def test_cancel_after_second_pass_prevents_final_save(self) -> None:
        pass_number = 0
        second_pass_finished = False

        def transcribe_side_effect(*args, **kwargs):
            nonlocal pass_number, second_pass_finished
            pass_number += 1
            if pass_number == 1:
                return candidate("Первый вариант.", -0.30)
            second_pass_finished = True
            return candidate("Второй вариант.", -0.20)

        def cancellation_check(fragment_id: str) -> None:
            if second_pass_finished:
                raise CancelledJob(fragment_id)

        with patch.object(dictation.cancellation, "check",
                          side_effect=cancellation_check):
            _, completed, updated = self._run(
                self._fragment(), transcribe_side_effect)

        self.assertEqual([], completed)
        self.assertIn("cancelled", [item.get("status") for item in updated])

    def test_cancel_in_first_pass_stops_immediately(self) -> None:
        calls = []

        def side_effect(*args, **kwargs):
            calls.append(1)
            raise CancelledJob("f1")

        _, completed, updated = self._run(self._fragment(), side_effect)
        self.assertEqual(1, len(calls))
        self.assertEqual([], completed)
        self.assertIn("cancelled", [u.get("status") for u in updated])

    def test_cancel_in_second_pass_is_not_swallowed_as_error(self) -> None:
        """Отмена во втором проходе остаётся отменой, а не превращается в успех."""
        calls = []

        def side_effect(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return candidate("Первый результат.", -0.15)
            raise CancelledJob("f1")

        _, completed, updated = self._run(self._fragment(), side_effect)
        self.assertEqual(2, len(calls))
        self.assertEqual([], completed)
        self.assertIn("cancelled", [u.get("status") for u in updated])


if __name__ == "__main__":
    unittest.main()
