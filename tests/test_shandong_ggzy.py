import unittest
from urllib.parse import parse_qs, urlsplit

from tender_app.crawlers.registry import get_site
from tender_app.crawlers.shandong_ggzy import (
    DETAIL_BODY_SELECTORS, SEARCH_URL, build_search_url, normalize_detail_url,
    notice_type_from_title, publication_date, search_date,
)


class ShandongGgzyTests(unittest.TestCase):
    def test_registered(self):
        site = get_site("shandong_ggzy")
        self.assertEqual(site.name, "山东省公共资源交易中心")
        self.assertEqual(site.entry_url, SEARCH_URL)

    def test_only_official_article_urls_are_allowed(self):
        self.assertEqual(
            normalize_detail_url("/art/2026/10/13/art_209488_11492347.html"),
            "",
        )
        self.assertEqual(
            normalize_detail_url("https://ggzyjyzx.shandong.gov.cn/art/2026/10/13/art_209501_1.html"),
            "https://ggzyjyzx.shandong.gov.cn/art/2026/10/13/art_209501_1.html",
        )
        self.assertEqual(normalize_detail_url("https://example.com/art/2026/1/1/art_209488_1.html"), "")

    def test_search_url_contains_keyword_page_and_compact_dates(self):
        url = build_search_url("研发招标 & 设备", 3, "2025-10-13", "2026-10-13")
        self.assertEqual(urlsplit(url).path, "/jsearchfront/search.do")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["q"], ["研发招标 & 设备"])
        self.assertEqual(query["p"], ["3"])
        self.assertEqual(query["begin"], ["20251013"])
        self.assertEqual(query["end"], ["20261013"])
        self.assertEqual(search_date("2026年7月9日"), "20260709")

    def test_date_and_notice_type(self):
        self.assertEqual(publication_date("发布时间：2026-10-03 12:30"), "2026-10-03")
        self.assertEqual(publication_date("2026年7月9日"), "2026-07-09")
        self.assertEqual(notice_type_from_title("某项目竞争性磋商公告"), "竞争性磋商公告")
        self.assertEqual(notice_type_from_title("某项目合同公示"), "合同公示")

    def test_supported_detail_body_structures(self):
        self.assertEqual(DETAIL_BODY_SELECTORS, (".zhengwen", "#zoom", "#noticeArea"))


if __name__ == "__main__":
    unittest.main()
