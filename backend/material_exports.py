"""Disk export of project artifacts, independent of HTTP and browser downloads."""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
import zipfile
from pathlib import Path

from . import materials, storage

logger = logging.getLogger(__name__)


def export_projects(ids: list[str], mode: str, include: bool, extension: str,
                    timestamps: str, formatting: str, speakers: str) -> dict:
    export_id = uuid.uuid4().hex
    root = storage.DATA_DIR / "material-exports" / export_id
    root.mkdir(parents=True)
    result = {"export_id": export_id, "directory": str(root), "projects": [], "errors": []}
    for project_id in ids:
        stage = root / f".stage-{uuid.uuid4().hex}"
        try:
            project = storage.get_session(project_id)
            files = materials.package_files(project_id, include, extension, timestamps, formatting, speakers)
            # Full ID also prevents collisions between projects with identical
            # names and the same first eight ID characters.
            folder = root / (storage.safe_file_name(project["name"], fallback="Проект")[:90] + f" [{project_id}]")
            stage.mkdir()
            names = []
            if mode in {"files", "both"}:
                for name, content in files:
                    materials.atomic_write(stage / name, content)
                    names.append(name)
            if mode in {"zip", "both"}:
                materials.atomic_write(stage / "Материалы.zip", materials.zip_files(files))
                names.append("Материалы.zip")
            stage.rename(folder)
            result["projects"].append({"project_id": project_id, "directory": str(folder),
                                        "files": [str(folder / name) for name in names]})
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            result["errors"].append({"project_id": project_id, "error": str(exc)})
        finally:
            # Only this operation's generated staging directory can be removed.
            if stage.exists():
                try:
                    shutil.rmtree(stage)
                except OSError:
                    logger.warning("Could not remove partial material export: %s", stage)
    materials.atomic_write(root / "Отчёт.json", json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
    partial = root / ".package.tmp"
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(root / "Отчёт.json", "Отчёт.json")
            # Only completed projects enter the archive; never include staging
            # files left by a failed or interrupted export.
            for project in result["projects"]:
                for filename in project["files"]:
                    path = Path(filename)
                    archive.write(path, path.relative_to(root).as_posix())
        os.replace(partial, root / "Пакет.zip")
    finally:
        partial.unlink(missing_ok=True)
    result["download_url"] = f"/api/materials/exports/{export_id}/archive"
    return result
