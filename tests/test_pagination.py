import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tender_app.access import AccessStore
from tender_app.crawlers.ccgp import CcgpCrawler
from tender_app.crawlers.shandong_ggzy import ShandongGgzyCrawler
from tender_app.pagination import PageProgress


class PaginationTests(unittest.TestCase):
    def _crawler(self, cls, folder):
        crawler = cls(MagicMock(), MagicMock(), Path(folder), policy=MagicMock(),
                      cache=MagicMock(), site_key="test")
        crawler.cache.cached.return_value = True
        crawler.cache.load_progress.return_value = {
            "next_page": 1, "last_page": None, "pending_pages": [],
            "pending_details": [],
        }
        crawler.active_keyword = "招标"
        return crawler

    def test_ccgp_continues_past_old_page_limit_when_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            crawler = self._crawler(CcgpCrawler, folder)
            pages = []
            crawler.open_search = lambda _kw, number, _start, _end: pages.append(number)
            crawler.current_candidates = lambda: ([{
                "url": f"https://example.test/{pages[-1]}",
                "title": "公告", "publish_date": "2026-10-14",
            }] if pages[-1] <= 70 else [])
            with patch("tender_app.crawlers.ccgp.reported_total", return_value=None):
                crawler.search("招标", start_date="2026-10-14", end_date="2026-10-14")
            self.assertEqual(pages[-1], 71)
            self.assertEqual(len(crawler.found_urls), 70)

    def test_shandong_queues_repeated_pages_and_avoids_endless_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            crawler = self._crawler(ShandongGgzyCrawler, folder)
            pages = []
            crawler.open_search = lambda _kw, number, _start, _end: pages.append(number)
            crawler.current_candidates = lambda: [{
                "url": "https://example.test/same",
                "title": "公告", "publish_date": "2026-10-14",
            }]
            with patch("tender_app.crawlers.shandong_ggzy.reported_total", return_value=None):
                crawler.search("招标", start_date="2026-10-14", end_date="2026-10-14")
            self.assertEqual(pages, [1, 2, 3, 4])
            self.assertEqual(crawler.cache.load_progress.return_value["pending_pages"],
                             [2, 3, 4])

    def test_each_row_has_page_position_and_outcome(self):
        for crawler_class, module_name in (
            (CcgpCrawler, "ccgp"), (ShandongGgzyCrawler, "shandong_ggzy"),
        ):
            with self.subTest(site=module_name), tempfile.TemporaryDirectory() as folder:
                crawler = self._crawler(crawler_class, folder)
                pages = []
                crawler.open_search = lambda _kw, number, _start, _end: pages.append(number)
                crawler.current_candidates = lambda: ([
                    {"url": f"https://example.test/{number}", "title": "同名公告",
                     "publish_date": "2026-10-14"}
                    for number in range(20)
                ] if pages[-1] == 1 else [])
                with patch(f"tender_app.crawlers.{module_name}.reported_total", return_value=20):
                    crawler.search("招标", start_date="2026-10-14", end_date="2026-10-14")
                rows = [item for item in crawler.audit if item.get("页内序号")]
                self.assertEqual(len(rows), 20)
                self.assertEqual([item["页内序号"] for item in rows], list(range(1, 21)))
                self.assertTrue(all(item["状态"] == "跨次缓存命中，跳过详情" for item in rows))
                self.assertEqual(crawler.cache.cached.call_count, 20)

    def test_timeout_page_is_queued_and_next_page_continues(self):
        for crawler_class, module_name in (
            (CcgpCrawler, "ccgp"), (ShandongGgzyCrawler, "shandong_ggzy"),
        ):
            with self.subTest(site=module_name), tempfile.TemporaryDirectory() as folder:
                crawler = self._crawler(crawler_class, folder)
                pages = []
                def open_page(_kw, number, _start, _end):
                    pages.append(number)
                    if number == 2:
                        raise PlaywrightTimeoutError("navigation timeout")
                crawler.open_search = open_page
                crawler.current_candidates = lambda: [{
                    "url": f"https://example.test/{pages[-1]}",
                    "title": "公告", "publish_date": "2026-10-14",
                }]
                with patch(f"tender_app.crawlers.{module_name}.reported_total", return_value=60), \
                     patch("tender_app.pagination.time.sleep"):
                    crawler.search("招标", start_date="2026-10-14", end_date="2026-10-14")
                self.assertIn(3, pages)
                state = crawler.cache.load_progress.return_value
                self.assertEqual(state["pending_pages"], [2])
                self.assertEqual(state["next_page"], 4)

    def test_progress_is_persisted_by_site_keyword_and_dates(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AccessStore(Path(folder) / "cache.sqlite3")
            progress = PageProgress(store, "ccgp", "招标", "2026-10-15", "2026-10-15")
            progress.set_total(60)
            progress.failed(2)
            progress.failed_detail(2, {"url": "https://example.test/a", "title": "公告"})
            resumed = PageProgress(store, "ccgp", "招标", "2026-10-15", "2026-10-15")
            self.assertEqual(resumed.data["pending_pages"], [2])
            self.assertEqual(resumed.data["pending_details"][0]["url"],
                             "https://example.test/a")
            self.assertEqual(resumed.target(), (3, False))
            resumed.resolved_detail("https://example.test/a")
            self.assertEqual(resumed.data["pending_details"], [])
            other_date = PageProgress(store, "ccgp", "招标", "2026-10-16", "2026-10-16")
            self.assertEqual(other_date.target(), (1, False))
            store.close()

    def test_navigation_timeout_can_use_already_rendered_results(self):
        for crawler_class, module_name in (
            (CcgpCrawler, "ccgp"), (ShandongGgzyCrawler, "shandong_ggzy"),
        ):
            with self.subTest(site=module_name), tempfile.TemporaryDirectory() as folder:
                crawler = self._crawler(crawler_class, folder)
                crawler.page.goto.side_effect = PlaywrightTimeoutError("navigation timeout")
                crawler.page.locator.return_value.count.return_value = 1
                crawler.page.locator.return_value.first.is_visible.return_value = True
                crawler.page.locator.return_value.inner_text.return_value = ""
                crawler.page.title.return_value = ""
                if module_name == "ccgp":
                    crawler.page.url = crawler.build_search_url(
                        "招标", 1, "2026-10-15", "2026-10-15")
                else:
                    from tender_app.crawlers.shandong_ggzy import build_search_url
                    crawler.page.url = build_search_url(
                        "招标", 1, "2026-10-15", "2026-10-15")
                crawler.open_search("招标", 1, "2026-10-15", "2026-10-15")


if __name__ == "__main__":
    unittest.main()
