"""CPU worker in the existing Torch environment; all models are local."""
import json
import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.punctuation import MODEL_DIR, LEXEMES, apply_labels
import torch
from transformers import AutoTokenizer, AutoModelForTokenClassification


def main():
    torch.set_num_threads(4)
    payload = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    texts = payload["texts"] if isinstance(payload, dict) else payload
    boundaries = payload.get("break_after", [True] * len(texts)) if isinstance(payload, dict) else [True] * len(texts)
    spans = [list(LEXEMES.finditer(text)) for text in texts]
    tokens = [match.group() for group in spans for match in group]
    if not tokens:
        Path(sys.argv[2]).write_text(json.dumps(texts, ensure_ascii=False), "utf-8"); return
    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR), local_files_only=True,
                                              trust_remote_code=False, use_fast=True)
    model = AutoModelForTokenClassification.from_pretrained(str(MODEL_DIR), local_files_only=True,
                                                            trust_remote_code=False, use_safetensors=True).eval()
    encoded = tokenizer(tokens, is_split_into_words=True, max_length=256, stride=64,
                        truncation=True, return_overflowing_tokens=True, padding=True, return_tensors="pt")
    predictions = [0] * len(tokens); scores = [-1] * len(tokens)
    windows = len(encoded["input_ids"])
    with torch.inference_mode():
        for start in range(0, windows, 4):
            inputs = {key: encoded[key][start:start+4] for key in ("input_ids", "attention_mask", "token_type_ids") if key in encoded}
            output = model(**inputs).logits.argmax(-1).tolist()
            for local, labels in enumerate(output):
                ids = encoded.word_ids(start + local)
                for position, word_id in enumerate(ids):
                    if word_id is None or (position+1<len(ids) and ids[position+1]==word_id):
                        continue
                    score = min(position, len(ids)-position-1)
                    if score > scores[word_id]:
                        scores[word_id] = score; predictions[word_id] = labels[position]
    if any(score < 0 for score in scores):
        raise RuntimeError("Punctuation classifier did not cover the complete recording")
    result = []; index = 0
    for text, group, boundary in zip(texts, spans, boundaries):
        labels = predictions[index:index+len(group)]
        index += len(group)
        sentence_start = not result or boundaries[len(result)-1] or result[-1].rstrip().endswith((".", "!", "?", "…"))
        rendered = apply_labels(text, labels, sentence_start=sentence_start)
        if boundary and rendered.rstrip() and rendered.rstrip()[-1] not in ".!?…":
            rendered = rendered.rstrip().rstrip(",;:") + "."
        result.append(rendered)
    if result:
        last = result[-1].rstrip()
        if last and last[-1] not in ".!?…":
            result[-1] = last.rstrip(",;:") + "."
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), "utf-8")


if __name__ == "__main__":
    main()
