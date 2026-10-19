"""Record normalization, deduplication, and export helpers."""
from datetime import datetime
from typing import Any

from .exporters import export_json
from .models import TenderRecord


class TenderCollectionState:
    """Hold normalized records and the operations used by the main workflow."""

    def __init__(self) -> None:
        self.tender_records: list[TenderRecord] = []

    def _normalize_record(self, raw_record: dict[str, Any]) -> TenderRecord:
        """Convert a crawler result into the standard record format."""
        return TenderRecord(
            title=self._get_value(
                raw_record, "title", "project_name", "项目名称", "标题"
            ),
            purchaser=self._get_value(
                raw_record,
                "purchaser",
                "buyer",
                "tenderer",
                "招标单位",
                "采购人",
            ),
            agency=self._get_value(
                raw_record, "agency", "代理机构", "招标代理"
            ),
            region=self._get_value(raw_record, "region", "location", "地区"),
            industry=self._get_value(
                raw_record, "industry", "category", "行业", "项目类型"
            ),
            notice_type=self._get_value(
                raw_record, "notice_type", "type", "公告类型"
            ),
            publish_date=self._get_value(
                raw_record, "publish_date", "date", "发布时间"
            ),
            deadline=self._get_value(
                raw_record, "deadline", "end_date", "截止时间"
            ),
            budget=self._get_value(raw_record, "budget", "预算", "预算金额"),
            winner=self._get_value(
                raw_record, "winner", "winning_company", "中标单位"
            ),
            winning_amount=self._get_value(
                raw_record, "winning_amount", "中标金额"
            ),
            contact=self._get_value(
                raw_record, "contact", "contact_person", "联系人"
            ),
            phone=self._get_value(
                raw_record, "phone", "telephone", "联系电话"
            ),
            summary=self._get_value(raw_record, "summary", "公告摘要"),
            content=self._get_value(raw_record, "content", "公告正文"),
            source_name=self._get_value(
                raw_record, "source_name", "source", "来源网站"
            ),
            source_url=self._get_value(
                raw_record, "source_url", "url", "link", "原文链接"
            ),
            collected_at=datetime.now().isoformat(timespec="seconds"),
        )

    @staticmethod
    def _get_value(record: dict[str, Any], *keys: str) -> str:
        """Return the first non-empty value matching the candidate keys."""
        for key in keys:
            value = record.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _deduplicate_records(
        records: list[TenderRecord],
    ) -> list[TenderRecord]:
        """Deduplicate by source URL, or by identifying content fields."""
        unique_records: list[TenderRecord] = []
        seen_keys = set()

        for record in records:
            if record.source_url:
                unique_key = ("url", record.source_url)
            else:
                unique_key = (
                    "content",
                    record.title,
                    record.purchaser,
                    record.publish_date,
                )

            if unique_key in seen_keys:
                continue
            seen_keys.add(unique_key)
            unique_records.append(record)

        return unique_records

    def export_json(
        self,
        output_path: str,
        records_by_keyword: dict[str, list[TenderRecord]],
        *,
        site_key: str,
    ) -> str:
        """Export records using the configured site's JSON root key."""
        return export_json(
            records_by_keyword,
            output_path,
            site_key=site_key,
        )
