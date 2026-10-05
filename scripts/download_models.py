from backend.transcriber import prepare_model_files


def report(message: str) -> None:
    print(message, flush=True)


if __name__ == "__main__":
    prepare_model_files(report)
    report("Обе модели загружены.")

