import unittest

from tender_app.crawlers.registry import get_site
from tender_app.crawlers.zycg import SEARCH_URL, normalize_notice_url, publication_date


class ZycgTests(unittest.TestCase):
    def test_registered(self):
        site = get_site("zycg")
        self.assertEqual(site.name, "中央政府采购网")
        self.assertEqual(site.entry_url, SEARCH_URL)

    def test_normalize_only_zycg_domains(self):
        self.assertEqual(
            normalize_notice_url("/freecms/site/zygjjgzfcgzx/ggxx/info/a.html?id=1"),
            "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/ggxx/info/a.html?id=1",
        )
        self.assertEqual(
            normalize_notice_url("http://mkt.zycg.gov.cn/mall-view/information/detail?noticeId=1"),
            "https://mkt.zycg.gov.cn/mall-view/information/detail?noticeId=1",
        )
        self.assertEqual(normalize_notice_url("https://example.com/a.html"), "")

    def test_publication_date(self):
        self.assertEqual(publication_date("发布时间 2026-10-03 12:00"), "2026-10-03")
        self.assertEqual(publication_date("2026年7月9日"), "2026-07-09")
        self.assertEqual(publication_date("暂无日期"), "")


if __name__ == "__main__":
    unittest.main()
