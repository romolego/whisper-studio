"""Pinned downloads publish complete verified weights only, without network access."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.model_download import DownloadSpec, download_weights


class ModelDownloadTests(unittest.TestCase):
    def test_only_a_complete_file_with_matching_hash_is_published(self):
        data = b"model-weights"

        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def head(self, *args, **kwargs):
                return SimpleNamespace(url="https://example.invalid/weights", raise_for_status=lambda: None)
            def get(self, *args, **kwargs):
                return SimpleNamespace(
                    headers={"Content-Range": f"bytes 0-{len(data)-1}/{len(data)}"},
                    content=data, raise_for_status=lambda: None,
                )

        for expected, succeeds in ((data, True), (b"different", False)):
            with self.subTest(valid_hash=succeeds), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                spec = DownloadSpec("example/model", "pinned", len(data),
                                    hashlib.sha256(expected).hexdigest(), root, "model.safetensors")
                if succeeds:
                    target = download_weights(Session, spec=spec, workers=1)
                    self.assertEqual(target.read_bytes(), data)
                    self.assertFalse(list(root.glob("*.partial")))
                    self.assertFalse(list(root.glob("*.ranges.json")))
                else:
                    with self.assertRaisesRegex(RuntimeError, "checksum"):
                        download_weights(Session, spec=spec, workers=1)
                    self.assertFalse((root / "model.safetensors").exists())

    def test_server_ignoring_ranges_cannot_publish_weights(self):
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def head(self, *args, **kwargs):
                return SimpleNamespace(url="https://example.invalid/weights", raise_for_status=lambda: None)
            def get(self, *args, **kwargs):
                return SimpleNamespace(headers={}, content=b"abc", raise_for_status=lambda: None)

        with tempfile.TemporaryDirectory() as folder, patch("scripts.model_download.time.sleep"):
            spec = DownloadSpec("example/model", "pinned", 3,
                                hashlib.sha256(b"abc").hexdigest(), Path(folder))
            with self.assertRaisesRegex(RuntimeError, "incorrect byte range"):
                download_weights(Session, spec=spec, workers=1)
            self.assertFalse((Path(folder) / "model.bin").exists())
