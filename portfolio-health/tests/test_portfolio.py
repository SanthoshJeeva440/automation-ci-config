import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "check_portfolio.py"
SPEC = importlib.util.spec_from_file_location("check_portfolio", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ConfigTests(unittest.TestCase):
    def test_placeholder_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yml"
            path.write_text("portfolio:\n  url: 'https://YOUR-PORTFOLIO-URL'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                module.load_config(path)

    def test_valid_url_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yml"
            path.write_text("portfolio:\n  url: 'https://example.com'\n", encoding="utf-8")
            self.assertEqual(module.load_config(path)["portfolio"]["url"], "https://example.com")


if __name__ == "__main__":
    unittest.main()
