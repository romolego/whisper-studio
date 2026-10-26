"""Абзацы вместо сегментов и готовые файлы проекта на диске.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_paragraphs

Проверяется ровно две вещи. Первая: граница абзаца стоит там, где она есть в
речи — на смене спикера, на паузе, на точке, — а не по счётчику знаков.
Вторая: расшифровка лежит файлами рядом с проектом и не отстаёт от базы.

Отрицательная часть здесь важна не меньше положительной: абзац, разорванный
посреди предложения, читается хуже, чем не разорванный вовсе.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from backend import exporter, paragraphs


def _segment(
    text: str,
    start: float,
    end: float | None = None,
    speaker: str | None = None,
) -> dict:
    item: dict = {
        "start": start,
        "end": start + 3.0 if end is None else end,
        "text": text,
    }
    if speaker is not None:
        item["speaker"] = speaker
    return item


def _sentences(count: int, start: float = 0.0, gap: float = 0.2) -> list[dict]:
    """Ровные фразы по 100 знаков с точкой на конце."""
    items = []
    position = start
    for index in range(count):
        text = f"Фраза номер {index} " + "слово " * 14
        items.append(_segment(text.strip()[:99] + ".", position, position + 6))
        position += 6 + gap
    return items


class GroupTests(unittest.TestCase):
    def test_short_talk_stays_one_paragraph(self) -> None:
        # Три фразы подряд без пауз — это одна мысль, рвать нечего.
        groups = paragraphs.group(_sentences(3))
        self.assertEqual(len(groups), 1)

    def test_break_waits_for_the_end_of_a_sentence(self) -> None:
        groups = paragraphs.group(_sentences(9))
        # Фразы по 99 знаков: порог в 400 набирается на пятой, и граница встаёт
        # сразу за ней — потому что там точка.
        self.assertEqual([len(item) for item in groups], [5, 4])
        for group in groups:
            self.assertTrue(group[-1]["text"].endswith("."))

    def test_speaker_change_always_breaks(self) -> None:
        groups = paragraphs.group(
            [
                _segment("Короткий вопрос?", 0, 2, speaker="SPEAKER_00"),
                _segment("Короткий ответ.", 2, 4, speaker="SPEAKER_01"),
                _segment("И ещё реплика.", 4, 6, speaker="SPEAKER_01"),
            ]
        )
        self.assertEqual([len(item) for item in groups], [1, 2])

    def test_long_pause_breaks_a_grown_paragraph(self) -> None:
        groups = paragraphs.group(
            [
                _segment("а" * 160, 0, 10),
                _segment("Продолжение после паузы.", 14, 18),
            ]
        )
        self.assertEqual(len(groups), 2)

    def test_pause_does_not_split_a_couple_of_words(self) -> None:
        # «Да.» и «Угу.» с паузами вокруг не должны стать отдельными абзацами.
        groups = paragraphs.group(
            [
                _segment("Да.", 0, 1),
                _segment("Угу.", 6, 7),
                _segment("Понятно.", 12, 13),
            ]
        )
        self.assertEqual(len(groups), 1)

    def test_speech_without_punctuation_still_gets_paragraphs(self) -> None:
        # Модель не расставила ни одной точки: единственная защита от стены —
        # жёсткий предел.
        segments = [
            _segment("слово " * 16, index * 6, index * 6 + 6)
            for index in range(30)
        ]
        groups = paragraphs.group(segments)
        self.assertGreater(len(groups), 1)
        for group in groups:
            length = sum(len(item["text"]) for item in group)
            self.assertLessEqual(length, paragraphs.HARD_LIMIT_CHARS + 200)


class RenderTests(unittest.TestCase):
    def test_paragraphs_are_separated_by_a_blank_line(self) -> None:
        text = exporter.render_segments(_sentences(9))
        self.assertIn("\n\n", text)
        self.assertEqual(len(text.split("\n\n")), 2)
        # Внутри абзаца переносов нет — это сплошной текст, а не лесенка.
        self.assertNotIn("\n", text.split("\n\n")[0])

    def test_plain_formatting_keeps_the_same_borders(self) -> None:
        # «Без знаков» снимает точки, по которым абзац и определяется. Границы
        # считаются до очистки, поэтому число абзацев обязано совпасть.
        formatted = exporter.render_segments(_sentences(9))
        plain = exporter.render_segments(_sentences(9), formatting="plain")
        self.assertEqual(
            len(formatted.split("\n\n")),
            len(plain.split("\n\n")),
        )
        self.assertNotIn(".", plain)

    def test_speaker_turns_become_labelled_paragraphs(self) -> None:
        segments = [
            _segment("Первый говорит.", 0, 3, speaker="SPEAKER_00"),
            _segment("Второй отвечает.", 3, 6, speaker="SPEAKER_01"),
        ]
        text = exporter.render_segments(segments, speakers="on")
        self.assertEqual(
            text,
            "СПИКЕР 1:\nПервый говорит.\n\nСПИКЕР 2:\nВторой отвечает.",
        )

    def test_markdown_marks_the_speaker_bold(self) -> None:
        segments = [_segment("Реплика.", 0, 3, speaker="SPEAKER_00")]
        text = exporter.render_segments(
            segments,
            speakers="on",
            markup="markdown",
        )
        self.assertTrue(text.startswith("**СПИКЕР 1:**\n"))

    def test_timecodes_keep_a_line_per_segment(self) -> None:
        # Режим «по фразам» существует ради таймкода у каждой фразы: абзац здесь
        # только группирует, но строки не склеивает.
        text = exporter.render_segments(_sentences(9), timestamps="segments")
        self.assertEqual(len([line for line in text.splitlines() if line]), 9)
        self.assertIn("\n\n", text)
        self.assertTrue(text.startswith("[00:00:00] "))


class ProjectFilesTests(unittest.TestCase):
    """Расшифровка обязана лежать файлом, а не только в базе."""

    def _payload(self, name: str = "Вебинар") -> tuple:
        return (
            {"id": "project-1", "name": name},
            [{"id": "job-1", "original_name": "Запись.mp4", "outputs": {}}],
            [
                {
                    "job_id": "job-1",
                    "source": "Запись.mp4",
                    "duration_seconds": 60.0,
                    "time_offset": 0.0,
                    "segments": [_segment("Первая фраза.", 0, 3)],
                }
            ],
        )

    def _run(self, folder: str, name: str = "Вебинар") -> list[Path]:
        with patch.object(
            exporter.storage, "list_speaker_aliases", return_value={}
        ), patch.object(
            exporter.storage, "session_dir", return_value=Path(folder)
        ), patch.object(
            exporter.storage,
            "get_settings",
            return_value={
                "default_view_timestamps": "none",
                "default_view_formatting": "formatted",
            },
        ), patch.object(
            exporter, "_project_payload", return_value=self._payload(name)
        ):
            return exporter.refresh_project_files("project-1")

    def test_three_formats_appear_next_to_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            written = self._run(folder)
            names = sorted(item.name for item in written)
            self.assertEqual(
                names,
                [
                    "Вебинар — расшифровка.docx",
                    "Вебинар — расшифровка.md",
                    "Вебинар — расшифровка.txt",
                ],
            )
            for item in written:
                self.assertTrue(item.is_file())
            markdown = (
                Path(folder) / "Вебинар — расшифровка.md"
            ).read_text(encoding="utf-8")
            self.assertTrue(markdown.startswith("# Вебинар\n"))
            self.assertIn("Первая фраза.", markdown)

    def test_rename_does_not_leave_the_old_file_behind(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self._run(folder, "Старое имя")
            self._run(folder, "Новое имя")
            left = sorted(
                item.name
                for item in Path(folder).iterdir()
                if "расшифровка" in item.name
            )
            self.assertEqual(
                left,
                [
                    "Новое имя — расшифровка.docx",
                    "Новое имя — расшифровка.md",
                    "Новое имя — расшифровка.txt",
                ],
            )

    def test_foreign_files_are_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            mine = Path(folder) / "Заметки.md"
            mine.write_text("моё", encoding="utf-8")
            self._run(folder)
            self.assertTrue(mine.is_file())

    def test_write_failure_never_fails_the_job(self) -> None:
        # Обновление вызывается сразу после того, как задание объявлено
        # готовым. Открытый в Word файл не должен превращать готовую
        # многочасовую расшифровку в неудачную.
        with patch.object(
            exporter,
            "_write_project_files",
            side_effect=PermissionError("файл занят"),
        ):
            with self.assertLogs("whisper.exporter", level="ERROR"):
                self.assertEqual(exporter.refresh_project_files("project-1"), [])

    def test_project_without_results_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(
                exporter.storage, "session_dir", return_value=Path(folder)
            ), patch.object(
                exporter,
                "_project_payload",
                side_effect=RuntimeError("В проекте пока нет готовых расшифровок"),
            ):
                self.assertEqual(exporter.refresh_project_files("project-1"), [])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_filtered_exports_are_named_and_marked_separately(self) -> None:
        payload = self._payload()
        payload[2][0]["segments"] = [
            _segment("Вопрос заказчика.", 0, 3, "SPEAKER_00"),
            _segment("Большой ответ исполнителя.", 3, 9, "SPEAKER_01"),
        ]
        with tempfile.TemporaryDirectory(dir=exporter.storage.ROOT) as folder, patch.object(
            exporter, "_project_payload", return_value=payload
        ), patch.object(
            exporter.storage, "list_speaker_aliases", return_value={}
        ), patch.object(
            exporter.storage, "session_dir", return_value=Path(folder)
        ):
            for kind in ("txt", "md", "docx", "srt", "vtt", "json"):
                full = exporter.create_project_export("project-1", kind, speakers="on")
                original = full.read_bytes()
                partial = exporter.create_project_export(
                    "project-1", kind, speakers="on", speaker_keys={"job-1:SPEAKER_00"}
                )
                self.assertNotEqual(full, partial)
                self.assertIn("только-выбранные-спикеры", partial.name)
                self.assertEqual(full.read_bytes(), original)
                if kind == "docx":
                    text = "\n".join(p.text for p in Document(partial).paragraphs)
                else:
                    text = partial.read_text(encoding="utf-8")
                self.assertIn("Вопрос заказчика.", text)
                self.assertNotIn("Большой ответ исполнителя.", text)
                if kind in {"txt", "md", "docx"}:
                    self.assertIn("ЧАСТИЧНАЯ РАСШИФРОВКА", text)


class SpeakerFilterTests(unittest.TestCase):
    def test_selecting_all_preserves_unlabelled_speech_and_original_text(self) -> None:
        segments = [
            {**_segment("Весь исходный текст, включая слова без меток.", 0, 8, "A"),
             "words": [{"word": "Весь", "start": 0, "end": 1, "speaker": "A"}]},
            _segment("Реплика без спикера.", 8, 10),
            _segment("Ответ второго участника.", 10, 15, "B"),
        ]
        for timestamps in ("none", "segments", "words"):
            self.assertEqual(
                exporter._render_file_text(segments, timestamps, "formatted", "on", "j", {}, {"j:A", "j:B"}),
                exporter._render_file_text(segments, timestamps, "formatted", "on", "j", {}, None),
            )

    def test_explicit_subset_keeps_only_requested_speaker(self) -> None:
        segments = [
            {**_segment("Вопрос. Ответ.", 0, 4, "A"), "words": [
                {"word": "Вопрос.", "start": 0, "end": 2, "speaker": "A"},
                {"word": "Ответ.", "start": 2, "end": 4, "speaker": "B"},
            ]},
        ]
        text = exporter._render_file_text(segments, "none", "formatted", "on", "j", {}, {"j:B"})
        self.assertIn("Ответ.", text)
        self.assertNotIn("Вопрос.", text)


if __name__ == "__main__":
    unittest.main()
