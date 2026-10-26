"""Local punctuation classification; the recognized words must not change."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from . import cancellation, storage, runtime

ROOT = Path(__file__).resolve().parents[1]
MODEL_REPO = "cointegrated/rubert-base-lesha17-punctuation"
MODEL_REVISION = "b71c5df24bf49aa2014be5671745b0f586952531"
MODEL_SHA256 = "d0351b38c0b8736cf030a9e84d790540225fc0c2449d15daa4e3d6557a4f5297"
MODEL_SIZE = 711_468_072
MODEL_DIR = ROOT / "models" / "punctuation-ru"
LEXEMES = re.compile(r"\w+(?:[-’']\w+)*", re.UNICODE)
LABELS = " !,-.:;?()"  # Published training mapping in Lesha17/Punctuation/utils.py.


def words(text: str) -> list[str]:
    return [match.group().casefold() for match in LEXEMES.finditer(text)]


def apply_labels(text: str, labels: list[int], sentence_start: bool = True) -> str:
    """Insert labels into the original string, never decode words from BERT."""
    matches = list(LEXEMES.finditer(text))
    if len(matches) != len(labels):
        raise ValueError("Punctuation label alignment failed")
    parts = []; cursor = 0
    for index, (match, label) in enumerate(zip(matches, labels)):
        gap = text[cursor:match.start()]
        if any(mark in gap for mark in ".!?…"):
            sentence_start = True
        original = match.group()
        value = original[0].upper() + original[1:] if sentence_start else original
        sentence_start = False
        parts.extend((gap, value))
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        following = text[match.end():next_start]
        if not any(mark in following for mark in ",.!?;:…"):
            mark = LABELS[label] if 0 <= label < len(LABELS) else " "
            if mark in ",.!?;:":
                parts.append(mark)
                sentence_start = mark in ".!?"
        cursor = match.end()
    parts.append(text[cursor:])
    result = "".join(parts)
    if words(result) != words(text):
        raise ValueError("Punctuation processing changed recognized words")
    return result


def restore(segments: list[dict], job_id: str) -> list[dict]:
    if not segments:
        return segments
    if not (MODEL_DIR / "model.safetensors").is_file():
        raise RuntimeError("Модель русской пунктуации не установлена: python -m scripts.install_punctuation_model")
    environments = (".venv_diarization_cpu", ".venv_diarization_gpu") if runtime.preferred_device() == "cpu" else (".venv_diarization_gpu", ".venv_diarization_cpu")
    python = next((ROOT / name / "Scripts/python.exe" for name in environments
                   if (ROOT / name / "Scripts/python.exe").is_file()), None)
    if python is None:
        raise RuntimeError("Для пунктуации нужна установленная среда разметки Whisper")
    result = copy.deepcopy(segments)
    input_path = storage.temporary_dir() / f"{job_id}-punctuation-in.json"
    output_path = storage.temporary_dir() / f"{job_id}-punctuation-out.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    boundaries = [index + 1 == len(result) or
                  float(result[index+1]["start"]) - float(item["end"]) >= 1.5
                  for index, item in enumerate(result)]
    input_path.write_text(json.dumps({"texts": [item["text"] for item in result],
                                      "break_after": boundaries}, ensure_ascii=False), "utf-8")
    try:
        completed = cancellation.run(job_id, [str(python), str(ROOT / "backend/punctuation_worker.py"),
                                              str(input_path), str(output_path)])
        if completed.returncode != 0:
            raise RuntimeError("Не удалось расставить знаки препинания: " + str(completed.stderr)[-1500:])
        texts = json.loads(output_path.read_text("utf-8"))
        if len(texts) != len(result):
            raise RuntimeError("Модель пунктуации вернула неполный результат")
        for item, text in zip(result, texts):
            if words(item["text"]) != words(text):
                raise RuntimeError("Пунктуация изменила распознанные слова; результат отклонён")
            item["text"] = text
            # Keep the same word timestamps; copy only case and punctuation.
            timed = item.get("words") or []
            tokens = re.findall(r"\S+", text)
            if len(timed) == len(tokens) and all(words(w.get("word", "")) == words(t)
                                                for w, t in zip(timed, tokens)):
                for word, token in zip(timed, tokens):
                    word["word"] = " " + token
        return result
    finally:
        input_path.unlink(missing_ok=True); output_path.unlink(missing_ok=True)
