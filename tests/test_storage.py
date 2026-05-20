import json
import stat
import tempfile
import unittest
from pathlib import Path

from wedownloader.storage import write_json_atomic


class StorageTests(unittest.TestCase):
    def test_write_json_atomic_default_permissions_are_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manifest.json"

            write_json_atomic(path, {"ok": True})

            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(mode, 0o644)

    def test_write_json_atomic_private_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "secret.json"

            write_json_atomic(path, {"token": "abc"}, private=True)

            data = json.loads(path.read_text(encoding="utf-8"))
            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(data["token"], "abc")
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
