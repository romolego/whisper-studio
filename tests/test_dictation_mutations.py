"""Мутационная проверка: каждый фикс откатывается, тесты обязаны упасть.

Зелёный набор тестов сам по себе ничего не доказывает — он мог бы быть зелёным
и на сломанном коде. Здесь для каждого исправления из аудита исходник
dictation.py портится ровно в одном месте, и проверяется, что соответствующий
тест это ловит.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_dictation_mutations
"""
from __future__ import annotations

import importlib
import io
import sys
import types
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "dictation.py"
SOURCE = MODULE_PATH.read_text(encoding="utf-8")

# (имя дефекта, что заменить, на что, какие тесты обязаны упасть)
MUTATIONS = [
    (
        "селектор снова выбирает просто по avg_logprob",
        "    return max(baseline, beam, key=lambda candidate: candidate.score)",
        "    return max(baseline, beam, key=lambda candidate: candidate.score)\n"
        "    # мутация ниже недостижима",
        [],  # заменяется целиком ниже
    ),
]


def _mutate_select_body() -> str:
    """Откат _select_candidate к старому поведению: только по score."""
    start = SOURCE.index("def _select_candidate(")
    end = SOURCE.index("def _transcribe_candidate(")
    old_body = SOURCE[start:end]
    new_body = (
        "def _select_candidate(baseline, beam):\n"
        "    return max(baseline, beam, key=lambda candidate: candidate.score)\n\n\n"
    )
    return SOURCE.replace(old_body, new_body)


def _mutate_identical_profiles() -> str:
    return SOURCE.replace(
        '_ACCURATE_BEAM_PROFILE: dict[str, Any] = {\n    "beam_size": 3,',
        '_ACCURATE_BEAM_PROFILE: dict[str, Any] = {\n    **_FAST_PROFILE,\n    "beam_size": 1,',
    )


def _mutate_dead_parameter_returns() -> str:
    return SOURCE.replace(
        '    "no_speech_threshold": 0.6,\n',
        '    "no_speech_threshold": 0.6,\n    "hallucination_silence_threshold": 2.0,\n',
        1,
    )


def _mutate_instruction_first() -> str:
    return SOURCE.replace(
        'return f"Предыдущая часть этой же диктовки: {previous_tail}\\n{instruction}"',
        'return f"{instruction}\\nПредыдущая часть этой же диктовки: {previous_tail}"',
    )


def _mutate_context_dropped() -> str:
    return SOURCE.replace(
        "    previous_tail = previous_text[-_CONTEXT_TAIL_CHARS:]\n"
        '    return f"Предыдущая часть этой же диктовки: {previous_tail}\\n{instruction}"',
        "    return instruction",
    )


def _mutate_long_context_tail() -> str:
    return SOURCE.replace("_CONTEXT_TAIL_CHARS = 300", "_CONTEXT_TAIL_CHARS = 600")


def _mutate_second_pass_not_isolated() -> str:
    return SOURCE.replace(
        "                    except CancelledJob:\n"
        "                        raise\n"
        "                    except Exception:",
        "                    except CancelledJob:\n"
        "                        raise\n"
        "                    except ZeroDivisionError:",
    )


def _mutate_longer_beam_wins() -> str:
    return SOURCE.replace(
        "    if baseline.words:\n"
        "        ratio = beam.words / baseline.words\n"
        "        if ratio < _COVERAGE_FLOOR or ratio > 1.0 / _COVERAGE_FLOOR:\n"
        "            return baseline",
        "    longer, shorter = (\n"
        "        (baseline, beam) if baseline.words >= beam.words\n"
        "        else (beam, baseline)\n"
        "    )\n"
        "    if longer.words and shorter.words < _COVERAGE_FLOOR * longer.words:\n"
        "        return longer",
    )


def _mutate_final_quality_guard_removed() -> str:
    return SOURCE.replace(
        "        if not _candidate_is_usable(selected):\n"
        "            raise RuntimeError(_unusable_candidate_error(selected))\n",
        "",
    )


def _mutate_short_bad_baseline_skips_recovery() -> str:
    return SOURCE.replace(
        "                    or duration >= _SECOND_PASS_MIN_SECONDS\n"
        "                    or not _candidate_is_usable(selected)\n",
        "                    or duration >= _SECOND_PASS_MIN_SECONDS\n",
    )


def _mutate_final_cancellation_check_removed() -> str:
    return SOURCE.replace(
        "        cancellation.check(fragment_id)\n"
        "        storage.complete_dictation_fragment(\n",
        "        storage.complete_dictation_fragment(\n",
        1,
    )


def _mutate_baseline_receives_previous_context() -> str:
    return SOURCE.replace(
        '                    _dictation_initial_prompt(fragment["language"]),\n'
        "                    _ACCURATE_BASELINE_PROFILE,",
        "                    _accurate_prompt(fragment),\n"
        "                    _ACCURATE_BASELINE_PROFILE,",
    )


def _mutate_structural_repair_removed() -> str:
    mutated = SOURCE.replace(
        "        if (\n"
        "            beam_defects < baseline_defects\n"
        "            and beam.score - baseline.score >= _MIN_BEAM_SCORE_GAIN\n"
        "        ):\n"
        "            return beam\n"
        "        return baseline\n",
        "        return baseline\n",
    )
    return mutated.replace(
        "    if beam_defects < baseline_defects and beam.score >= baseline.score - 0.03:\n"
        "        return beam\n",
        "",
    )


def _mutate_global_repetition_hard_reject() -> str:
    return SOURCE.replace(
        "    if baseline.text == beam.text:\n"
        "        return baseline\n",
        "    if baseline.text == beam.text:\n"
        "        return baseline\n"
        "    if beam.repetition > 0.35:\n"
        "        return baseline\n",
    )


CASES = [
    ("P0 селектор выбирает по одному avg_logprob", _mutate_select_body,
     ["test_under_transcribed_candidate_loses_despite_higher_score",
      "test_long_repetitive_beam_addition_does_not_win",
      "test_extreme_repetitive_length_difference_keeps_baseline"]),
    ("второй проход перестал отличаться от первого", _mutate_identical_profiles,
     ["test_second_pass_profile_actually_differs_from_first"]),
    ("вернулся мёртвый hallucination_silence_threshold",
     _mutate_dead_parameter_returns,
     ["test_beam_profile_has_no_parameter_faster_whisper_ignores"]),
    ("инструкция снова перед контекстом", _mutate_instruction_first,
     ["test_instruction_comes_after_context"]),
    ("контекст предыдущего фрагмента потерян", _mutate_context_dropped,
     ["test_previous_text_reaches_the_prompt",
      "test_baseline_matches_fast_prompt_and_beam_receives_context",
      "test_instruction_comes_after_context"]),
    ("хвост контекста снова 600 символов", _mutate_long_context_tail,
     ["test_prompt_fits_whisper_context_window"]),
    ("сбой второго прохода снова роняет фрагмент",
     _mutate_second_pass_not_isolated,
     ["test_second_pass_error_keeps_the_first_result"]),
    ("более длинный beam снова считается автоматически полнее",
     _mutate_longer_beam_wins,
     ["test_longer_beam_is_not_assumed_more_complete"]),
    ("пустой результат снова сохраняется как completed",
     _mutate_final_quality_guard_removed,
     ["test_fast_empty_result_marks_fragment_failed",
      "test_both_empty_passes_mark_fragment_failed"]),
    ("короткий пустой baseline снова не получает восстановительный проход",
     _mutate_short_bad_baseline_skips_recovery,
     ["test_short_fragment_retries_when_baseline_is_empty"]),
    ("отмена после второго прохода снова успевает сохранить completed",
     _mutate_final_cancellation_check_removed,
     ["test_cancel_after_second_pass_prevents_final_save"]),
    ("baseline точного режима снова получает накопленный контекст",
     _mutate_baseline_receives_previous_context,
     ["test_baseline_matches_fast_prompt_and_beam_receives_context"]),
    ("исправление смешанного алфавита снова проигрывает avg_logprob",
     _mutate_structural_repair_removed,
     ["test_beam_can_fix_obvious_text_artifact_with_slightly_worse_score",
      "test_low_agreement_structural_repair_selects_real_repeated_beam"]),
    ("глобальная повторяемость снова ошибочно отклоняет реальную речь",
     _mutate_global_repetition_hard_reject,
     ["test_global_repetition_does_not_reject_possible_real_speech"]),
]


def load_mutated(source: str) -> types.ModuleType:
    module = types.ModuleType("backend.dictation")
    module.__package__ = "backend"
    module.__file__ = str(MODULE_PATH)
    # dataclass ищет модуль в sys.modules прямо во время создания класса,
    # поэтому регистрируем его до exec, а не после.
    sys.modules["backend.dictation"] = module
    exec(compile(source, str(MODULE_PATH), "exec"), module.__dict__)
    # `from backend import dictation` берёт атрибут пакета, а не sys.modules,
    # поэтому без этой строки уже импортированные тесты продолжали бы видеть
    # исходный модуль и мутация проходила бы незамеченной.
    backend_package = sys.modules.get("backend")
    if backend_package is not None:
        backend_package.dictation = module
    return module


def run_named_tests(names: list[str]) -> dict[str, bool]:
    """Возвращает {имя теста: прошёл ли}."""
    import tests.test_dictation_quality as quality
    importlib.reload(quality)
    loader = unittest.TestLoader()
    outcome = {}
    for name in names:
        suite = unittest.TestSuite()
        for cls_name in dir(quality):
            cls = getattr(quality, cls_name)
            if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
                if name in loader.getTestCaseNames(cls):
                    suite.addTest(cls(name))
        with io.StringIO() as sink:
            result = unittest.TextTestRunner(verbosity=0, stream=sink).run(suite)
        outcome[name] = result.wasSuccessful() and result.testsRun > 0
    return outcome


class MutationTests(unittest.TestCase):
    """Каждый откат фикса обязан быть пойман хотя бы одним тестом."""

    def tearDown(self) -> None:
        sys.modules["backend.dictation"] = load_mutated(SOURCE)
        import tests.test_dictation_quality as quality
        importlib.reload(quality)

    def test_every_fix_is_covered_by_a_failing_test(self) -> None:
        report = []
        for label, mutate, expected_failures in CASES:
            mutated_source = mutate()
            self.assertNotEqual(SOURCE, mutated_source,
                                f"мутация не применилась: {label}")
            sys.modules["backend.dictation"] = load_mutated(mutated_source)
            outcome = run_named_tests(expected_failures)
            caught = [name for name, passed in outcome.items() if not passed]
            report.append((label, expected_failures, caught))

        print()
        print("=" * 78)
        print("МУТАЦИОННАЯ ПРОВЕРКА: откатываем фикс -> тест обязан упасть")
        print("=" * 78)
        undetected = []
        for label, expected, caught in report:
            status = "ПОЙМАНО" if caught else "НЕ ПОЙМАНО"
            print(f"\n  {label}")
            print(f"    ожидали падения : {', '.join(expected)}")
            print(f"    упали           : {', '.join(caught) if caught else '— ничего —'}")
            print(f"    итог            : {status}")
            if not caught:
                undetected.append(label)

        print()
        print(f"  покрыто мутаций: {len(CASES) - len(undetected)} из {len(CASES)}")
        self.assertEqual([], undetected,
                         "эти дефекты можно вернуть, и тесты этого не заметят")


if __name__ == "__main__":
    unittest.main()
