"""Клиент локального Whisper: те же ручки, которыми пользуется браузер.

Бот сознательно ходит по HTTP, а не импортирует backend напрямую. Так он не
держит открытой базу, не зависит от порядка запуска и переживает перезапуск
сервиса: пропало соединение — значит сервис выключен, и об этом есть что
ответить в чат, а не молчание и упавший процесс.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import requests


# Заголовок, которым бот представляется сервису. Значение совпадает с
# backend/integrations.py: по нему находится папка «Телеграм» и отметка
# источника на проектах и файлах.
INTEGRATION_HEADER = {"X-Whisper-Integration": "telegram-bot-local"}


class WhisperUnavailable(RuntimeError):
    """Сервис не отвечает: выключен, ещё грузится или занят портом."""


class WhisperError(RuntimeError):
    """Сервис ответил отказом и объяснил причину."""


class WhisperApi:
    def __init__(self, base: str = "http://127.0.0.1:8765/api") -> None:
        self.base = base.rstrip("/")
        self.session = requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(INTEGRATION_HEADER)
        try:
            response = self.session.request(
                method,
                f"{self.base}{path}",
                timeout=timeout,
                headers=headers,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise WhisperUnavailable(str(exc)) from exc
        if response.status_code >= 400:
            raise WhisperError(_detail(response))
        return response

    # --- состояние -------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", timeout=10).json()

    def alive(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except (WhisperUnavailable, WhisperError):
            return False

    # --- проекты и файлы -------------------------------------------------

    def project(self, external_id: str, name: str) -> dict[str, Any]:
        """Проект в папке «Телеграм»: тот же external_id — тот же проект."""
        return self._request(
            "POST",
            "/integrations/project",
            json={"external_id": external_id, "name": name[:120]},
        ).json()

    def enqueue_file(
        self,
        project_id: str,
        path: Path,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """Поставить файл в общую очередь распознавания."""
        data = {
            "session_id": project_id,
            "language": options.get("language", "ru"),
            "quality": options.get("quality", "accurate"),
            "timestamps": options.get("timestamps", "segments"),
            "formatting": options.get("formatting", "formatted"),
            "diarization": str(bool(options.get("diarization", False))).lower(),
        }
        with path.open("rb") as handle:
            created = self._request(
                "POST",
                "/jobs",
                data=data,
                files={"files": (path.name, handle)},
                timeout=600,
            ).json()
        if not created:
            raise WhisperError("Сервис не создал задание")
        return created[0]

    def job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}", timeout=15).json()

    def jobs(self, project_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/jobs",
            params={"session_id": project_id},
            timeout=15,
        ).json()

    def text(self, job_id: str) -> str:
        return self._request("GET", f"/jobs/{job_id}/text", timeout=30).text

    def export(self, job_id: str, kind: str, destination: Path) -> Path:
        """Скачать готовый docx/txt/srt/vtt в файл рядом с ботом."""
        response = self._request(
            "GET",
            f"/jobs/{job_id}/download/{kind}",
            timeout=120,
            stream=True,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as target:
            for chunk in response.iter_content(1024 * 256):
                target.write(chunk)
        return destination

    # --- ссылки ----------------------------------------------------------

    def probe_url(self, url: str, passcode: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/url-downloads/probe",
            json={"url": url, "passcode": passcode},
            timeout=120,
        ).json()

    def start_url_download(
        self,
        url: str,
        project_id: str,
        quality: str,
        options: dict[str, Any],
        passcode: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/url-downloads",
            json={
                "url": url,
                "quality": quality,
                "project_id": project_id,
                "passcode": passcode,
                "transcription_language": options.get("language", "ru"),
                "transcription_quality": options.get("quality", "accurate"),
                "transcription_diarization": bool(options.get("diarization", False)),
            },
            timeout=180,
        ).json()

    def url_download_status(self, project_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/url-downloads/{project_id}",
            timeout=15,
        ).json()


def _detail(response: requests.Response) -> str:
    """Вытащить объяснение сервиса, а не показывать голый код ответа."""
    try:
        body = response.json()
    except ValueError:
        return f"сервис ответил {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    return f"сервис ответил {response.status_code}"
