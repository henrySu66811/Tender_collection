import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .models import TenderRecord
from .field_values import amount_value, budget_value, date_value, notice_dates


def _first_match(text: str, *patterns: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.M | re.S)
        if match:
            return match.group(1).strip(" \t：:；;")
    return ""


def _entity_contact(
    text: str,
    start_pattern: str,
    end_pattern: str,
) -> tuple[str, str, str]:
    block = _first_match(text, rf"{start_pattern}(.*?)(?={end_pattern}|\Z)")
    if not block:
        return "", "", ""
    name = _first_match(block, r"^\s*([^\n]+)")
    contact = _first_match(block, r"(?:联系人|项目联系人)\s*[：:]\s*([^\n]+)")
    phone = _first_match(block, r"联系方式\s*[：:]\s*([^\n]+)")
    return name, contact, phone


def _table_value(text: str, label_pattern: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines[:-1]):
        headers = [part.strip() for part in line.split("\t")]
        for column, header in enumerate(headers):
            if re.search(label_pattern, header):
                values = [part.strip() for part in lines[index + 1].split("\t")]
                if column < len(values):
                    return values[column]
    return ""


def _section(text: str, start_pattern: str, end_pattern: str) -> str:
    return _first_match(text, rf"{start_pattern}(.*?)(?={end_pattern}|\Z)")


def _section_unit(text: str, section_name: str, next_section: str) -> dict[str, str]:
    block = _section(text, section_name, next_section)
    phone_text = _first_match(
        block,
        r"(?:联系方式|联系电话|电\s*话)\s*[：:]\s*([^\n]+)",
    )
    phones = re.findall(r"(?:0\d{2,3}-\d{7,8}|1[3-9]\d{9})", phone_text)
    file_contacts = re.findall(
        r"([\u4e00-\u9fff·]{2,20})\s+(0\d{2,3}-\d{7,8}|1[3-9]\d{9})",
        _first_match(block, r"文件联系人及电话\s*[：:]\s*([^\n]+)"),
    )
    return {
        "name": _first_match(block, r"名\s*称\s*[：:]\s*([^\n]+)"),
        "contact": (
            _first_match(block, r"(?:项目联系人|联系人)\s*[：:]\s*([^\n]+)")
            or "、".join(name for name, _ in file_contacts)
        ),
        "phone": "、".join(phones) or "、".join(phone for _, phone in file_contacts),
    }


def _labeled_unit(text: str, label_pattern: str) -> dict[str, str]:
    matches = list(re.finditer(rf"{label_pattern}\s*[：:]\s*([^\n，,]+)", text))
    for index in range(len(matches) - 1, -1, -1):
        match = matches[index]
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end]
        contact = _first_match(block, r"(?:项目联系人|联系人)\s*[：:]\s*([^\n]+)")
        phone = _first_match(block, r"(?:联系方式|联系电话|电\s*话)\s*[：:]\s*([^\n]+)")
        if contact or phone:
            return {
                "name": match.group(1).strip(),
                "contact": contact,
                "phone": phone,
            }
    return {"name": "", "contact": "", "phone": ""}


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _award_table(text: str) -> tuple[str, str]:
    """Read common vertically rendered award-result tables."""
    lines = _nonempty_lines(text)
    for index, line in enumerate(lines):
        if "中标供应商名称" not in line and "成交供应商名称" not in line:
            continue
        amount_header = next(
            (
                pos
                for pos in range(index + 1, min(index + 5, len(lines)))
                if re.search(r"(?:中标|成交)金额", lines[pos])
            ),
            None,
        )
        if amount_header is None:
            continue
        value_start = amount_header + 1
        # Tables commonly include: package number, supplier, address, amount.
        values = lines[value_start:value_start + 4]
        if len(values) >= 4:
            amount = values[3]
            if "万元" in lines[amount_header] and not amount.endswith("万元"):
                amount += "万元"
            return values[1], amount
    return "", ""


def _notice_type(text: str) -> str:
    first_lines = "\n".join(_nonempty_lines(text)[:8])
    for value in (
        "中标结果公告", "成交结果公告", "中标公告", "成交公告",
        "公开招标公告", "招标公告", "竞争性谈判公告", "竞争性磋商公告", "询价公告",
        "更正公告", "变更公告", "终止公告", "合同公示", "合同公告",
    ):
        if value in first_lines:
            return value
    return ""


def _content_fallbacks(record: TenderRecord) -> dict[str, str]:
    """Extract output-only fields from text already collected by an adapter."""
    text = record.content or record.summary
    project_no = _first_match(
        text,
        r"(?:采购项目编码|采购项目编号|项目编号|招标编号)\s*[：:]\s*([^\n]+)",
        r"合同编号\s*[：:]\s*([^\n]+)",
    )
    city = _first_match(
        text,
        r"(?:地\s*址|地址)\s*[：:]\s*[^\n]*?([\u4e00-\u9fff]{2,10}市)",
        r"([\u4e00-\u9fff]{2,10}市)",
    )
    city = re.sub(r"^.*?(?:省|自治区)", "", city)
    province = _first_match(
        text,
        r"(?:地\s*址|地址)\s*[：:]\s*[^\n]*?([\u4e00-\u9fff]{2,10}(?:省|自治区))",
        r"([\u4e00-\u9fff]{2,10}(?:省|自治区))",
    )
    if not province and city in {"北京市", "上海市", "天津市", "重庆市"}:
        province = city
    budget = budget_value(text)
    contract_amount = _first_match(
        text,
        r"合同金额\s*[：:]\s*([0-9,.]+\s*(?:万元|元)?)",
    ) or _table_value(text, r"合同金额[（(]万元[）)]")
    if contract_amount and not contract_amount.endswith(("元", "万元")):
        contract_amount += "万元"

    purchaser, purchaser_contact, purchaser_phone = _entity_contact(
        text,
        r"采购人(?:（甲方）)?\s*[：:]",
        r"供应商(?:（乙方）)?\s*[：:]",
    )
    winner, winner_contact, winner_phone = _entity_contact(
        text,
        r"供应商(?:（乙方）)?\s*[：:]",
        r"六、|合同主要信息",
    )
    purchaser_section = _section_unit(
        text,
        r"(?:1[.．、]|一、)\s*采购人信息",
        r"(?:2[.．、]|二、)\s*(?:采购代理机构|采购执行机构)信息",
    )
    agency_section = _section_unit(
        text,
        r"(?:2[.．、]|二、)\s*(?:采购代理机构|采购执行机构)信息(?:（如有）)?",
        r"(?:3[.．、]|三、)\s*项目联系方式",
    )
    project_section = _section_unit(
        text,
        r"(?:3[.．、]|三、)\s*项目联系方式",
        r"(?:附件|相关公告|\Z)",
    )
    if agency_section["name"] and project_section["contact"]:
        agency_section["contact"] = (
            agency_section["contact"] or project_section["contact"]
        )
        agency_section["phone"] = agency_section["phone"] or project_section["phone"]
    if (
        not agency_section["contact"]
        and project_section["contact"]
        and agency_section["phone"]
        and agency_section["phone"] == project_section["phone"]
    ):
        agency_section["contact"] = project_section["contact"]
    if (
        not purchaser_section["contact"]
        and project_section["contact"]
        and purchaser_section["phone"]
        and purchaser_section["phone"] == project_section["phone"]
    ):
        purchaser_section["contact"] = project_section["contact"]
    purchaser_labeled = _labeled_unit(text, r"(?:采购人|招标人)")
    agency_labeled = _labeled_unit(text, r"(?:采购代理机构|招标代理机构)")
    purchaser = purchaser or purchaser_section["name"] or _first_match(
        text, r"招标人\s*[：:]\s*([^，,\n]+)"
    )
    purchaser = purchaser or purchaser_labeled["name"]
    purchaser_contact = (
        purchaser_contact or purchaser_section["contact"] or purchaser_labeled["contact"]
    )
    purchaser_phone = (
        purchaser_phone or purchaser_section["phone"] or purchaser_labeled["phone"]
    )

    inline_winner = _first_match(
        text,
        r"单位名称\s*[：:]\s*([^；;\n]+).*?(?:中标|成交)金额\s*[：:]\s*([^；;\n]+)",
    )
    inline_amount = ""
    inline_match = re.search(
        r"单位名称\s*[：:]\s*([^；;\n]+).*?(?:中标|成交)金额\s*[：:]\s*([^；;\n]+)",
        text,
        re.M | re.S,
    )
    if inline_match:
        inline_winner = inline_match.group(1).strip()
        inline_amount = inline_match.group(2).strip()
    table_winner, table_amount = _award_table(text)
    winner = winner or inline_winner or table_winner
    winning_amount = inline_amount or table_amount or contract_amount

    industry = _table_value(text, r"中小企业划分标准所属行业") or _first_match(
        text, r"所属行业\s*[：:]\s*([^\n]+)"
    )
    contract_end = _first_match(
        text,
        r"(?:履约期限|服务期限)[^：:\n]*[：:]\s*[^\n]*?至\s*(\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)",
    )
    dates = notice_dates(text)
    return {
        "project_no": project_no,
        "province": province,
        "city": city,
        "budget": budget,
        "contract_amount": contract_amount,
        "purchaser": purchaser,
        "purchaser_contact": purchaser_contact,
        "purchaser_phone": purchaser_phone,
        "winner": winner,
        "winner_contact": winner_contact,
        "winner_phone": winner_phone,
        "winning_amount": winning_amount,
        "agency": agency_section["name"] or agency_labeled["name"],
        "agency_contact": agency_section["contact"] or agency_labeled["contact"],
        "agency_phone": agency_section["phone"] or agency_labeled["phone"],
        "industry": industry,
        "notice_type": _notice_type(text),
        "contract_end": date_value(contract_end),
        **dates,
    }


def _record_id(record: TenderRecord) -> str:
    """Use an identifier from the source URL, with a stable hash fallback."""
    if record.source_url:
        parsed = urlparse(record.source_url)
        query = parse_qs(parsed.query)
        for key in ("id", "guid", "pkid", "articleId", "noticeId", "encryptId"):
            value = query.get(key, [""])[0].strip()
            if value:
                return unquote(value)
        stem = Path(parsed.path).stem
        if stem and stem.lower() not in {"detail", "index", "view", "notice"}:
            return unquote(stem)
    identity = record.source_url or "\x1f".join(
        (record.title, record.purchaser, record.publish_date)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _bid_progress(notice_type: str) -> list[str]:
    if not notice_type:
        return []
    if "合同" in notice_type:
        return ["中标公告", "合同"]
    if any(value in notice_type for value in ("废标", "流标", "终止")):
        return ["中标公告", "废标流标终止"]
    if any(value in notice_type for value in ("中标", "成交", "结果")):
        return ["中标公告", "中标成交"]
    if any(value in notice_type for value in ("变更", "更正")):
        return ["招标公告", "变更"]
    if any(value in notice_type for value in ("招标", "采购", "谈判", "磋商", "询价")):
        return ["招标公告", "招标"]
    return [notice_type]


def _unit(
    name: str,
    *,
    contact: str = "",
    phone: str = "",
    amount: str | None = None,
) -> dict:
    unit = {}
    if amount is not None:
        unit["WinBidAmt"] = amount
    unit.update(
        {
            "KeyNo": "",
            "Name": name,
            "Contact": contact or None,
            "TelNo": phone or None,
            "CreditCode": "",
        }
    )
    return unit


def _record_payload(record: TenderRecord) -> dict:
    fallback = _content_fallbacks(record)
    purchaser = record.purchaser or fallback["purchaser"]
    winner = record.winner or fallback["winner"]
    winning_amount = record.winning_amount or fallback["winning_amount"]
    agency = record.agency or fallback["agency"]
    notice_type = record.notice_type or fallback["notice_type"]
    purchaser_contact = fallback["purchaser_contact"]
    purchaser_phone = fallback["purchaser_phone"]
    content = record.content or record.summary

    return {
        "Id": _record_id(record),
        "Title": record.title,
        "ProjectNo": fallback["project_no"],
        "ChannelName": notice_type,
        "Province": record.region or fallback["province"],
        "City": fallback["city"],
        "IndustryDesc": record.industry or fallback["industry"],
        "BudgetAmt": fallback["budget"] or amount_value(record.budget),
        "PublishDate": record.publish_date,
        "OpenDate": fallback["open_date"],
        "ObtainEndDate": fallback["obtain_end_date"],
        "BidInviUnitList": (
            [
                _unit(
                    purchaser,
                    contact=purchaser_contact,
                    phone=purchaser_phone,
                )
            ]
            if purchaser
            else []
        ),
        "WinBidUnitList": (
            [
                _unit(
                    winner,
                    contact=fallback["winner_contact"],
                    phone=fallback["winner_phone"],
                    amount=winning_amount,
                )
            ]
            if winner
            else []
        ),
        "AgentUnitList": (
            [
                _unit(
                    agency,
                    contact=fallback["agency_contact"],
                    phone=fallback["agency_phone"],
                )
            ]
            if agency
            else []
        ),
        "BidProgressList": _bid_progress(notice_type),
        "BidEndDate": fallback["bid_end_date"] or date_value(record.deadline),
        "ContentUrl": record.source_url,
        "ContractEndTime": fallback["contract_end"],
        "content_text": content,
        "content_status": "ok" if content else "empty",
    }


def build_output_payload(
    records_by_keyword: Mapping[str, Sequence[TenderRecord]],
    *,
    site_key: str,
    order_number: str | None = None,
) -> dict:
    """Build the same nested response shape as output_structure.json."""
    if order_number is None:
        order_number = "TENDERCHECK" + datetime.now().strftime("%Y%m%d%H%M%S%f")

    samples = []
    for keyword, records in records_by_keyword.items():
        data = [_record_payload(record) for record in records]
        samples.append(
            {
                "keyword": keyword,
                "status": "200",
                "message": "【有效请求】查询成功",
                "order_number": order_number,
                "paging": {
                    "PageSize": len(data),
                    "PageIndex": 1,
                    "TotalRecords": len(data),
                },
                "result": {
                    "VerifyResult": 1 if data else 0,
                    "Data": data,
                },
            }
        )

    return {site_key: {"samples": samples}}


def export_json(
    records_by_keyword: Mapping[str, Sequence[TenderRecord]],
    output_path: str,
    *,
    site_key: str,
) -> str:
    """Export records using the output_structure.json response shape."""
    path = Path(output_path)
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")

    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        build_output_payload(records_by_keyword, site_key=site_key),
        ensure_ascii=False,
        indent=2,
    )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return str(path.resolve())
