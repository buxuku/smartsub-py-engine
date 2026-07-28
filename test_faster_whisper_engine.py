"""Unit tests for faster-whisper parameter forwarding (no model required)."""

import unittest
from unittest.mock import patch

from engines import faster_whisper_engine


class _FakeInfo:
    language = "en"
    language_probability = 1.0
    duration = 1.0


class _FakeModel:
    def __init__(self):
        self.kwargs = None

    def transcribe(self, _audio_file, **kwargs):
        self.kwargs = kwargs
        return iter(()), _FakeInfo()


class FasterWhisperAdvancedParamsTest(unittest.TestCase):
    def test_forwards_explicit_beam_and_best_of(self):
        model = _FakeModel()
        params = {
            "audio_file": "fixture.wav",
            "beam_size": 7,
            "best_of": 3,
        }

        with patch.object(
            faster_whisper_engine,
            "_get_model",
            return_value=model,
        ):
            result = faster_whisper_engine.transcribe(
                params,
                emit_event=lambda *_args: None,
                is_cancelled=lambda: False,
            )

        self.assertEqual(model.kwargs["beam_size"], 7)
        self.assertEqual(model.kwargs["best_of"], 3)
        self.assertEqual(result["engine"], "faster_whisper")

    def test_omits_unspecified_advanced_params(self):
        model = _FakeModel()

        with patch.object(
            faster_whisper_engine,
            "_get_model",
            return_value=model,
        ):
            faster_whisper_engine.transcribe(
                {"audio_file": "fixture.wav"},
                emit_event=lambda *_args: None,
                is_cancelled=lambda: False,
            )

        self.assertNotIn("beam_size", model.kwargs)
        self.assertNotIn("best_of", model.kwargs)


if __name__ == "__main__":
    unittest.main()
