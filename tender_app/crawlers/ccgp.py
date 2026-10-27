"""中国政府采购网（ccgp.gov.cn）公开采购公告适配器。"""
import hashlib
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .base import TenderCrawler, reported_total
from ..access import AccessStopped
from ..config import NOTICE_CACHE_TTL, SAVE_TEXT_FILES
from ..extraction import extract_tender_fields
from ..ocr import needs_image_ocr, recognize_image
from ..pagination import PageProgress, open_with_timeout_retries

SEARCH_ENDPOINT = "https://search.ccgp.gov.cn/bxsearch"
# www.ccgp.gov.cn 首页偶尔会对程序浏览器返回403；启动时直接进入公开搜索入口。
SEARCH_URL = SEARCH_ENDPOINT
DETAIL_PATH = re.compile(
    r"^/cggg/(?:zygg|dfgg)/[^/]+/\d{6}/t\d+_\d+\.s?html?$", re.IGNORECASE
)


def normalize_detail_url(value):
    parsed = urlsplit(value or "")
    host = (parsed.hostname or "").lower()
    if host not in ("www.ccgp.gov.cn", "ccgp.gov.cn") or not DETAIL_PATH.fullmatch(parsed.path):
        return ""
    # 搜索结果使用HTTP原文地址。该站HTTPS详情对Playwright Chromium可能
    # 返回403，而相同公开页面的HTTP地址正常返回完整正文，因此不强制升级。
    return urlunsplit(("http", "www.ccgp.gov.cn", parsed.path, parsed.query, ""))


def publication_date(value):
    match = re.search(r"(20\d{2})[年:/.-](\d{1,2})[月:/.-](\d{1,2})", value or "")
    if not match:
        return ""
    year, month, day = map(int, match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def result_publication_date(row_text, title):
    """从列表元数据取发布日期，不把标题中的项目年份当成发布日期。"""
    return publication_date((row_text or "").replace(title or "", "", 1))


def notice_type_from_title(title):
    for value in ("公开招标公告", "竞争性磋商公告", "竞争性谈判公告", "询价公告",
                  "资格预审公告", "单一来源公告", "邀请招标公告", "更正公告",
                  "中标公告", "成交公告", "终止公告", "废标公告"):
        if value in title:
            return value
    return ""


class CcgpCrawler(TenderCrawler):
    """使用官网搜索检索中央和地方采购公告。"""

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

    @staticmethod
    def build_search_url(keyword, page_number, start_date, end_date):
        start = (start_date or "").replace("-", ":")
        end = (end_date or "").replace("-", ":")
        return SEARCH_ENDPOINT + "?" + urlencode({
            "searchtype": 1, "page_index": page_number, "bidSort": 0,
            "buyerName": "", "projectId": "", "pinMu": 0, "bidType": 0,
            "dbselect": "bidx", "kw": keyword, "start_time": start,
            "end_time": end, "timeType": 6, "displayZone": "", "zoneId": "",
            "pppStatus": 0, "agentName": "",
        })

    def open_search(self, keyword, page_number, start_date, end_date):
        self.policy.check()
        url = self.build_search_url(keyword, page_number, start_date, end_date)
        try:                                                        # 搜索列表页导航超时时间
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except PlaywrightTimeoutError:
            self.policy.check()
            # 导航虽超时，但结果容器可能已就绪；避免无谓地再次请求同一页。
            results = self.page.locator(".vT-srch-result-list-bid")
            if self.page.url != url or not results.count() or not results.first.is_visible():
                raise
            response = None
        if response is not None and response.status >= 400:
            raise RuntimeError(f"中国政府采购网搜索页返回HTTP {response.status}")
        body = self.page.locator("body").inner_text(timeout=10000)
        if "访问过于频繁" in body or "频繁访问" in self.page.title():
            self.policy.stop("中国政府采购网提示访问过于频繁；已停止本次采集，请稍后再运行")
        self.page.locator(".vT-srch-result-list-bid").wait_for(timeout=60000)

    def current_candidates(self):
        results = []
        rows = self.page.locator(".vT-srch-result-list-bid li")
        for row in rows.all():
            link = row.locator("a[href*='/cggg/']").first
            if not link.count():
                results.append({"url": "", "title": row.inner_text().strip(),
                                "publish_date": "", "skip_reason": "列表项缺少公告详情链接"})
                continue
            url = normalize_detail_url(link.get_attribute("href"))
            title = (link.get_attribute("title") or link.inner_text()).strip()
            text = row.inner_text()
            region_match = re.search(r"(?:地域|地区)\s*[:：]\s*([^|\n]+)", text)
            purchaser_match = re.search(r"采购人\s*[:：]\s*([^|\n]+)", text)
            results.append({
                "url": url, "title": title, "publish_date": result_publication_date(text, title),
                "region": region_match.group(1).strip() if region_match else "",
                "purchaser": purchaser_match.group(1).strip() if purchaser_match else "",
                "skip_reason": "" if url and title else "标题或原文链接无法解析",
                "原始链接": link.get_attribute("href") or "",
            })
        return results

    def read_detail(self, page, key):
        title_node = page.locator(".vF_detail_header h2").first
        title = title_node.inner_text().strip() if title_node.count() else page.title().strip()
        pub_node = page.locator("#pubTime").first
        published = publication_date(pub_node.inner_text() if pub_node.count() else "")
        overview_node = page.locator(".vF_detail_main .table").first
        content_node = page.locator(".vF_detail_content").first
        overview = overview_node.inner_text().strip() if overview_node.count() else ""
        body = content_node.inner_text().strip() if content_node.count() else ""
        content = "\n\n".join(part for part in (overview, body) if part).strip()
        main_node = page.locator(".vF_detail_main").first
        if not main_node.count() or not main_node.is_visible():
            raise ValueError("未找到公告正文容器 .vF_detail_main，页面结构可能已变化")
        image_count = sum(1 for image in main_node.locator("img").all() if image.is_visible())
        meta = {"正文来源": "网页文字", "OCR图片": "", "OCR平均置信度": None}
        if needs_image_ocr(content, image_count):
            page.wait_for_timeout(1000)
            self.ocr_count += 1
            image_path = self.run_dir / f"中国政府采购网图片正文_{self.ocr_count:02d}_{key[:8]}.png"
            main_node.screenshot(path=str(image_path), timeout=30000)
            ocr_text, confidence = recognize_image(image_path)
            if len(re.sub(r"\s+", "", ocr_text)) < 20:
                raise ValueError(f"图片正文OCR未得到足够文字，原图已保存：{image_path.name}")
            content = "\n".join(part for part in (content, ocr_text) if part).strip()
            meta = {"正文来源": "图片OCR", "OCR图片": image_path.name,
                    "OCR平均置信度": round(confidence, 4)}
        if len(re.sub(r"\s+", "", content)) < 20:
            raise ValueError("公告正文有效文字不足20个字符")
        return title, published, content, meta

    def search(self, keyword, regions=None, start_date=None, end_date=None, limit=50):
        if regions:
            raise ValueError("中国政府采购网适配器当前仅支持地区不限")
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
            print(f"\n{keyword}：读取中国政府采购网搜索结果第{page_number}页。")
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
                title, url = item["title"], item["url"]
                published = item["publish_date"]
                if item.get("skip_reason"):
                    entry["状态"] = item["skip_reason"]
                    entry["原始链接"] = item.get("原始链接", "")
                    continue
                if url in self.seen_urls:
                    entry["状态"] = "本次运行链接重复"
                    continue
                self.seen_urls.add(url)
                if (start_date or end_date) and not published:
                    entry["状态"] = "列表中无发布日期"
                    continue
                if published and ((start_date and published < start_date) or
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
                    detail_title, detail_date, content, content_meta = self.read_detail(detail, key)
                    published = detail_date or published
                    if ((start_date and published < start_date) or
                            (end_date and published > end_date)):
                        raise ValueError("详情页发布日期超出指定范围")
                    fields = extract_tender_fields("", content)
                    record = {**fields, "title": fields.get("title") or detail_title or title,
                              "purchaser": fields.get("purchaser") or item["purchaser"],
                              "region": fields.get("region") or item["region"],
                              "notice_type": fields.get("notice_type") or notice_type_from_title(title),
                              "publish_date": published, "summary": "", "content": content,
                              "source_name": "中国政府采购网", "source_url": url}
                    self.policy.check()
                    if SAVE_TEXT_FILES:
                        (self.run_dir / f"ccgp_{key[:16]}.txt").write_text(
                            record["title"] + "\n" + url + "\n\n【公告正文】\n" + content,
                            encoding="utf-8")
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
