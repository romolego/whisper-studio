import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from backend import speech_profile


class SpeechProfileTests(unittest.TestCase):
    def test_distant_turns_are_decoded_separately_without_losing_original_offsets(self):
        calls = []
        def transcribe(audio, **options):
            calls.append((len(audio), options))
            word = SimpleNamespace(start=.2, end=.4, word=" Проверка", probability=.9)
            segment = SimpleNamespace(start=.1, end=.5, text=" Проверка.", words=[word])
            return iter([segment]), SimpleNamespace(language="ru", language_probability=1)
        model = SimpleNamespace(transcribe=transcribe)
        with patch.object(speech_profile, "decode_audio", return_value=np.zeros(20*16000)), \
                patch.object(speech_profile, "get_speech_timestamps", return_value=[
                    {"start":0,"end":16000}, {"start":80000,"end":96000}]):
            result, info = speech_profile.transcribe(model, "unused.wav", {
                "language":"ru", "beam_size":10, "vad_filter":True,
                "vad_parameters":{"threshold":.2}, "condition_on_previous_text":False}, "test")
            items = list(result)
        self.assertEqual([item.start for item in items], [.1, 5.1])
        self.assertEqual([item.words[0].start for item in items], [.2, 5.2])
        self.assertEqual(info.duration, 20)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(length==16000 and not opts["vad_filter"] for length,opts in calls))
        self.assertTrue(all(opts["beam_size"]==10 for _,opts in calls))


if __name__ == "__main__": unittest.main()
