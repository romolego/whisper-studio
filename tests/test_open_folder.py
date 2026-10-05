from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app


class ExplorerRevealTests(unittest.TestCase):
    @patch.object(app.subprocess, "Popen")
    def test_reveal_uses_explorer_select_with_quoted_absolute_path(
        self,
        popen,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "файл с пробелами.m4a"
            source.write_bytes(b"audio")

            app._reveal_in_explorer(source)

            command = popen.call_args.args[0]
            self.assertEqual(
                command,
                f'explorer.exe /select,"{source.resolve()}"',
            )
            self.assertEqual(
                popen.call_args.kwargs["creationflags"],
                app.subprocess.CREATE_NO_WINDOW,
            )


if __name__ == "__main__":
    unittest.main()
