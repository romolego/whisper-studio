"""Foreground opt-in adapter lease. Closing this launcher disables the listener."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import requests

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def tailscale_address():
    executable = shutil.which("tailscale.exe") or str(
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale" / "tailscale.exe")
    if not Path(executable).is_file():
        raise RuntimeError(
            "Для связи через интернет установите Tailscale на оба компьютера и войдите в одну сеть: "
            "https://tailscale.com/download/windows . Затем запустите адаптер снова. "
            "Для домашней сети можно явно указать -BindAddress <IPv4 этого компьютера>.")
    result = subprocess.run([executable, "ip", "-4"], capture_output=True, text=True,
                            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Откройте Tailscale, войдите в свою сеть и запустите адаптер снова.")
    return result.stdout.strip().splitlines()[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", default=8766, type=int)
    args = parser.parse_args()
    address = args.bind or tailscale_address()
    base = "http://127.0.0.1:8765/api/remote/host"
    with requests.Session() as session:
        session.trust_env = False

        def post(path, value):
            result = session.post(base + path, json=value, timeout=(3, 15))
            if result.status_code >= 300:
                if result.status_code == 404:
                    raise RuntimeError("Перезапустите Whisper после обновления, затем запустите адаптер.")
                raise RuntimeError(result.json().get("detail", "Не удалось включить адаптер"))
            return result.json()

        lease = post("/start", {"address": address, "port": args.port})
        try:
            print("\nУдалённые вычисления включены: " + lease["url"])
            print("\nКод подключения (храните как пароль):\n\n" + lease["code"] + "\n")
            try:
                subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                               input=lease["code"], text=True, timeout=10, check=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                print("Код скопирован в буфер обмена.")
            except (OSError, subprocess.SubprocessError):
                pass
            print("На ноутбуке: Настройки → Вычисления → вставить код → Использовать удалённый компьютер.")
            print("Оставьте это окно открытым. Ctrl+C выключает адаптер.", flush=True)
            while True:
                time.sleep(10)
                status = post("/heartbeat", {"lease": lease["lease"]})
                if not status["running"]:
                    raise RuntimeError("Сетевой адаптер остановлен. Проверьте, свободен ли выбранный порт.")
        except KeyboardInterrupt:
            print("\nВыключаю удалённые вычисления…")
        finally:
            try:
                post("/stop", {"lease": lease["lease"]})
            except (requests.RequestException, RuntimeError):
                pass


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException) as exc:
        print(str(exc))
        raise SystemExit(1)
