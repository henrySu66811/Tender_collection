import unittest
from urllib.parse import parse_qs, urlsplit

from tender_app.crawlers.ccgp import (
    SEARCH_URL, CcgpCrawler, normalize_detail_url, notice_type_from_title,
    publication_date, result_publication_date,
)
from tender_app.crawlers.registry import get_site


class CcgpTests(unittest.TestCase):
    def test_registered(self):
        site = get_site("ccgp")
        self.assertEqual(site.name, "中国政府采购网")
        self.assertEqual(site.entry_url, SEARCH_URL)
        self.assertEqual(urlsplit(site.entry_url).hostname, "search.ccgp.gov.cn")

    def test_search_url_contains_keyword_dates_and_page(self):
        url = CcgpCrawler.build_search_url("物业管理 & 服务", 2, "2025-10-13", "2026-10-13")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["kw"], ["物业管理 & 服务"])
        self.assertEqual(query["page_index"], ["2"])
        self.assertEqual(query["start_time"], ["2025:10:13"])
        self.assertEqual(query["end_time"], ["2026:10:13"])

    def test_only_ccgp_procurement_details_are_allowed(self):
        source = "http://www.ccgp.gov.cn/cggg/dfgg/gkzb/202610/t20261013_27478535.htm"
        self.assertEqual(
            normalize_detail_url(source),
            "http://www.ccgp.gov.cn/cggg/dfgg/gkzb/202610/t20261013_27478535.htm",
        )
        self.assertEqual(
            normalize_detail_url(
                "https://www.ccgp.gov.cn/cggg/dfgg/qtgg/202610/t20261013_27480548.htm"
            ),
            "http://www.ccgp.gov.cn/cggg/dfgg/qtgg/202610/t20261013_27480548.htm",
        )
        self.assertEqual(normalize_detail_url("https://example.com/cggg/dfgg/gkzb/202610/t1_2.htm"), "")
        self.assertEqual(normalize_detail_url("https://www.ccgp.gov.cn/xwdt/1.htm"), "")

    def test_date_and_notice_type(self):
        self.assertEqual(publication_date("2026年10月13日 14:06"), "2026-10-13")
        self.assertEqual(publication_date("2026-7-9"), "2026-07-09")
        self.assertEqual(notice_type_from_title("某项目竞争性磋商公告"), "竞争性磋商公告")
        self.assertEqual(notice_type_from_title("某项目中标公告"), "中标公告")

    def test_result_date_ignores_project_year_in_title(self):
        title = "2025年设备采购项目招标公告"
        row = title + "\n发布时间：2026-10-15 16:20 | 采购人：某单位"
        self.assertEqual(result_publication_date(row, title), "2026-10-15")


if __name__ == "__main__":
    unittest.main()
