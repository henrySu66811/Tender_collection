"""山东省公共资源交易中心站内搜索适配器。"""
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .base import TenderCrawler, reported_total
from ..access import AccessStopped
from ..config import NOTICE_CACHE_TTL, SAVE_TEXT_FILES
from ..extraction import extract_tender_fields
from ..ocr import needs_image_ocr, recognize_image
from ..pagination import PageProgress, open_with_timeout_retries

SEARCH_URL = "https://ggzyjyzx.shandong.gov.cn/"
SEARCH_ENDPOINT = "https://ggzyjyzx.shandong.gov.cn/jsearchfront/search.do"
DETAIL_PATH = re.compile(
    r"^/art/\d{4}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html$", re.IGNORECASE
)
DETAIL_BODY_SELECTORS = (".zhengwen", "#zoom", "#noticeArea")


def normalize_detail_url(value):
    """接受搜索结果显示的原文地址或 visit/link.do 跳转地址。"""
    raw = (value or "").strip()
    parsed = urlsplit(raw)
    if parsed.path.endswith("/jsearchfront/visit/link.do"):
        raw = parse_qs(parsed.query).get("url", [""])[0]
        parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if host != "ggzyjyzx.shandong.gov.cn" or not DETAIL_PATH.fullmatch(parsed.path):
        return ""
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def publication_date(value):
    match = re.search(r"(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})", value or "")
    if not match:
        return ""
    year, month, day = map(int, match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def search_date(value):
    """站内搜索要求 yyyyMMdd；其他格式会显示在表单中但不会真正筛选。"""
    parsed = publication_date(value or "")
    return parsed.replace("-", "") if parsed else ""


def build_search_url(keyword, page_number=1, start_date=None, end_date=None):
    return SEARCH_ENDPOINT + "?" + urlencode({
        "pagemode": "result", "appid": "all", "style": 1, "ck": 0,
        "pos": "title,content,_default_search", "od": 0, "webid": 428,
        "tpl": 1164, "websiteid": "370000000000110", "q": keyword,
        "begin": search_date(start_date), "end": search_date(end_date),
        "p": page_number,
    })


def notice_type_from_title(title):
    for value in ("公开招标公告", "招标公告", "采购公告", "竞争性磋商公告",
                  "竞争性谈判公告", "询价公告", "更正公告", "变更公告",
                  "评标结果公示", "中标候选人公示", "结果公告", "成交公告",
                  "中标公告", "合同公示", "终止公告", "废标公告"):
        if value in title:
            return value
    return ""


class ShandongGgzyCrawler(TenderCrawler):
    """使用官网搜索框对应的搜索页面检索关键词，并复核发布日期。"""

    def __init__(self, context, page, run_dir, *, policy, cache, site_key):
        self.context, self.page, self.run_dir = context, page, Path(run_dir)
        self.policy, self.cache, self.site_key = policy, cache, site_key
        self.audit, self.completed, self.seen_urls = [], [], set()
        self.ocr_count = 0
        self.detail_page = None
        self.found_urls = set()
        self.reported_total = None

    def close(self):
        if self.detail_page is not None and not self.detail_page.is_closed():
            self.detail_page.close()

    def open_search(self, keyword, page_number, start_date, end_date):
        self.policy.check()
        url = build_search_url(keyword, page_number, start_date, end_date)
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except PlaywrightTimeoutError:
            self.policy.check()
            results = self.page.locator(".jcse-result-box")
            if self.page.url != url or not results.count() or not results.first.is_visible():
                raise
            response = None
        status = response.status if response is not None else 0
        if status >= 400:
            raise RuntimeError(f"山东站内搜索页返回HTTP {status}")
        self.page.locator(".jcse-result-box").first.wait_for(timeout=60000)

    def current_candidates(self):
        results = []
        for row in self.page.locator(".jcse-result-box").all():
            title_link = row.locator(".jcse-news-title a").first
            if not title_link.count():
                results.append({"title": row.inner_text().strip(), "url": "",
                                "publish_date": "", "skip_reason": "列表项缺少标题链接"})
                continue
            title = title_link.inner_text().strip()
            date_node = row.locator(".jcse-news-date").first
            published = publication_date(date_node.inner_text() if date_node.count() else "")
            shown_url = row.locator(".jcse-news-url a").first
            raw_url = shown_url.inner_text().strip() if shown_url.count() else ""
            url = normalize_detail_url(raw_url) or normalize_detail_url(
                title_link.get_attribute("href")
            )
            results.append({"title": title, "url": url, "publish_date": published,
                            "skip_reason": "" if title and url else "标题或原文链接无法解析",
                            "原始链接": raw_url or title_link.get_attribute("href") or ""})
        return results

    def read_detail(self, page, key):
        title_node = page.locator(".article-title").first
        title_meta = page.locator('meta[name="ArticleTitle"]').first
        title = title_node.inner_text().strip() if title_node.count() else ""
        if not title and title_meta.count():
            title = (title_meta.get_attribute("content") or "").strip()
        if not title:
            title = page.title().strip()
        metadata_node = page.locator(".article-xinxi").first
        metadata = metadata_node.inner_text().strip() if metadata_node.count() else ""
        pubdate_meta = page.locator('meta[name="pubdate"]').first
        if not publication_date(metadata) and pubdate_meta.count():
            metadata = (pubdate_meta.get_attribute("content") or "").strip()

        body_node = None
        body_selector = ""
        for selector in DETAIL_BODY_SELECTORS:
            candidate = page.locator(selector).first
            if candidate.count() and candidate.is_visible():
                body_node, body_selector = candidate, selector
                break
        if body_node is None:
            raise ValueError(
                "未找到公告正文 .zhengwen、#zoom 或 #noticeArea，页面结构可能已变化"
            )
        content = body_node.inner_text().strip()
        image_count = sum(1 for image in body_node.locator("img").all() if image.is_visible())
        source_meta = {"正文来源": "网页文字", "正文容器": body_selector,
                       "OCR图片": "", "OCR平均置信度": None}
        if needs_image_ocr(content, image_count):
            page.wait_for_timeout(1000)
            self.ocr_count += 1
            image_path = self.run_dir / f"山东公共资源图片正文_{self.ocr_count:02d}_{key[:8]}.png"
            body_node.screenshot(path=str(image_path), timeout=30000)
            ocr_text, confidence = recognize_image(image_path)
            if len(re.sub(r"\s+", "", ocr_text)) < 20:
                raise ValueError(f"图片正文OCR未得到足够文字，原图已保存：{image_path.name}")
            content = "\n".join(part for part in (content, ocr_text) if part).strip()
            source_meta = {"正文来源": "图片OCR", "正文容器": body_selector,
                           "OCR图片": image_path.name,
                           "OCR平均置信度": round(confidence, 4)}
        if len(re.sub(r"\s+", "", content)) < 20:
            raise ValueError("公告正文有效文字不足20个字符")
        return title, metadata, content, source_meta

    def search(self, keyword, regions=None, start_date=None, end_date=None, limit=50):
        if regions:
            raise ValueError("山东省公共资源交易中心适配器当前仅支持地区不限")
        result = []
        progress = PageProgress(self.cache, self.site_key, keyword, start_date, end_date)
        seen_pages = {}
        page_visits = {}
        while len(result) < limit:
            self.policy.check()
            page_number, repairing = progress.target()
            if page_number is None:
                break
            if not open_with_timeout_retries(
                lambda: self.open_search(keyword, page_number, start_date, end_date),
                self.policy, page_number, self.audit, keyword,
            ):
                progress.failed(page_number, repairing)
                self.audit.append({"关键词": keyword, "页码": page_number,
                                   "状态": "列表页待补采"})
                if progress.consecutive_timeouts >= 3:
                    self.audit.append({"关键词": keyword, "状态": "连续三页超时，保留断点待下次续采"})
                    break
                continue
            print(f"\n{keyword}：读取山东站内搜索第{page_number}页，时间范围 "
                  f"{start_date or '不限'} 至 {end_date or '不限'}。")
            page_visits[page_number] = page_visits.get(page_number, 0) + 1
            visit = page_visits[page_number]
            candidates = self.current_candidates()
            self.found_urls.update(item["url"] for item in candidates if item.get("url"))
            if page_number == 1:
                self.reported_total = reported_total(self.page)
                progress.set_total(self.reported_total)
            self.audit.append({"状态": "列表页", "关键词": keyword, "访问轮次": visit,
                               "页码": page_number, "候选数": len(candidates)})
            if not candidates:
                if repairing or (progress.data['last_page'] is not None
                                 and page_number <= progress.data['last_page']):
                    progress.failed(page_number, repairing=True)
                    self.audit.append({"关键词": keyword, "页码": page_number,
                                       "状态": "补采页为空，仍待补采"})
                else:
                    progress.end_at(page_number)
                continue
            page_audit = [{"关键词": keyword, "页码": page_number, "访问轮次": visit,
                           "页内序号": index,
                           "标题": item.get("title", ""), "链接": item.get("url", ""),
                           "列表发布日期": item.get("publish_date", ""),
                           "状态": "未处理：采集提前停止"}
                          for index, item in enumerate(candidates, 1)]
            self.audit.extend(page_audit)
            page_signature = tuple((item.get("url"), item.get("title")) for item in candidates)
            if page_signature in seen_pages and seen_pages[page_signature] != page_number:
                self.audit.append({"状态": "列表页重复，结束翻页", "页码": page_number})
                for entry in page_audit:
                    entry["状态"] = "列表内容重复，待补采"
                progress.failed(page_number, repairing)
                if progress.consecutive_timeouts >= 3:
                    break
                continue
            seen_pages[page_signature] = page_number
            detail_failures = False
            for item, entry in zip(candidates, page_audit):
                if len(result) >= limit:
                    for pending in page_audit:
                        if pending["状态"] == "未处理：采集提前停止":
                            pending["状态"] = "达到PER_KEYWORD上限，未处理"
                    return result
                title, url, published = item["title"], item["url"], item["publish_date"]
                if item.get("skip_reason"):
                    entry["状态"] = item["skip_reason"]
                    entry["原始链接"] = item.get("原始链接", "")
                    continue
                if url in self.seen_urls:
                    entry["状态"] = "本次运行链接重复"
                    continue
                self.seen_urls.add(url)
                if not published:
                    entry["状态"] = "搜索结果无发布日期"
                    continue
                if ((start_date and published < start_date) or
                        (end_date and published > end_date)):
                    entry["状态"] = "发布日期超出范围"
                    continue
                key = hashlib.sha256(url.encode()).hexdigest()
                if self.cache.cached(self.site_key, key, NOTICE_CACHE_TTL):
                    entry["状态"] = "跨次缓存命中，跳过详情"
                    progress.resolved_detail(url)
                    continue
                try:
                    self.policy.before_detail()
                    print("读取详情：" + title)
                    if self.detail_page is None or self.detail_page.is_closed():
                        self.detail_page = self.context.new_page()
                    detail = self.detail_page
                    response = detail.goto(url, wait_until="domcontentloaded", timeout=90000)
                    status = response.status if response is not None else 0
                    if status >= 400:
                        raise ValueError(f"详情页返回HTTP {status}")
                    detail_title, metadata, content, content_meta = self.read_detail(detail, key)
                    detail_date = publication_date(metadata)
                    if detail_date and detail_date != published:
                        published = detail_date
                    if ((start_date and published < start_date) or
                            (end_date and published > end_date)):
                        raise ValueError("详情页发布日期超出指定范围")
                    fields = extract_tender_fields("", content)
                    record = {**fields, "title": fields.get("title") or detail_title or title,
                              "region": fields.get("region") or "山东省",
                              "notice_type": fields.get("notice_type") or notice_type_from_title(title),
                              "publish_date": published, "summary": "", "content": content,
                              "source_name": "山东省公共资源交易中心", "source_url": url}
                    self.policy.check()
                    if SAVE_TEXT_FILES:
                        (self.run_dir / f"shandong_ggzy_{key[:16]}.txt").write_text(
                            record["title"] + "\n" + url + "\n" + metadata
                            + "\n\n【公告正文】\n" + content, encoding="utf-8")
                    self.cache.save_completed(self.site_key, key, record, self.run_dir, self.active_keyword)
                    progress.resolved_detail(url)
                    self.completed.append(record)
                    result.append(record)
                    entry.update({"状态": "成功", **content_meta})
                except AccessStopped as exc:
                    entry["状态"] = f"访问控制停止：{exc}"
                    for pending in page_audit:
                        if pending["状态"] == "未处理：采集提前停止":
                            pending["状态"] = "访问控制停止，未处理"
                    raise
                except Exception as exc:
                    self.policy.check()
                    print("跳过：" + str(exc))
                    entry["状态"] = str(exc)
                    if str(exc) != "详情页发布日期超出指定范围":
                        self.seen_urls.discard(url)
                        detail_failures = True
                        progress.failed_detail(page_number, item)
                    else:
                        progress.resolved_detail(url)
            progress.completed(page_number, repairing, retry_needed=detail_failures)
        return result
