import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tender_app.app import (
    _validated_sites,
    open_login_entry,
)


class LoginEntryTests(unittest.TestCase):
    def test_falls_back_after_http_error(self):
        page = MagicMock()
        page.goto.side_effect = [
            SimpleNamespace(status=521),
            SimpleNamespace(status=200),
        ]
        page.url = "https://s.example.test/s?queryword=x"
        with tempfile.TemporaryDirectory() as directory:
            open_login_entry(
                page,
                "https://www.example.test/",
                Path(directory),
                fallback_url="https://s.example.test/s?queryword=x",
            )
        self.assertEqual(page.goto.call_count, 2)

    def test_reports_all_failed_entries(self):
        page = MagicMock()
        page.goto.side_effect = [
            SimpleNamespace(status=521),
            SimpleNamespace(status=503),
        ]
        page.url = "https://s.example.test/s"
        page.title.return_value = ""
        page.locator.return_value.inner_text.return_value = ""
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "HTTP 521"):
                open_login_entry(
                    page,
                    "https://www.example.test/",
                    run_dir,
                    fallback_url="https://s.example.test/s",
                )
            self.assertTrue((run_dir / "启动页面诊断.json").exists())


class SiteConcurrencyTests(unittest.TestCase):
    def test_different_sites_are_allowed(self):
        self.assertEqual(
            _validated_sites(["zycg", "ccgp"]),
            ["zycg", "ccgp"],
        )

    def test_duplicate_site_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "同一站点不能启动多个进程"):
            _validated_sites(["zycg", "zycg"])


if __name__ == "__main__":
    unittest.main()
