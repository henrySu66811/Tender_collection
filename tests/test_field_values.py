import unittest

from tender_app.field_values import amount_value, budget_value, date_value, notice_dates
from tender_app.extraction import extract_tender_fields
from tender_app.exporters import _record_payload
from tender_app.models import TenderRecord


class FieldValueTests(unittest.TestCase):
    def test_wuhan_regression(self):
        text = (
            "预算金额￥59.000000万元（人民币）联系人\n"
            "项目开标时间：2026-11-05｜项目监管地：武昌区|阅读次数：\n"
            "5、预算金额：59.000000(万元)\n"
            "三、获取招标文件\n"
            "1、时间：2026年10月16日至2026年10月22日，每天00:00至12:00\n"
            "四、提交投标文件截止时间、开标时间和地点\n"
            "1、开始时间：2026年10月16日00点00分（北京时间）\n"
            "2、截止时间：2026年11月05日09点30分（北京时间）\n"
            "3、地点：网上\n五、公告期限\n"
        )
        item = _record_payload(TenderRecord(content=text, budget="59.000000", deadline="2026-10-16 00:00"))
        self.assertEqual(item["BudgetAmt"], "59.000000万元")
        self.assertEqual(item["OpenDate"], "2026-11-05 09:30")
        self.assertEqual(item["BidEndDate"], "2026-11-05 09:30")
        self.assertEqual(item["ObtainEndDate"], "2026-10-22")
        self.assertEqual(item["content_text"], text)
        self.assertEqual(extract_tender_fields("", text)["deadline"], "2026-11-05 09:30")

    def test_budget_units(self):
        for text in ("预算金额：59(万元)", "预算金额（万元）：59", "采购预算：￥59万元"):
            self.assertEqual(budget_value(text), "59万元")
        self.assertEqual(amount_value("59（万元）"), "59万元")
        self.assertEqual(budget_value("最高限价：59万元"), "")

    def test_dates_not_invented(self):
        self.assertEqual(date_value("2026-02-30 09:00"), "")
        self.assertEqual(date_value("2026年11月5日"), "2026-11-05")
        self.assertEqual(date_value("2026-11-05 09:30:00｜阅读次数123"), "2026-11-05 09:30")

    def test_registration_is_not_submission(self):
        text = "报名截止时间：2026-10-22 17:00\n截止时间：2026-10-22 17:00"
        self.assertEqual(notice_dates(text)["bid_end_date"], "")
        self.assertNotIn("deadline", extract_tender_fields("", text))

    def test_start_only_is_not_end(self):
        dates = notice_dates("四、提交投标文件截止时间、开标时间和地点\n1、开始时间：2026-10-16 00:00\n五、其他\n截止时间：2026-10-17 00:00")
        self.assertEqual(dates["bid_end_date"], "")
        self.assertEqual(dates["open_date"], "")

    def test_distinct_dates(self):
        dates = notice_dates("投标截止时间：2026-11-05 09:00\n开标时间：2026-11-05 10:00")
        self.assertEqual(dates["bid_end_date"], "2026-11-05 09:00")
        self.assertEqual(dates["open_date"], "2026-11-05 10:00")
