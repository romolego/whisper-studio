from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import app as backend_app
from backend import diarization, exporter, media, transcriber


class TrimBoundsTests(unittest.TestCase):
    def test_untouched_track_stays_whole(self) -> None:
        self.assertEqual(media.trim_bounds(3600, 0, None), (0.0, None))

    def test_middle_fragment_keeps_both_edges(self) -> None:
        self.assertEqual(media.trim_bounds(3600, 754.5, 874.5), (754.5, 874.5))

    def test_end_at_the_very_tail_drops_the_limit(self) -> None:
        # Ручка доведена до правого края: ограничивать `-t` нельзя, иначе
        # неточная длительность контейнера срежет последние слова.
        self.assertEqual(media.trim_bounds(3600, 60, 3599.8), (60.0, None))

    def test_bounds_are_clamped_to_the_file(self) -> None:
        self.assertEqual(media.trim_bounds(600, -30, 4000), (0.0, None))

    def test_fragment_shorter_than_a_second_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            media.trim_bounds(3600, 100, 100.4)


class ExtractAudioTrimTests(unittest.TestCase):
    def _command(self, run) -> list[str]:
        run.assert_called_once()
        return list(run.call_args.args[1])

    @patch.object(media.Path, "exists", return_value=True)
    @patch.object(media.cancellation, "run")
    def test_trim_adds_seek_before_input_and_length_after(
        self,
        run,
        _exists,
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stderr = ""

        media.extract_audio(
            Path("source.mkv"),
            2,
            Path("target.wav"),
            "job-1",
            start_seconds=754.5,
            end_seconds=874.5,
        )

        command = self._command(run)
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(command[command.index("-ss") + 1], "754.500")
        self.assertGreater(command.index("-t"), command.index("-i"))
        self.assertEqual(command[command.index("-t") + 1], "120.000")

    @patch.object(media.Path, "exists", return_value=True)
    @patch.object(media.cancellation, "run")
    def test_whole_track_passes_no_trim_flags(self, run, _exists) -> None:
        run.return_value.returncode = 0
        run.return_value.stderr = ""

        media.extract_audio(Path("source.mkv"), 1, Path("target.wav"))

        command = self._command(run)
        self.assertNotIn("-ss", command)
        self.assertNotIn("-t", command)

    @patch.object(media.Path, "exists", return_value=True)
    @patch.object(media.cancellation, "run")
    def test_open_end_keeps_the_tail(self, run, _exists) -> None:
        run.return_value.returncode = 0
        run.return_value.stderr = ""

        media.extract_audio(
            Path("source.mkv"),
            1,
            Path("target.wav"),
            start_seconds=60,
        )

        command = self._command(run)
        self.assertIn("-ss", command)
        self.assertNotIn("-t", command)


class FragmentDownloadTests(unittest.TestCase):
    def _cut(self, start: float, end: float | None) -> list[str]:
        """Вызвать вырезание с подставным FFmpeg и вернуть его команду."""
        seen: list[list[str]] = []

        def fake_ffmpeg(command, **_kwargs):
            seen.append(list(command))
            Path(command[-1]).write_bytes(b"fake mp3 payload")
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "cut.mp3"
            with patch.object(media, "fragment_path", return_value=target):
                with patch.object(media.subprocess, "run", fake_ffmpeg):
                    result = media.create_fragment(
                        "media-1",
                        Path("source.mkv"),
                        2,
                        start,
                        end,
                    )
                self.assertTrue(result.exists())
        return seen[0]

    def test_fragment_cuts_exactly_the_selected_piece(self) -> None:
        command = self._cut(754.5, 874.5)

        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(command[command.index("-ss") + 1], "754.500")
        self.assertEqual(command[command.index("-t") + 1], "120.000")
        self.assertIn("libmp3lame", command)
        self.assertIn("0:2", command)

    def test_fragment_open_to_the_end_has_no_length_limit(self) -> None:
        command = self._cut(754.5, None)

        self.assertIn("-ss", command)
        self.assertNotIn("-t", command)

    def test_download_name_avoids_characters_windows_forbids(self) -> None:
        self.assertEqual(backend_app._file_clock(754), "12-34")
        self.assertEqual(backend_app._file_clock(3754), "1-02-34")
        self.assertEqual(backend_app._file_clock(0), "00-00")


class ShiftSegmentsTests(unittest.TestCase):
    def test_marks_return_to_the_original_timeline(self) -> None:
        segments = [
            {
                "start": 0.0,
                "end": 2.5,
                "text": "первая фраза",
                "words": [{"start": 0.0, "end": 1.2, "word": "первая"}],
            },
        ]

        shifted = transcriber._shift_segments(segments, 754.5)

        self.assertEqual(shifted[0]["start"], 754.5)
        self.assertEqual(shifted[0]["end"], 757.0)
        self.assertEqual(shifted[0]["words"][0]["start"], 754.5)
        self.assertEqual(shifted[0]["words"][0]["end"], 755.7)

    def test_whole_track_is_left_untouched(self) -> None:
        segments = [{"start": 1.0, "end": 2.0, "words": None}]
        self.assertEqual(
            transcriber._shift_segments(segments, 0),
            [{"start": 1.0, "end": 2.0, "words": None}],
        )


class DiarizationOffsetTests(unittest.TestCase):
    @patch.object(diarization, "_turns_local")
    @patch.object(diarization, "_external_ready", return_value=False)
    def test_turns_move_onto_the_segment_timeline(
        self,
        _external_ready,
        turns_local,
    ) -> None:
        # Pyannote слушает обрезанный кусок и считает время от нуля, а метки
        # фрагментов уже переведены на шкалу исходника.
        turns_local.return_value = [(0.0, 5.0, "SPEAKER_00"), (5.0, 9.0, "SPEAKER_01")]
        segments = [
            {"start": 100.5, "end": 103.0, "words": []},
            {"start": 106.0, "end": 108.0, "words": []},
        ]

        result = diarization.assign_speakers(
            Path("cut.wav"),
            segments,
            time_offset=100.0,
        )

        self.assertEqual(result[0]["speaker"], "SPEAKER_00")
        self.assertEqual(result[1]["speaker"], "SPEAKER_01")


def _media(duration: float) -> dict:
    return {
        "id": "media-1",
        "session_id": "project-1",
        "original_name": "Вебинар.mkv",
        "duration_seconds": duration,
        "stored_path": "E:/whisper/data/media-1.mkv",
        "audio_streams": [{"index": 1, "codec": "aac"}],
        "storage_mode": "managed",
    }


class EnqueueTrimTests(unittest.TestCase):
    def _enqueue(self, **item_fields) -> dict:
        return backend_app.MediaEnqueue(
            items=[{"media_id": "media-1", "audio_stream_index": 1, **item_fields}],
            language="ru",
            quality="accurate",
            timestamps="segments",
            formatting="formatted",
            diarization=False,
        )

    @patch.object(backend_app, "enqueue_project_job")
    @patch.object(backend_app.media, "clear_track_previews")
    @patch.object(backend_app.storage, "remove_media_record")
    @patch.object(backend_app.storage, "create_job")
    @patch.object(backend_app.storage, "get_media")
    def test_selected_fragment_reaches_job_options(
        self,
        get_media,
        create_job,
        _remove_media_record,
        _clear_track_previews,
        _enqueue_project_job,
    ) -> None:
        get_media.return_value = _media(3600)
        create_job.return_value = {"id": "job-1"}

        backend_app.enqueue_staged_media(
            self._enqueue(trim_start_seconds=754.5, trim_end_seconds=874.5)
        )

        options = create_job.call_args.args[3]
        self.assertEqual(options["trim_start_seconds"], 754.5)
        self.assertEqual(options["trim_end_seconds"], 874.5)

    @patch.object(backend_app, "enqueue_project_job")
    @patch.object(backend_app.media, "clear_track_previews")
    @patch.object(backend_app.storage, "remove_media_record")
    @patch.object(backend_app.storage, "create_job")
    @patch.object(backend_app.storage, "get_media")
    def test_whole_track_keeps_options_clean(
        self,
        get_media,
        create_job,
        _remove_media_record,
        _clear_track_previews,
        _enqueue_project_job,
    ) -> None:
        get_media.return_value = _media(3600)
        create_job.return_value = {"id": "job-1"}

        backend_app.enqueue_staged_media(self._enqueue())

        options = create_job.call_args.args[3]
        self.assertNotIn("trim_start_seconds", options)
        self.assertNotIn("trim_end_seconds", options)

    @patch.object(backend_app.storage, "get_media")
    def test_too_short_fragment_is_refused(self, get_media) -> None:
        get_media.return_value = _media(3600)

        with self.assertRaises(backend_app.HTTPException) as raised:
            backend_app.enqueue_staged_media(
                self._enqueue(trim_start_seconds=100, trim_end_seconds=100.4)
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Вебинар.mkv", raised.exception.detail)


class CombinedExportTrimTests(unittest.TestCase):
    """Сплошная лента субтитров проекта не должна зиять дырой из-за обрезки."""

    def _files(self) -> list[dict]:
        return [
            {
                "job_id": "job-1",
                "source": "Первый.mp3",
                "duration_seconds": 60.0,
                "time_offset": 0.0,
                "segments": [{"start": 5.0, "end": 8.0, "text": "начало"}],
            },
            {
                "job_id": "job-2",
                "source": "Второй.mkv",
                "duration_seconds": 120.0,
                "time_offset": 754.0,
                "segments": [{"start": 760.0, "end": 763.0, "text": "фрагмент"}],
            },
            {
                "job_id": "job-3",
                "source": "Третий.mp3",
                "duration_seconds": 30.0,
                "time_offset": 0.0,
                "segments": [{"start": 2.0, "end": 4.0, "text": "конец"}],
            },
        ]

    @patch.object(exporter.storage, "list_speaker_aliases", return_value=[])
    @patch.object(exporter.storage, "session_dir")
    @patch.object(exporter, "_project_payload")
    def test_trimmed_file_keeps_its_place_in_the_combined_srt(
        self,
        project_payload,
        session_dir,
        _aliases,
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            session_dir.return_value = Path(folder)
            project_payload.return_value = (
                {"id": "project-1", "name": "Проект"},
                [],
                self._files(),
            )

            target = exporter.create_project_export(
                "project-1",
                "srt",
                "segments",
            )
            stamps = [
                line
                for line in target.read_text(encoding="utf-8").splitlines()
                if "-->" in line
            ]

        self.assertEqual(
            stamps,
            [
                # первый файл лежит как есть
                "00:00:05,000 --> 00:00:08,000",
                # обрезанный кусок начинается через 6 с после стыка на 60-й
                "00:01:06,000 --> 00:01:09,000",
                # третий файл идёт за полными двумя минутами второго
                "00:03:02,000 --> 00:03:04,000",
            ],
        )


if __name__ == "__main__":
    unittest.main()
