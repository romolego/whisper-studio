"""Кооперативная остановка задач распознавания.

Остановка запрашивается из интерфейса и снимается в трёх местах:

* задача, которая ещё стоит в очереди, помечается в базе и просто не берётся
  в работу;
* задача, которая уже выполняется, проверяет флаг между сегментами и на
  границах этапов и завершается штатно, без записи результата;
* дочерние процессы (FFmpeg, Pyannote) убиваются сразу, поэтому долгие этапы
  извлечения аудио и разделения голосов прерываются, не дожидаясь конца.

Флаг живёт только в памяти процесса: после перезапуска сервиса запрос на
остановку не сохраняется, а прерванная задача возвращается в очередь обычным
механизмом восстановления (storage.init_db).
"""

from __future__ import annotations

import subprocess
import threading


class CancelledJob(Exception):
    """Задачу остановил пользователь. Это не ошибка обработки."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Остановлено пользователем: {task_id}")
        self.task_id = task_id


_lock = threading.RLock()
_requested: set[str] = set()
_processes: dict[str, set[subprocess.Popen]] = {}


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    # taskkill /T снимает и возможные дочерние процессы, иначе остался бы
    # осиротевший FFmpeg или рабочий процесс Pyannote.
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass
    try:
        process.kill()
    except Exception:
        pass


def request(task_id: str) -> None:
    """Запросить остановку задачи и немедленно убить её дочерние процессы."""
    with _lock:
        _requested.add(task_id)
        running = list(_processes.get(task_id, ()))
    for process in running:
        _terminate(process)


def is_requested(task_id: str) -> bool:
    with _lock:
        return task_id in _requested


def check(task_id: str) -> None:
    """Точка кооперативной проверки. Вызывается на границах этапов."""
    if is_requested(task_id):
        raise CancelledJob(task_id)


def release(task_id: str) -> None:
    """Забыть задачу после того, как её судьба уже записана в базу."""
    with _lock:
        _requested.discard(task_id)
        _processes.pop(task_id, None)


def requested_ids() -> set[str]:
    with _lock:
        return set(_requested)


def _attach(task_id: str, process: subprocess.Popen) -> None:
    with _lock:
        _processes.setdefault(task_id, set()).add(process)
        already = task_id in _requested
    if already:
        # Остановку успели запросить между проверкой и запуском процесса.
        _terminate(process)


def _detach(task_id: str, process: subprocess.Popen) -> None:
    with _lock:
        group = _processes.get(task_id)
        if group is not None:
            group.discard(process)
            if not group:
                _processes.pop(task_id, None)


def run(
    task_id: str | None,
    command: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run, который можно прервать запросом остановки.

    Поднимает CancelledJob, если процесс был убит по запросу пользователя, —
    чтобы вызывающий код не принял остановку за ошибку обработки.
    """
    if task_id is not None:
        check(task_id)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if task_id is not None:
        _attach(task_id, process)
    try:
        stdout, stderr = process.communicate()
    finally:
        if task_id is not None:
            _detach(task_id, process)
    if task_id is not None and is_requested(task_id):
        raise CancelledJob(task_id)
    return subprocess.CompletedProcess(
        command, process.returncode, stdout, stderr
    )
