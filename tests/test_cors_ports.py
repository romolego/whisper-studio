"""CORS обязан покрывать весь диапазон портов, который выбирает start.ps1.

Интерфейс по умолчанию на 3000, но при занятом порте start.ps1 берёт следующий
свободный вплоть до 3030. Раньше в CORS был список из одного порта 3000, и на
занятом порте сервис ломался наглухо: страница открывалась, а браузер резал все
запросы к API без единого сообщения.

Запуск:  .venv\\Scripts\\python.exe -m unittest tests.test_cors_ports
"""
from __future__ import annotations

import re
import unittest

from starlette.middleware.cors import CORSMiddleware

from backend import app as backend_app


def _cors_regex() -> re.Pattern[str]:
    for middleware in backend_app.app.user_middleware:
        if middleware.cls is CORSMiddleware:
            pattern = middleware.kwargs["allow_origin_regex"]
            return re.compile(pattern)
    raise AssertionError("CORSMiddleware не подключён")


class CorsOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pattern = _cors_regex()

    def test_all_ports_start_script_can_pick(self) -> None:
        # start.ps1: Get-FreeLocalPort 3000 -> кандидаты 3000..3030.
        for port in range(3000, 3031):
            for host in ("127.0.0.1", "localhost", "[::1]"):
                origin = f"http://{host}:{port}"
                with self.subTest(origin=origin):
                    self.assertTrue(
                        self.pattern.fullmatch(origin),
                        f"origin отклонён: {origin}",
                    )

    def test_outside_hosts_are_rejected(self) -> None:
        for origin in (
            "http://evil.example",
            "http://evil.example:3000",
            "https://127.0.0.1:3000",
            "http://192.168.0.107:3000",
            "http://127.0.0.1.evil.example:3000",
            "http://127.0.0.1:8765",
            "http://127.0.0.1:3031",
            "http://127.0.0.1:80",
        ):
            with self.subTest(origin=origin):
                self.assertIsNone(
                    self.pattern.fullmatch(origin),
                    f"посторонний origin принят: {origin}",
                )


if __name__ == "__main__":
    unittest.main()
