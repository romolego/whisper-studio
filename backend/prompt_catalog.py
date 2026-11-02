"""User prompt configuration; defaults remain source controlled, results stay in projects."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from . import storage
from .project_locks import project_lock

CUSTOM_ID = r"custom_[a-f0-9]{32}"


def defaults() -> list[dict]:
    source = Path(__file__).resolve().parents[1] / "app" / "transcript-prompts.json"
    return [{**item, "type": "images" if item["id"] == "diagram" else "text", "enabled": True}
            for item in json.loads(source.read_text(encoding="utf-8"))]


def _read() -> dict:
    path = storage.DATA_DIR / "prompt-catalog.json"
    if not path.exists():
        return {"revision": 0, "enabled": True, "prompts": defaults(), "types": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or type(data.get("revision")) is not int
                or type(data.get("enabled")) is not bool or not isinstance(data.get("prompts"), list)):
            raise ValueError()
        _validate(data["prompts"], data.get("types", {}))
        return data
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Настройки промптов повреждены. Файл оставлен без изменений.") from exc


def get() -> dict:
    with project_lock("prompt-catalog"):
        return {key: value for key, value in _read().items() if key != "types"}


def _validate(items: list[dict], known: dict) -> None:
    fixed = {item["id"]: item["type"] for item in defaults()} | known
    ids = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Неверное описание промпта")
        key = item.get("id", "")
        if (key not in {"summary", "minutes", "diagram"} and not re.fullmatch(CUSTOM_ID, key)) or key in ids:
            raise ValueError("Неверный или повторяющийся ID промпта")
        ids.add(key)
        if item.get("type") not in {"text", "images"} or (key in fixed and item["type"] != fixed[key]):
            raise ValueError("Тип сохранённого промпта менять нельзя")
        if type(item.get("enabled")) is not bool:
            raise ValueError("Неверное состояние промпта")
        for field, limit in (("label", 120), ("title", 120), ("text", 2_000_000)):
            if not isinstance(item.get(field), str) or not item[field].strip() or len(item[field]) > limit:
                raise ValueError("Заполните название и текст промпта")


def save(enabled: bool, items: list[dict], revision: int) -> dict:
    from .materials import Conflict
    with project_lock("prompt-catalog"):
        current = _read()
        _validate(items, current.get("types", {}))
        if current["enabled"] == enabled and current["prompts"] == items:
            return get()
        if current["revision"] != revision:
            raise Conflict("Промпты изменены в другом окне. Обновите настройки перед сохранением.")
        data = {"revision": revision + 1, "enabled": enabled, "prompts": items,
                "types": current.get("types", {}) | {item["id"]: item["type"] for item in items}}
        path = storage.DATA_DIR / "prompt-catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return get()


def find(kind: str) -> dict:
    item = next((item for item in get()["prompts"] if item["id"] == kind), None)
    if item is None:
        raise KeyError(kind)
    return item
