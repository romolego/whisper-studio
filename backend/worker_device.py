"""Выбор устройства для процессов разметки спикеров.

Общий для Pyannote и Nemotron: оба запускаются отдельным интерпретатором
среды диаризации и подчиняются одной настройке «Устройство». Модуль нарочно
импортирует только torch — worker Nemotron не должен тянуть за собой Pyannote.
"""

from __future__ import annotations

import os
import sys

import torch


def pick_device() -> torch.device:
    """GPU, если сборка torch умеет работать с этой видеокартой.

    На CPU без AVX2 диаризация идёт примерно в 0.4x реального времени, то есть
    часовая запись считается больше двух часов. Поэтому GPU здесь решает всё.
    Проверяем не только доступность CUDA, но и что ядра под архитектуру карты
    действительно собраны: иначе torch падает уже во время вычислений.
    """
    preferred = os.environ.get("WHISPER_DIARIZATION_DEVICE", "auto").lower()
    if preferred == "cpu":
        return torch.device("cpu")
    if not torch.cuda.is_available():
        return torch.device("cpu")
    try:
        major, minor = torch.cuda.get_device_capability(0)
        if f"sm_{major}{minor}" not in torch.cuda.get_arch_list():
            print(
                f"CUDA sm_{major}{minor} не поддержана этой сборкой torch, "
                "используется CPU",
                file=sys.stderr,
            )
            return torch.device("cpu")
        # Пробный расчёт: ловит случаи, когда ядра формально есть, но
        # драйвер или видеопамять не дают их выполнить.
        probe = torch.zeros(8, 8, device="cuda")
        _ = (probe @ probe).sum().item()
        return torch.device("cuda")
    except Exception as exc:
        print(f"GPU недоступен ({exc}), используется CPU", file=sys.stderr)
        return torch.device("cpu")
