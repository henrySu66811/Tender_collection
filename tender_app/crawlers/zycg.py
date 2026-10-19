"""中央政府采购网（zycg.gov.cn）公开公告适配器。"""
import hashlib
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .base import TenderCrawler, reported_total
from ..access import AccessStopped
from ..config import NOTICE_CACHE_TTL, SAVE_TEXT_FILES
from ..extraction import extract_tender_fields
from ..ocr import needs_image_ocr, recognize_image

SEARCH_URL = "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/searchlist/index.html"


def normalize_notice_url(value):
    """只保留中央政府采购网自身及其子域公告地址，并统一使用HTTPS。"""
    url = urljoin(SEARCH_URL, value or "")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host != "zycg.gov.cn" and not host.endswith(".zycg.gov.cn"):
        return ""
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def publication_date(value):
    match = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", value or "")
    if not match:
        return ""
    year, month, day = map(int, match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


class ZycgCrawler(TenderCrawler):
    """搜索公开公告，最多检查三页，并读取详情正文。"""

    BODY_SELECTORS = (
        ".infoContent", ".info-content", ".article-content", ".articleContent",
        ".detail-content", "#content", ".cont",
    )

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

    def open_search(self, keyword):
        self.policy.check()
        url = SEARCH_URL + "?searchContent=" + quote(keyword)
        with self.page.expect_response(
            lambda item: "/freecms/rest/v1/notice/searchAll.do" in item.url,
            timeout=90000,
        ) as search_response:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        if response is not None and response.status >= 400:
            raise RuntimeError(f"中央政府采购网搜索页返回HTTP {response.status}")
        api_response = search_response.value
        if api_response.status >= 400:
            raise RuntimeError(f"中央政府采购网搜索接口返回HTTP {api_response.status}")
        self.page.wait_for_timeout(300)

    @staticmethod
    def _longest_visible(page, selectors):
        best_node, best_text = None, ""
        for selector in selectors:
            for node in page.locator(selector).all():
                try:
                    if not node.is_visible():
                        continue
                    text = node.inner_text().strip()
                    if len(re.sub(r"\s+", "", text)) > len(re.sub(r"\s+", "", best_text)):
                        best_node, best_text = node, text
                except Exception:
                    continue
        return best_node, best_text

    def read_detail(self, page, key):
        node, content = self._longest_visible(page, self.BODY_SELECTORS)
        if node is None:
            raise ValueError("未找到公告正文容器，页面结构可能已变化")
        image_count = sum(1 for image in node.locator("img").all() if image.is_visible())
        meta = {"正文来源": "网页文字", "OCR图片": "", "OCR平均置信度": None}
        if needs_image_ocr(content, image_count):
            page.wait_for_timeout(1000)
            self.ocr_count += 1
            image_path = self.run_dir / f"中央政府采购网图片正文_{self.ocr_count:02d}_{key[:8]}.png"
            node.screenshot(path=str(image_path), timeout=30000)
            ocr_text, confidence = recognize_image(image_path)
            if len(re.sub(r"\s+", "", ocr_text)) < 20:
                raise ValueError(f"图片正文OCR未得到足够文字，原图已保存：{image_path.name}")
            content = "\n".join(part for part in (content, ocr_text) if part).strip()
            meta = {"正文来源": "图片OCR", "OCR图片": image_path.name,
                    "OCR平均置信度": round(confidence, 4)}
        if len(re.sub(r"\s+", "", content)) < 20:
            raise ValueError("公告正文有效文字不足20个字符")
        return content, meta

    def _current_candidates(self):
        result = []
        for item in self.page.locator("#box li").all():
            link = item.locator("a").first
            if not link.count():
                continue
            url = normalize_notice_url(link.get_attribute("href"))
            title = (link.get_attribute("title") or link.inner_text()).strip()
            text = item.inner_text()
            if url and title:
                result.append({"url": url, "title": title,
                               "publish_date": publication_date(text)})
        return result

    def search(self, keyword, regions=None, start_date=None, end_date=None, limit=50):
        if regions:
            raise ValueError("中央政府采购网适配器当前仅支持地区不限")
        self.open_search(keyword)
        print(f"\n{keyword}：中央政府采购网站内公告搜索已加载。")
        result = []
        page_number = 1
        seen_pages = set()
        while len(result) < limit:
            self.policy.check()
            candidates = self._current_candidates()
            self.found_urls.update(item["url"] for item in candidates)
            if page_number == 1:
                self.reported_total = reported_total(self.page)
            self.audit.append({"状态": "列表页", "页码": page_number, "候选数": len(candidates)})
            if not candidates:
                break
            page_signature = tuple(item["url"] for item in candidates)
            if page_signature in seen_pages:
                self.audit.append({"状态": "列表页重复，结束翻页", "页码": page_number})
                break
            seen_pages.add(page_signature)
            for item in candidates:
                if len(result) >= limit:
                    return result
                url, title, published = item["url"], item["title"], item["publish_date"]
                if url in self.seen_urls:
                    continue
                self.seen_urls.add(url)
                if (start_date or end_date) and not published:
                    self.audit.append({"标题": title, "状态": "列表中无发布日期", "链接": url})
                    continue
                if published and ((start_date and published < start_date) or
                                  (end_date and published > end_date)):
                    continue
                key = hashlib.sha256(url.encode()).hexdigest()
                if self.cache.cached(self.site_key, key, NOTICE_CACHE_TTL):
                    self.audit.append({"标题": title, "状态": "跨次缓存命中，跳过详情", "链接": url})
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
                    content, meta = self.read_detail(detail, key)
                    fields = extract_tender_fields("", content)
                    record = {**fields, "title": fields.get("title") or title,
                              "publish_date": published, "summary": content[:500],
                              "content": content, "source_name": "中央政府采购网",
                              "source_url": detail.url}
                    self.policy.check()
                    if SAVE_TEXT_FILES:
                        (self.run_dir / f"zycg_{key[:16]}.txt").write_text(
                            title + "\n" + detail.url + "\n\n【公告正文】\n" + content,
                            encoding="utf-8")
                    self.cache.save_completed(self.site_key, key, record, self.run_dir, self.active_keyword)
                    self.completed.append(record)
                    result.append(record)
                    self.audit.append({"标题": title, "状态": "成功", "链接": detail.url, **meta})
                except AccessStopped:
                    raise
                except Exception as exc:
                    self.policy.check()
                    print("跳过：" + str(exc))
                    self.audit.append({"标题": title, "状态": str(exc), "链接": url})
            if len(result) >= limit:
                break
            next_button = self.page.locator("#page button").filter(has_text="下一页").first
            if not next_button.count() or not next_button.is_visible() or next_button.is_disabled():
                break
            old_urls = [item["url"] for item in candidates]
            next_button.click()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                self.policy.check()
                self.page.wait_for_timeout(300)
                if [item["url"] for item in self._current_candidates()] != old_urls:
                    break
            else:
                break
            page_number += 1
        return result
