"""Keep models lazy and release their cache after the last task goes idle."""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Callable, Iterator

logger = logging.getLogger(__name__)


class IdleModels:
    def __init__(self, release: Callable[[], int], seconds: float = 60) -> None:
        self.release = release
        self.seconds = seconds
        self._lock = threading.RLock()
        self._active = 0
        self._generation = 0
        self._timer: threading.Timer | None = None

    @contextmanager
    def activity(self) -> Iterator[None]:
        # The lock also protects release: a new job cannot load its model
        # between the idle check and cache clearing. Count whole tasks,
        # including batches temporarily yielding the GPU to dictation.
        with self._lock:
            self._generation += 1
            self._active += 1
            if self._timer:
                self._timer.cancel()
                self._timer = None
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
                if not self._active and self.seconds > 0:
                    self._timer = threading.Timer(
                        self.seconds, self.release_if_idle, (self._generation,)
                    )
                    self._timer.daemon = True
                    self._timer.start()

    def release_if_idle(self, generation: int) -> None:
        with self._lock:
            if self._active or generation != self._generation:
                return
            self._timer = None
            try:
                count = self.release()
                if count:
                    logger.info("Whisper: released %s idle model(s)", count)
            except Exception:
                logger.exception("Could not release idle Whisper models")
