import json
import tempfile
import unittest
from pathlib import Path

from tender_app.exporters import build_output_payload, export_json
from tender_app.models import TenderRecord


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.record = TenderRecord(
            title="测试项目",
            purchaser="采购单位",
            agency="代理机构",
            region="山东省",
            industry="通讯电子",
            notice_type="招标公告",
            publish_date="2026-10-14",
            deadline="2026-10-30 09:00",
            budget="100万元",
            winner="中标单位",
            winning_amount="88万元",
            contact="张三",
            phone="010-12345678",
            summary="摘要",
            content="完整正文",
            source_url="https://example.test/notice/1",
        )

    def test_payload_matches_reference_structure(self):
        payload = build_output_payload(
            {"算力": [self.record]},
            site_key="qichacha",
            order_number="TENDERCHECK1",
        )
        self.assertEqual(list(payload), ["qichacha"])
        sample = payload["qichacha"]["samples"][0]
        self.assertEqual(
            list(sample),
            ["keyword", "status", "message", "order_number", "paging", "result"],
        )
        self.assertEqual(sample["keyword"], "算力")
        self.assertEqual(sample["status"], "200")
        self.assertEqual(
            sample["paging"],
            {"PageSize": 1, "PageIndex": 1, "TotalRecords": 1},
        )
        self.assertEqual(sample["result"]["VerifyResult"], 1)

        item = sample["result"]["Data"][0]
        self.assertEqual(
            list(item),
            [
                "Id", "Title", "ProjectNo", "ChannelName", "Province", "City",
                "IndustryDesc", "BudgetAmt", "PublishDate", "OpenDate",
                "ObtainEndDate", "BidInviUnitList", "WinBidUnitList",
                "AgentUnitList", "BidProgressList", "BidEndDate", "ContentUrl",
                "ContractEndTime", "content_text", "content_status",
            ],
        )
        self.assertEqual(item["Title"], "测试项目")
        self.assertEqual(item["Id"], "1")
        self.assertEqual(item["content_text"], "完整正文")
        self.assertEqual(item["content_status"], "ok")
        self.assertEqual(item["OpenDate"], "")
        self.assertEqual(item["BidEndDate"], "2026-10-30 09:00")
        self.assertEqual(item["BidInviUnitList"][0]["Name"], "采购单位")
        self.assertIsNone(item["BidInviUnitList"][0]["Contact"])
        self.assertEqual(item["WinBidUnitList"][0]["WinBidAmt"], "88万元")
        self.assertEqual(item["AgentUnitList"][0]["Name"], "代理机构")
        self.assertIsNone(item["AgentUnitList"][0]["Contact"])

    def test_json_supports_multiple_keywords_and_empty_results(self):
        with tempfile.TemporaryDirectory() as folder:
            path = export_json(
                {"招标": [self.record], "采购": []},
                str(Path(folder) / "result"),
                site_key="shandong_ggzy",
            )
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(list(payload), ["shandong_ggzy"])
            samples = payload["shandong_ggzy"]["samples"]
            self.assertEqual(
                [sample["keyword"] for sample in samples],
                ["招标", "采购"],
            )
            self.assertEqual(samples[1]["result"]["Data"], [])
            self.assertEqual(samples[1]["paging"]["TotalRecords"], 0)
            self.assertEqual(samples[1]["result"]["VerifyResult"], 0)

    def test_contract_fields_are_completed_from_collected_content(self):
        record = TenderRecord(
            title="合同公示",
            region="山东省",
            notice_type="合同公示",
            content=(
                "三、采购项目编码：SDGP370000000202602007880\n"
                "五、合同主体\n"
                "采购人：山东省交通运输厅\n"
                "地    址：山东省济南市舜耕路17号\n"
                "联系方式：13553165101\n"
                "供应商（乙方）：山东路科有限公司\n"
                "地    址：山东省济南市高新区新泺大街\n"
                "联系方式：15552879996\n"
                "六、合同主要信息\n"
                "主要标的名称\t合同金额（万元）\n"
                "地图服务\t7.7\n"
            ),
        )
        item = build_output_payload(
            {"招标": [record]}, site_key="qichacha", order_number="TENDERCHECK1"
        )["qichacha"]["samples"][0]["result"]["Data"][0]
        self.assertEqual(item["ProjectNo"], "SDGP370000000202602007880")
        self.assertEqual(item["City"], "济南市")
        self.assertEqual(item["BudgetAmt"], "")
        self.assertEqual(item["BidInviUnitList"][0]["Name"], "山东省交通运输厅")
        self.assertEqual(item["BidInviUnitList"][0]["TelNo"], "13553165101")
        self.assertEqual(item["WinBidUnitList"][0]["Name"], "山东路科有限公司")
        self.assertEqual(item["WinBidUnitList"][0]["WinBidAmt"], "7.7万元")
        self.assertEqual(item["WinBidUnitList"][0]["TelNo"], "15552879996")
        self.assertEqual(item["BidProgressList"], ["中标公告", "合同"])

    def test_dates_are_kept_separate_by_meaning(self):
        record = TenderRecord(
            title="采购公告",
            notice_type="公开招标公告",
            deadline="2026-11-02 09:30",
            content=(
                "获取招标文件截止时间：2026-10-20 17:00\n"
                "开标时间：2026-11-02 10:00\n"
                "投标截止时间：2026-11-02 09:30\n"
            ),
        )
        item = build_output_payload(
            {"采购": [record]}, site_key="qichacha", order_number="TENDERCHECK1"
        )["qichacha"]["samples"][0]["result"]["Data"][0]
        self.assertEqual(item["OpenDate"], "2026-11-02 10:00")
        self.assertEqual(item["ObtainEndDate"], "2026-10-20 17:00")
        self.assertEqual(item["BidEndDate"], "2026-11-02 09:30")
        self.assertEqual(item["BidProgressList"], ["招标公告", "招标"])

    def test_multiline_notice_content_fills_explicit_fields(self):
        record = TenderRecord(
            title="实验系统采购项目",
            region="山东省",
            publish_date="2026-10-13",
            budget="6,300,000.00元",
            content=(
                "公开招标公告\n"
                "项目编号：SDGP370000000202602007803\n"
                "采购标的\t数量（单位）\t中小企业划分标准所属行业\n"
                "实验系统\t1(宗)\t软件和信息技术服务业\n"
                "三、获取招标文件\n"
                "时间：2026-10-13 至 2026-10-20，法定节假日除外\n"
                "四、提交投标文件截止时间、开标时间和地点\n"
                "2026-11-05 09:00:00（北京时间）\n"
                "1.采购人信息\n"
                "名称：鲁东大学\n"
                "联系方式：0535-6697527\n"
                "2.采购代理机构信息（如有）\n"
                "名称：山东鑫睿达项目管理有限公司\n"
                "联系方式：13001705253\n"
                "3.项目联系方式\n"
                "项目联系人：刘晓娇\n"
                "电话：13001705253\n"
            ),
        )
        item = build_output_payload(
            {"采购": [record]}, site_key="shandong_ggzy", order_number="T1"
        )["shandong_ggzy"]["samples"][0]["result"]["Data"][0]
        self.assertEqual(item["ChannelName"], "公开招标公告")
        self.assertEqual(item["IndustryDesc"], "软件和信息技术服务业")
        self.assertEqual(item["OpenDate"], "2026-11-05 09:00")
        self.assertEqual(item["ObtainEndDate"], "2026-10-20")
        self.assertEqual(item["BidEndDate"], "2026-11-05 09:00")
        self.assertEqual(item["BidInviUnitList"][0]["Name"], "鲁东大学")
        self.assertEqual(
            item["AgentUnitList"][0],
            {
                "KeyNo": "",
                "Name": "山东鑫睿达项目管理有限公司",
                "Contact": "刘晓娇",
                "TelNo": "13001705253",
                "CreditCode": "",
            },
        )

    def test_zycg_notice_content_fills_structured_fields(self):
        record = TenderRecord(
            title="湖北省气象数据中心算力扩充采购项目招标公告",
            publish_date="2026-10-14",
            content=(
                "湖北省气象数据中心算力扩充采购项目招标公告\n"
                "一、项目基本情况\n"
                "1．项目编号：GC-HGX261025\n"
                "3．预算金额：1040.200000万元\n"
                "四、提交投标文件\n"
                "1．提交（上传）投标文件截止时间：\n"
                "2026年11月5日09时00分（北京时间）\n"
                "五、开标\n"
                "1.时间：2026年11月5日09时00分（北京时间）\n"
                "八、凡对本次招标提出询问，请按以下方式联系\n"
                "1．采购人信息\n"
                "名\u00a0\u00a0称：湖北省气象数据中心\n"
                "地\u00a0\u00a0址：湖北省武汉市洪山区东湖东路3号\n"
                "联系方式：027-67848019\n"
                "2．采购执行机构信息\n"
                "名\u00a0\u00a0称：中央国家机关政府采购中心\n"
                "联系方式：详见http://www.zycg.gov.cn/home/contactus\n"
                "3．项目联系方式\n"
                "文件联系人及电话：徐婧婧\u00a0010-83084953\u00a0\u00a0王震\u00a0010-55602563\n"
                "九、附件\n"
            ),
        )
        item = build_output_payload(
            {"算力": [record]}, site_key="zycg", order_number="T1"
        )["zycg"]["samples"][0]["result"]["Data"][0]

        self.assertEqual(item["ProjectNo"], "GC-HGX261025")
        self.assertEqual(item["ChannelName"], "招标公告")
        self.assertEqual(item["Province"], "湖北省")
        self.assertEqual(item["City"], "武汉市")
        self.assertEqual(item["BudgetAmt"], "1040.200000万元")
        self.assertEqual(item["OpenDate"], "2026-11-05 09:00")
        self.assertEqual(item["BidEndDate"], "2026-11-05 09:00")
        self.assertEqual(item["BidInviUnitList"][0]["Name"], "湖北省气象数据中心")
        self.assertEqual(item["BidInviUnitList"][0]["TelNo"], "027-67848019")
        self.assertEqual(item["AgentUnitList"][0]["Name"], "中央国家机关政府采购中心")
        self.assertEqual(item["AgentUnitList"][0]["Contact"], "徐婧婧、王震")
        self.assertEqual(item["AgentUnitList"][0]["TelNo"], "010-83084953、010-55602563")
        self.assertEqual(item["BidProgressList"], ["招标公告", "招标"])
        self.assertEqual(item["WinBidUnitList"], [])

        municipality_record = TenderRecord(
            title="云计算服务采购项目招标公告",
            content="招标公告\n1．采购人信息\n地  址：北京市西城区宣武门西大街26号\n",
        )
        municipality_item = build_output_payload(
            {"云计算": [municipality_record]}, site_key="zycg", order_number="T2"
        )["zycg"]["samples"][0]["result"]["Data"][0]
        self.assertEqual(municipality_item["Province"], "北京市")
        self.assertEqual(municipality_item["City"], "北京市")


if __name__ == "__main__":
    unittest.main()
