import tempfile
import unittest
from pathlib import Path

from wedownloader.config import load_dotenv


class ConfigTests(unittest.TestCase):
    def test_load_dotenv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("WECHAT_APP_ID='abc'\nWECHAT_APP_SECRET=def\n", encoding="utf-8")

            values = load_dotenv(path)

        self.assertEqual(values["WECHAT_APP_ID"], "abc")
        self.assertEqual(values["WECHAT_APP_SECRET"], "def")


if __name__ == "__main__":
    unittest.main()

