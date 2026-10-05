"""Модели держатся в памяти по языкам, а не по одной.

Замер на GTX 1060: русская модель поднимается за 1.84 с (на диске уже int8),
английская — за 32.96 с, потому что на диске она во float16 и переквантуется в
int8 при каждой загрузке. Раньше смена языка выгружала одну модель и грузила
другую, и смешанная очередь ru/en платила эти 33 секунды за каждое
переключение.

Обе модели в видеопамяти занимают 1734 МиБ из 6144 — помещаются. Но пик разметки
спикеров 5360 МиБ, поэтому перед Pyannote модели обязаны выгружаться.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_model_cache
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from backend import transcriber


class _StubModel:
    def __init__(self, path: str, **kwargs) -> None:
        self.path = path
        self.kwargs = kwargs


class ModelCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        transcriber.release_models()
        self.addCleanup(transcriber.release_models)

    def test_same_language_loads_once(self) -> None:
        with patch.object(
            transcriber, "WhisperModel", side_effect=_StubModel
        ) as factory:
            first, device = transcriber._load_model("ru")
            second, _ = transcriber._load_model("ru")
        self.assertIs(first, second)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(device, "cuda")

    def test_switching_back_does_not_reload(self) -> None:
        """Главное в этой правке: ru -> en -> ru стоит две загрузки, не три."""
        with patch.object(
            transcriber, "WhisperModel", side_effect=_StubModel
        ) as factory:
            russian, _ = transcriber._load_model("ru")
            english, _ = transcriber._load_model("en")
            russian_again, _ = transcriber._load_model("ru")
            english_again, _ = transcriber._load_model("auto")

        self.assertEqual(factory.call_count, 2)
        self.assertIs(russian, russian_again)
        self.assertIs(english, english_again)
        self.assertIsNot(russian, english)
        self.assertEqual(
            transcriber.loaded_model_keys(), ["ru", "turbo"]
        )

    def test_auto_uses_the_multilingual_model(self) -> None:
        with patch.object(transcriber, "WhisperModel", side_effect=_StubModel):
            auto, _ = transcriber._load_model("auto")
            english, _ = transcriber._load_model("en")
        self.assertIs(auto, english)
        self.assertEqual(transcriber.loaded_model_keys(), ["turbo"])

    def test_release_frees_everything(self) -> None:
        with patch.object(transcriber, "WhisperModel", side_effect=_StubModel):
            transcriber._load_model("ru")
            transcriber._load_model("en")
            self.assertEqual(len(transcriber.loaded_model_keys()), 2)
            self.assertEqual(transcriber.release_models(), 2)
            self.assertEqual(transcriber.loaded_model_keys(), [])
            # После выгрузки модель грузится заново, а не берётся из пустоты.
            transcriber._load_model("ru")
            self.assertEqual(transcriber.loaded_model_keys(), ["ru"])

    def test_cpu_fallback_is_cached_with_its_device(self) -> None:
        """Машина без видеокарты: откат кешируется вместе с устройством."""
        calls: list[str] = []

        def factory(path: str, **kwargs):
            calls.append(kwargs["device"])
            if kwargs["device"] == "cuda":
                raise RuntimeError("no CUDA-capable device is detected")
            return _StubModel(path, **kwargs)

        with patch.object(transcriber, "WhisperModel", side_effect=factory):
            model, device = transcriber._load_model("ru")
            again, device_again = transcriber._load_model("ru")

        self.assertEqual(device, "cpu")
        self.assertEqual(device_again, "cpu")
        self.assertIs(model, again)
        # Второй вызов не должен снова пробовать cuda.
        self.assertEqual(calls, ["cuda", "cpu"])

    def test_release_is_safe_when_nothing_is_loaded(self) -> None:
        self.assertEqual(transcriber.release_models(), 0)
        self.assertEqual(transcriber.release_models(), 0)


class DiarizationFreesMemoryTests(unittest.TestCase):
    """Перед Pyannote модели обязаны уходить из видеопамяти."""

    def test_release_is_called_before_diarization(self) -> None:
        source = transcriber.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()

        # Обе точки, где запускается разметка спикеров, должны сначала
        # освободить видеопамять: иначе Whisper (889 МиБ) плюс пик Pyannote
        # (5360 МиБ) не влезут в 6144 МиБ.
        for marker in ("items = diarization.assign_speakers", "segments = diarization.assign_speakers"):
            index = text.index(marker)
            preceding = text[:index]
            release_at = preceding.rindex("release_models()")
            batch_at = preceding.rindex("gpu_turns.batch()")
            self.assertLess(
                batch_at,
                release_at,
                "release_models() должен вызываться под слотом батча",
            )
            self.assertLess(
                index - release_at,
                400,
                f"release_models() слишком далеко от {marker}",
            )


if __name__ == "__main__":
    unittest.main()
