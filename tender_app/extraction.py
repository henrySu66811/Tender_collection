import re
from .field_values import amount_value, budget_value, notice_dates

FIELD_LABELS = {
    "title": ("项目名称", "采购项目名称", "招标项目名称"),
    "purchaser": (
        "招标单位",
        "采购单位",
        "采购人",
        "招标人",
        "采购人名称",
        "招标人名称",
    ),
    "agency": ("代理机构", "采购代理机构", "招标代理机构", "代理机构名称"),
    "region": ("项目地区", "所属地区", "所在地区", "地区", "行政区域"),
    "industry": ("所属行业", "行业", "项目类型", "采购类别", "采购品目"),
    "notice_type": ("公告类型", "信息类型"),
    "deadline": (
        "投标截止时间",
        "投标截止日期",
        "提交投标文件截止时间",
        "响应文件提交截止时间",
    ),
    "budget": (
        "预算金额",
        "采购预算",
        "项目预算",
        "预算",
        "预算金额（元）",
        "预算金额(元)",
        "预算金额（万元）",
        "预算金额(万元)",
    ),
    "winner": (
        "中标单位",
        "中标人",
        "成交供应商",
        "成交单位",
        "中标供应商",
        "中标（成交）供应商名称",
    ),
    "winning_amount": (
        "中标金额",
        "成交金额",
        "成交价",
        "中标价",
        "中标（成交）金额",
        "中标金额（元）",
        "成交金额（元）",
        "中标金额（万元）",
        "成交金额（万元）",
    ),
    "contact": ("项目联系人", "采购联系人", "招标联系人", "联系人"),
    "phone": ("项目联系电话", "采购联系电话", "联系电话", "电话"),
}


def extract_tender_fields(summary, body):
    """摘要优先，正文补缺；仅按明确标签提取，金额保留币种/单位。"""

    labels = sorted(
        {x for values in FIELD_LABELS.values() for x in values}
        | {
            "地址",
            "联系地址",
            "项目编号",
            "发布时间",
            "发布日期",
            "采购方式",
            "最高限价",
            "服务期限",
            "合同履行期限",
        },
        key=len,
        reverse=True,
    )

    pattern = re.compile(
        r"(?:^|[\n\t；;]|(?<=\s))(?P<label>"
        + "|".join(map(re.escape, labels))
        + r")\s*(?:[:：][ \t]*|[ \t]*\n[ \t]*|\t+)",
        re.M,
    )

    result = {}

    for text in (summary, body):

        matches = list(pattern.finditer(text))

        values = {}

        for i, match in enumerate(matches):

            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

            # 不跨越后续正文段落，以免把条款整段装入字段。

            value = text[match.end() : end].strip().split("\n")[0].strip(" \t；;")

            if not value or value in ("-", "--", "暂无", "无", "未披露"):

                continue

            if any(x in value for x in ("***", "登录", "会员", "点击查看", "NaN")):

                continue

            values.setdefault(match.group("label"), value)

        for field, aliases in FIELD_LABELS.items():

            if field in result:

                continue

            for label in aliases:

                if label not in values:

                    continue

                value = values[label]

                if field in ("budget", "winning_amount"):

                    if not re.search(r"\d", value):

                        continue

                    if "元" not in value:

                        if "万元" in label:

                            value += "万元"

                        elif "元" in label:

                            value += "元"

                if field == "phone" and not re.search(r"\d{3}", value):

                    continue

                result[field] = value

                break

    # Only an explicitly scoped submission deadline may override the summary.
    # A generic 截止时间 may instead refer to registration or document purchase.
    budget = budget_value(body) or budget_value(summary)
    deadline = notice_dates(body)["bid_end_date"] or notice_dates(summary)["bid_end_date"]
    if budget:
        result["budget"] = budget
    if deadline:
        result["deadline"] = deadline
    for field in ("budget", "winning_amount"):
        if field in result:
            result[field] = amount_value(result[field])
    return result
