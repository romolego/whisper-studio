"""Внешние сервисы, которые пользуются этим Whisper как транскрибатором.

Зачем отдельный модуль. Раньше интеграция была одна — Анализ вебинаров — и всё
про неё было вписано прямо в app.py: имя проекта, проверка заголовка, отбор
задач. Все её файлы при этом складывались в один общий проект на верхнем уровне,
поэтому в общей ленте они выглядели как обычные проекты, а внутри одного проекта
свалка росла без разбора.

Теперь каждая интеграция получает:

* свою папку — в ленте сразу видно, что расшифровка пришла из другого сервиса;
* по проекту на каждый проект внешнего сервиса, а не одну общую свалку, — так
  видно, что и когда транскрибировалось;
* отметку на проекте, чтобы источник был виден и в плоской ленте, где папок нет.

Добавить второй или третий сервис — это одна запись в REGISTRY. Именно на это и
был расчёт: с одной интеграцией разница незаметна, а на трёх без разделения по
папкам пользоваться уже нельзя.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import storage


@dataclass(frozen=True)
class Integration:
    key: str
    # Название папки и подписи в интерфейсе.
    title: str
    # Значение заголовка X-Whisper-Integration, которым сервис себя опознаёт.
    header: str
    # Как называть проекты, если внешний сервис не передал своего названия.
    fallback_project: str


REGISTRY: dict[str, Integration] = {
    "webinar_analyzer": Integration(
        key="webinar_analyzer",
        title="Анализ вебинаров",
        header="webinar-analyzer-local",
        fallback_project="Разбор вебинара",
    ),
    "telegram": Integration(
        key="telegram",
        title="Телеграм",
        header="telegram-bot-local",
        fallback_project="Из Телеграма",
    ),
}


def by_header(value: str | None) -> Integration | None:
    if not value:
        return None
    clean = value.strip()
    for integration in REGISTRY.values():
        if integration.header == clean:
            return integration
    return None


def title(key: str | None) -> str:
    integration = REGISTRY.get(str(key or ""))
    return integration.title if integration else ""


def folder(integration: Integration) -> dict[str, Any]:
    """Папка интеграции: находится по отметке или создаётся.

    Поиск идёт по ключу интеграции, а не по названию: папку можно переименовать,
    и она должна остаться той же. Удалить её нельзя — за это отвечает
    storage.delete_folder, иначе вместе с папкой ушли бы расшифровки, которых в
    самом внешнем сервисе может уже не быть.
    """
    for item in storage.list_folders():
        if item.get("integration") == integration.key:
            return item
        # Папка могла быть создана до появления отметки — подбираем по названию
        # и помечаем, чтобы дальше она искалась по ключу и была защищена.
        if str(item.get("name") or "") == integration.title:
            storage.mark_folder_integration(item["id"], integration.key)
            return storage.get_folder(item["id"])
    return storage.create_folder(integration.title, integration=integration.key)


def project(
    integration: Integration,
    external_id: str | None,
    external_name: str | None,
) -> dict[str, Any]:
    """Проект под конкретный проект внешнего сервиса.

    Один проект внешнего сервиса — один проект здесь. Пока внешний сервис
    присылает тот же external_id, файлы дописываются в тот же проект; новый
    разбор получает свой.

    Если external_id не передан, все файлы такого сервиса идут в один проект: без
    идентификатора отличить один разбор от другого невозможно, и плодить проект
    на каждый файл было бы хуже, чем собрать их вместе.
    """
    existing = storage.find_session_by_integration(integration.key, external_id)
    if existing:
        return existing
    name = (external_name or "").strip() or integration.fallback_project
    return storage.create_session(
        name=name,
        folder_id=folder(integration)["id"],
        integration=integration.key,
        integration_external_id=external_id,
    )


def adopt_legacy_projects() -> int:
    """Подобрать проекты интеграций, созданные до появления папок.

    До этой правки Анализ вебинаров складывал файлы в один проект с фиксированным
    названием на верхнем уровне. Он уже есть у пользователя, и оставить его вне
    папки значило бы, что порядок наводится только для будущих файлов.

    Проект опознаётся по названию и по задачам с меткой integration_source в
    настройках — по ней интеграция помечала свои задачи и раньше. Возвращает
    число подобранных проектов.
    """
    legacy_names = {
        "webinar_analyzer": "Webinar Analyzer · локальная очередь",
    }
    adopted = 0
    for key, legacy_name in legacy_names.items():
        integration = REGISTRY.get(key)
        if integration is None:
            continue
        for project in storage.list_sessions():
            if project.get("integration"):
                continue
            if str(project.get("name") or "") != legacy_name:
                continue
            storage.mark_session_integration(project["id"], key, None)
            storage.update_session(
                project["id"], folder_id=folder(integration)["id"]
            )
            adopted += 1
    return adopted


def connected() -> list[dict[str, Any]]:
    """Список сервисов с признаком, пользовались ли они этим Whisper.

    Показывается в настройках: пользователь должен видеть, кто ходит в его
    сервис, не заглядывая в код.
    """
    counts = storage.integration_usage()
    result = []
    for integration in REGISTRY.values():
        usage = counts.get(integration.key, {})
        result.append(
            {
                "key": integration.key,
                "title": integration.title,
                "header": integration.header,
                "projects": usage.get("projects", 0),
                "files": usage.get("files", 0),
                "last_used_at": usage.get("last_used_at"),
            }
        )
    return sorted(result, key=lambda item: item["title"])
