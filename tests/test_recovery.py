import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from tender_app.access import AccessStore
from tender_app.app import _configured_date, _recover_pending_runs
from tender_app.crawlers.base import reported_total


class RecoveryTests(unittest.TestCase):
    def test_cache_and_output_record_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run_dir = root / "results" / "run_ccgp"
            run_dir.mkdir(parents=True)
            db = root / "cache" / "access.sqlite3"
            record = {
                "title": "采购公告", "content": "完整公告正文", "source_url": "https://example.test/a",
            }
            store = AccessStore(db)
            store.save_completed("ccgp", "key1", record, run_dir, "招标")
            self.assertTrue(store.cached("ccgp", "key1", 3600))
            store.close()

            restarted = AccessStore(db)
            self.assertEqual(restarted.pending_runs("ccgp"), [str(run_dir)])
            _recover_pending_runs(restarted, "ccgp")
            output = json.loads((run_dir / "招投标信息.json").read_text(encoding="utf-8"))
            data = output["ccgp"]["samples"][0]["result"]["Data"]
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["Title"], "采购公告")
            self.assertEqual(restarted.pending_runs("ccgp"), [])
            restarted.close()

    def test_auto_dates_and_displayed_total(self):
        today = date(2026, 10, 15)
        self.assertEqual(_configured_date("yesterday", today), date(2026, 10, 14))
        self.assertEqual(_configured_date("today", today), today)
        self.assertEqual(_configured_date("2026-10-01", today), date(2026, 10, 1))
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "找到 1,073 条结果"
        self.assertEqual(reported_total(page), 1073)

    def test_failed_export_remains_recoverable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run_dir = root / "run_ccgp"
            run_dir.mkdir()
            store = AccessStore(root / "access.sqlite3")
            store.save_completed(
                "ccgp", "key2", {"title": "测试", "content": "正文"}, run_dir, "招标",
            )
            with patch("tender_app.app.TenderCollectionState.export_json", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    _recover_pending_runs(store, "ccgp")
            self.assertEqual(store.pending_runs("ccgp"), [str(run_dir)])
            _recover_pending_runs(store, "ccgp")
            self.assertEqual(store.pending_runs("ccgp"), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
