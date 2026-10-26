"""Install the pinned Russian punctuation classifier, without runtime changes."""
from backend.punctuation import MODEL_DIR, MODEL_REPO, MODEL_REVISION, MODEL_SHA256, MODEL_SIZE
from scripts.model_download import DownloadSpec, download_weights


def install(proxy: str = ""):
    import requests
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    def session():
        client = requests.Session(); client.trust_env = False
        if proxy: client.proxies.update({"http": proxy, "https": proxy})
        return client
    with session() as client:
        for filename in ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"):
            response = client.get(f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{filename}", timeout=60)
            response.raise_for_status(); (MODEL_DIR / filename).write_bytes(response.content)
    download_weights(session, spec=DownloadSpec(MODEL_REPO, MODEL_REVISION, MODEL_SIZE,
                                               MODEL_SHA256, MODEL_DIR, "model.safetensors"))
    print("Russian punctuation classifier verified", flush=True)


if __name__ == "__main__":
    install()
