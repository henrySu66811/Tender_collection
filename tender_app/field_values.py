"""Conservative amount/date extraction shared by collection and JSON export."""
import re
from datetime import datetime


def amount_value(value: str, label: str = "") -> str:
    match = re.match(
        r"\s*(?:人民币\s*)?[￥¥]?\s*(\d[\d,]*(?:\.\d+)?)"
        r"\s*(?:[（(]\s*)?(亿元|万元|元)?", value
    )
    if not match:
        return ""
    unit = match[2] or next((u for u in ("亿元", "万元", "元") if u in label), "")
    return match[1] + unit


def date_value(value: str) -> str:
    match = re.search(
        r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})日?"
        r"(?:[ \t]*(\d{1,2})\s*[:：时点]\s*(\d{1,2})(?:分|(?:[:：]\d{1,2})秒?)?)?",
        value,
    )
    if not match:
        return ""
    year, month, day, hour, minute = match.groups()
    try:
        date = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
    except ValueError:
        return ""
    return date.strftime("%Y-%m-%d %H:%M" if hour is not None else "%Y-%m-%d")


def budget_value(text: str) -> str:
    for match in re.finditer(
        r"(?:预算金额|采购预算|项目预算)(?:\s*[（(](亿元|万元|元)[）)])?"
        r"\s*[：:]?\s*((?:人民币\s*)?[￥¥]?\s*\d[\d,]*(?:\.\d+)?"
        r"\s*(?:[（(]\s*)?(?:亿元|万元|元)?)", text
    ):
        result = amount_value(match[2], match[1] or "")
        if result.endswith("元"):
            return result
    return ""


def notice_dates(text: str) -> dict[str, str]:
    def labeled(block, label):
        for match in re.finditer(rf"(?:{label})\s*[：:]\s*([^\n]+)", block):
            value = date_value(match[1])
            if value:
                return value
        return ""

    # A combined heading can contain a submission start AND end. Never use
    # its first dated line indiscriminately, and never search beyond the section.
    combined = re.search(
        r"提交投标文件截止时间[、，,及\s]+开标时间和地点[^\n]*\n"
        r"(.*?)(?=\n\s*[五六七八九十][、．.]|\Z)", text, re.S
    )
    block = combined[1] if combined else ""
    end = labeled(block, r"截止时间|投标截止时间")
    if not end and block:
        first = block.strip().splitlines()[0]
        if not re.search(r"开始|起始|获取|报名", first):
            end = date_value(first)
    bid = end or labeled(text, r"投标截止时间|投标截止日期|提交(?:（上传）)?投标文件截止时间|响应文件提交截止时间")
    if not bid:
        match = re.search(r"并于\s*([^\n]+?)前(?:递交|提交)投标文件", text)
        bid = date_value(match[1]) if match else ""
    opening = labeled(block, r"开标时间|开启时间") or end
    if not opening:
        section = re.search(r"(?:五、|5[.．、])\s*开标\s*\n([^\n]*\n?[^\n]*)", text)
        opening = labeled(section[1], r"时间") if section else ""
    opening = opening or labeled(text, r"开标时间|开启时间")
    obtain = labeled(text, r"获取(?:招标|采购)文件(?:截止时间|结束时间)|招标文件获取截止时间")
    if not obtain:
        match = re.search(
            r"获取(?:招标|采购)文件[^\n]*\n?\s*(?:\d+[.．、]\s*)?时间\s*[：:]"
            r"[^\n]*?(?:至|到)\s*([^，,\n]+)", text
        )
        obtain = date_value(match[1]) if match else ""
    return {"open_date": opening, "bid_end_date": bid, "obtain_end_date": obtain}
