from __future__ import annotations

import random
import unittest
from unittest.mock import patch

from backend import diarization, speaker_samples, storage, transcriber


class TurnIndexTests(unittest.TestCase):
    def test_index_gives_same_speakers_as_full_scan(self) -> None:
        """Индекс — только ускорение: итог обязан совпасть с полным перебором.

        Включая ничьи по перекрытию: при равном перекрытии выигрывает реплика,
        что раньше стоит в списке, а не раньше начинается.
        """
        rng = random.Random(7)
        turns = []
        for _ in range(400):
            start = rng.uniform(0, 3600)
            turns.append((start, start + rng.uniform(0.05, 240), f"S{rng.randint(0, 7)}"))
        # Дубли с другим спикером — ровно та ничья, где важен порядок.
        turns += [(start, end, "DUP") for start, end, _ in turns[:40]]
        rng.shuffle(turns)
        index = diarization._TurnIndex(turns)
        for _ in range(3000):
            start = rng.uniform(-5, 3700)
            end = start + rng.choice([0.0, rng.uniform(0, 3), rng.uniform(0, 60)])
            self.assertEqual(
                diarization._speaker_at(start, end, index.near(start, end)),
                diarization._speaker_at(start, end, turns),
                (start, end),
            )

    def test_empty_turns(self) -> None:
        index = diarization._TurnIndex([])
        self.assertIsNone(diarization._speaker_at(1, 2, index.near(1, 2)))


def _segment(start: float, words: list[tuple[float, float, str]]) -> dict:
    return {
        "start": start,
        "end": words[-1][1],
        "text": " ".join("слово" for _ in words),
        "speaker": words[0][2],
        "words": [
            {"start": a, "end": b, "word": " слово", "speaker": speaker}
            for a, b, speaker in words
        ],
    }


class SpeakerSampleClipTests(unittest.TestCase):
    def test_longest_runs_of_that_speaker_in_time_order(self) -> None:
        segments = [
            # Короткая реплика — в образец не идёт, есть длиннее.
            _segment(0, [(0, 0.5, "A"), (0.6, 1.0, "A")]),
            # Длинная реплика A из нескольких слов с паузами меньше секунды.
            _segment(10, [(10, 12, "A"), (12.5, 15, "A"), (15.4, 18, "A")]),
            _segment(20, [(20, 30, "B")]),
            # Пауза больше секунды рвёт реплику на две.
            _segment(40, [(40, 43, "A"), (45, 47, "A")]),
        ]
        clips = speaker_samples.pick_clips(segments, "A")
        self.assertEqual(clips, [(10, 18), (40, 43), (45, 47)])
        self.assertEqual(speaker_samples.pick_clips(segments, "B"), [(20, 28)])

    def test_limits_on_count_and_total_length(self) -> None:
        segments = [
            _segment(start, [(start, start + 20, "A")])
            for start in range(0, 400, 40)
        ]
        clips = speaker_samples.pick_clips(segments, "A")
        self.assertLessEqual(len(clips), speaker_samples.MAX_CLIPS)
        self.assertLessEqual(
            sum(end - start for start, end in clips),
            speaker_samples.MAX_TOTAL_SECONDS + 1e-9,
        )
        self.assertTrue(
            all(
                end - start <= speaker_samples.MAX_CLIP_SECONDS
                for start, end in clips
            )
        )
        self.assertEqual(clips, sorted(clips))

    def test_only_short_runs_still_give_a_sample(self) -> None:
        segments = [_segment(5, [(5, 5.4, "C")])]
        self.assertEqual(speaker_samples.pick_clips(segments, "C"), [(5, 5.4)])
        self.assertEqual(speaker_samples.pick_clips(segments, "D"), [])

    def test_segment_without_words_uses_segment_bounds(self) -> None:
        segments = [{"start": 3, "end": 9, "text": "фраза", "speaker": "E"}]
        self.assertEqual(speaker_samples.pick_clips(segments, "E"), [(3, 9)])


class EngineChoiceTests(unittest.TestCase):
    def test_old_jobs_stay_on_pyannote(self) -> None:
        """У файлов до выбора движка в опциях его нет — их размечал Pyannote."""
        self.assertEqual(
            transcriber._diarization_engine({"diarization": True}), "pyannote"
        )
        self.assertEqual(
            transcriber._diarization_engine({"diarization_engine": "nemotron"}),
            "nemotron",
        )
        self.assertEqual(
            transcriber._diarization_engine({"diarization_engine": "other"}),
            "pyannote",
        )

    def test_engine_setting_defaults_to_nemotron(self) -> None:
        self.assertEqual(storage.DEFAULT_SETTINGS["diarization_engine"], "nemotron")
        self.assertEqual(
            storage.SETTING_VALUES["diarization_engine"], {"pyannote", "nemotron"}
        )

    def test_missing_or_invalid_engine_uses_nemotron_and_explicit_choice_is_kept(self) -> None:
        for settings, expected in (
            ({}, "nemotron"),
            ({"diarization_engine": "invalid"}, "nemotron"),
            ({"diarization_engine": "pyannote"}, "pyannote"),
            ({"diarization_engine": "nemotron"}, "nemotron"),
        ):
            with patch.object(storage, "get_settings", return_value=settings):
                self.assertEqual(diarization.resolve_engine(None), expected)


if __name__ == "__main__":
    unittest.main()
