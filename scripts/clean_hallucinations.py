"""Вычистить галлюцинации из уже готовых результатов.

Просмотр и экспорт проектов чистятся на лету (backend/exporter.py), поэтому
запускать это не обязательно. Скрипт нужен для файлов, лежащих на диске в
`data/sessions/*/outputs/`: их писали до появления фильтра, и если открыть такой
txt или docx напрямую из папки, мусор в нём всё ещё будет.

По умолчанию только показывает, что нашёл. Перезапись включается явно:

    .venv\\Scripts\\python.exe -m scripts.clean_hallucinations            # отчёт
    .venv\\Scripts\\python.exe -m scripts.clean_hallucinations --apply    # править

Перезапись затрагивает md, txt, docx, srt, vtt и json задания, а также
готовые файлы проектов, которых это коснулось. Сами исходные аудио- и
видеофайлы не трогаются, поэтому любое задание всегда можно распознать заново
кнопкой «В очередь».
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend import exporter, hallucinations, storage
from backend.transcriber import _write_outputs


def _jobs_with_json() -> list[dict]:
    result = []
    with storage.connect() as db:
        rows = db.execute("SELECT id FROM jobs ORDER BY created_at").fetchall()
    for row in rows:
        try:
            job = storage.get_job(row[0])
        except KeyError:
            continue
        value = job["outputs"].get("json")
        if value and Path(value).is_file():
            result.append(job)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="перезаписать файлы результата, а не только показать отчёт",
    )
    arguments = parser.parse_args()

    total_removed = 0
    touched = 0
    projects: set[str] = set()
    for job in _jobs_with_json():
        path = Path(job["outputs"]["json"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        segments = payload.get("segments") or []
        cleaned, removed = hallucinations.clean_segments(segments)
        if not removed:
            continue

        touched += 1
        total_removed += len(removed)
        print(f"\n{job['original_name']}  (задание {job['id'][:8]})")
        for item in removed:
            print(
                f"  {item['start']:9.1f}-{item['end']:.1f}  "
                f"{item['text']!r}\n      причина: {item['reason']}"
            )

        if not arguments.apply:
            continue

        metadata = {
            "language": payload.get("language")
            or job.get("detected_language")
            or "unknown",
            "language_probability": payload.get("language_probability") or 0,
            "transcription_profile": payload.get("transcription_profile"),
            "duration": payload.get("duration_seconds")
            or job.get("duration_seconds")
            or 0,
            "removed_hallucinations": (
                (payload.get("removed_hallucinations") or []) + removed
            ),
        }
        _write_outputs(job, cleaned, metadata)
        projects.add(job["session_id"])
        print("      переписано: md, txt, docx, srt, vtt, json")

    # Готовый файл проекта собран из заданий, поэтому чистка задания делает
    # его устаревшим ровно так же, как новое распознавание.
    for project_id in projects:
        exporter.refresh_project_files(project_id)

    if not touched:
        print("Мусора в готовых результатах не найдено.")
        return 0

    print(
        f"\nЗаданий с мусором: {touched}, снятых фрагментов: {total_removed}."
    )
    if not arguments.apply:
        print("Это только отчёт. Для перезаписи добавьте --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
