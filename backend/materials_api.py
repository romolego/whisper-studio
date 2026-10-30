"""Artifact HTTP boundary. Storage/inference adapters have no HTTP dependency."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Literal
from urllib.parse import quote
import mimetypes

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import exporter, material_exports, materials, storage

router = APIRouter(prefix="/api/projects/{project_id}/materials", tags=["Project materials"])
batch_router = APIRouter(prefix="/api", tags=["Project materials"])


def prompts() -> list[dict]:
    return json.loads((Path(__file__).resolve().parents[1] / "app" / "transcript-prompts.json").read_text(encoding="utf-8"))


@batch_router.get("/material-prompts")
def prompt_catalog():
    return prompts()


@router.get("/prompt/{kind}")
def prompt_input(project_id: str, kind: Literal["summary", "minutes", "diagram"],
                 include_transcript: bool = True,
                 timestamps: Literal["none", "segments", "words"] = "none",
                 formatting: Literal["formatted", "plain"] = "formatted",
                 speakers: Literal["off", "on"] = "off", speaker_keys: str | None = None,
                 include_materials: bool = False):
    _call(storage.get_session, project_id)
    prompt = next(item for item in prompts() if item["id"] == kind)
    text = (_call(exporter.combined_text, project_id, timestamps, formatting, speakers,
                  exporter.parse_speaker_keys(speaker_keys)) if include_transcript else "")
    if include_transcript and not text.strip():
        raise HTTPException(409, "В проекте пока нет готовой транскрибации в выбранном виде")
    result = {"project_id": project_id, "kind": kind, "prompt": prompt["text"],
              "transcript": text, "input": prompt["text"] + ("\n\nТранскрибация:\n" + text if include_transcript else "")}
    if include_materials:
        if kind != "diagram":
            raise HTTPException(422, "Материалы добавляются к промпту схемы")
        data = _call(materials.get, project_id)
        blocks = [prompt["text"]]
        if data["summary"].strip():
            blocks.append("Резюме:\n" + data["summary"])
        if data["minutes"].strip():
            blocks.append("Цифровой протокол:\n" + data["minutes"])
        if include_transcript:
            blocks.append("Исходная транскрибация:\n" + text)
        result.update(input="\n\n---\n\n".join(blocks), summary=data["summary"], minutes=data["minutes"], materials_revision=data["revision"])
    return result


@batch_router.get("/materials/status")
def status(folder_id: str | None = None):
    if folder_id is not None:
        _call(storage.get_folder, folder_id)
    projects = storage.list_sessions(folder_id=folder_id) if folder_id is not None else storage.list_sessions()
    result = []
    for project in projects:
        data = _call(materials.get, project["id"])
        result.append({"project_id": project["id"], "name": project["name"], "folder_id": project["folder_id"],
                       "transcription_ready": bool(project["completed_count"]),
                       "summary_ready": bool(data["summary"].strip()), "minutes_ready": bool(data["minutes"].strip()),
                       "diagram_count": len(data["images"]), "revision": data["revision"]})
    return {"projects": result}


class BatchExport(BaseModel):
    project_ids: list[str] | None = Field(default=None, min_length=1, max_length=500)
    folder_id: str | None = Field(default=None, max_length=64)
    mode: Literal["files", "zip", "both"] = "both"
    include_transcript: bool | None = None
    transcript_format: Literal["txt", "docx"] | None = None
    timestamps: Literal["none", "segments", "words"] = "none"
    formatting: Literal["formatted", "plain"] = "formatted"
    speakers: Literal["off", "on"] = "off"


@batch_router.post("/materials/export")
def batch_export(payload: BatchExport):
    if (payload.project_ids is None) == (payload.folder_id is None):
        raise HTTPException(422, "Укажите либо project_ids, либо folder_id")
    if payload.folder_id is not None:
        _call(storage.get_folder, payload.folder_id)
    ids = (list(dict.fromkeys(payload.project_ids)) if payload.project_ids else
           [item["id"] for item in storage.list_sessions(folder_id=payload.folder_id)])
    if len(ids) > 500:
        raise HTTPException(422, "За один запрос экспортируется до 500 проектов; передайте несколько списков project_ids")
    settings = storage.get_settings()
    include = payload.include_transcript if payload.include_transcript is not None else settings["materials_include_transcript"] == "on"
    extension = payload.transcript_format or settings["materials_transcript_format"]
    return _call(material_exports.export_projects, ids, payload.mode, include, extension,
                 payload.timestamps, payload.formatting, payload.speakers)


@batch_router.get("/materials/exports/{export_id}/archive")
def batch_download(export_id: str):
    if len(export_id) != 32 or any(char not in "0123456789abcdef" for char in export_id):
        raise HTTPException(404, "Экспорт не найден")
    path = storage.DATA_DIR / "material-exports" / export_id / "Пакет.zip"
    if not path.is_file():
        raise HTTPException(404, "Экспорт не найден")
    return FileResponse(path, filename="Материалы проектов.zip")


class TextEdit(BaseModel):
    text: str = Field(max_length=2_000_000)
    revision: int = Field(ge=0)


class ImageEdit(BaseModel):
    id: str = Field(max_length=64)
    note: str = Field(default="", max_length=10000)


class ImagesEdit(BaseModel):
    images: list[ImageEdit] = Field(max_length=5)
    revision: int = Field(ge=0)


def _call(function, *args):
    try:
        return function(*args)
    except materials.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except materials.InvalidState as exc:
        raise HTTPException(503, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "Не удалось прочитать или записать файлы материалов. Повторите после проверки диска.") from exc
    except KeyError as exc:
        raise HTTPException(404, "Проект или материал не найден") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def read(project_id: str):
    return _call(materials.get, project_id)


@router.put("/text/{kind}")
def write(project_id: str, kind: Literal["summary", "minutes"], payload: TextEdit):
    return _call(materials.save_text, project_id, kind, payload.text, payload.revision)


@router.post("/images")
async def upload(project_id: str, revision: int = Form(ge=0), file: UploadFile = File()):
    try:
        content = await file.read(materials.MAX_IMAGE_BYTES + 1)
    finally:
        await file.close()
    # Decode/write outside the event loop.
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_call, materials.add_image, project_id,
                                  file.filename or "Схема", content, revision)


@router.put("/images")
def images(project_id: str, payload: ImagesEdit):
    return _call(materials.edit_images, project_id,
                 [item.model_dump() for item in payload.images], payload.revision)


@router.get("/images/{image_id}")
def image(project_id: str, image_id: str, download: bool = False):
    content, name, extension = _call(materials.image_bytes, project_id, image_id)
    return Response(content, media_type=mimetypes.guess_type(f"image{extension}")[0],
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                             "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quote(name)}"})


def _download(name: str, content: bytes, media_type: str):
    return Response(content, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/text/{kind}/{extension}")
def download(project_id: str, kind: Literal["summary", "minutes"], extension: Literal["txt", "docx"]):
    name, content = _call(materials.text_file, project_id, kind, extension)
    return _download(name, content, "text/plain" if extension == "txt" else
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@router.get("/package/{mode}")
def package(project_id: str, mode: Literal["zip", "files"], include_transcript: bool = True,
            transcript_format: Literal["txt", "docx"] = "txt",
            timestamps: Literal["none", "segments", "words"] = "none",
            formatting: Literal["formatted", "plain"] = "formatted",
            speakers: Literal["off", "on"] = "off", speaker_keys: str | None = None):
    files = _call(materials.package_files, project_id, include_transcript, transcript_format,
                  timestamps, formatting, speakers, speaker_keys)
    if mode == "zip":
        return _download("Материалы проекта.zip", materials.zip_files(files), "application/zip")
    # A single snapshot even if an agent edits the project between downloads.
    return {"files": [{"name": name, "base64": base64.b64encode(content).decode("ascii")}
                       for name, content in files]}
