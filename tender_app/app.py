import re
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright
from .models import TenderRecord
from .collection import TenderCollectionState
from .config import (
    SITES as CONFIGURED_SITES,
    MAX_SITE_PROCESSES,
    KEYWORDS,
    PER_KEYWORD,
    START_DATE,
    END_DATE,
    OUTPUT_DIR,
)
from .crawlers.registry import get_site
from .access import AccessStore, AccessPolicy, AccessStopped
from .config import (
    MAX_HTTP_REQUESTS,
    MAX_DETAIL_ATTEMPTS,
    COOLDOWN_SECONDS,
    CACHE_DB,
    CRAWLER_USER_AGENT,
    ZYCG_REQUEST_INTERVAL,
    ZYCG_REQUEST_JITTER,
    SHANDONG_GGZY_REQUEST_INTERVAL,
    SHANDONG_GGZY_REQUEST_JITTER,
    CCGP_REQUEST_INTERVAL,
    CCGP_REQUEST_JITTER,
)


def open_login_entry(page, primary_url, run_dir, *, fallback_url=None):
    """打开可用的登录入口；HTTP错误时尝试备用入口并保存诊断。"""
    attempts = []
    urls = [primary_url]
    if fallback_url and fallback_url != primary_url:
        urls.append(fallback_url)

    for url in urls:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=180000)
            status = response.status if response is not None else 0
            attempts.append(
                {"requested_url": url, "final_url": page.url, "status": status}
            )
            if 200 <= status < 400:
                if url != primary_url:
                    print(f"首页不可用，已改用备用页面作为登录入口：{page.url}")
                return
        except Exception as exc:
            attempts.append(
                {
                    "requested_url": url,
                    "final_url": getattr(page, "url", ""),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    try:
        body = page.locator("body").inner_text(timeout=5000)[:2000]
    except Exception:
        body = ""
    diagnostic = run_dir / "启动页面诊断.json"
    diagnostic.write_text(
        json.dumps(
            {
                "attempts": attempts,
                "title": page.title(),
                "body_excerpt": body,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    summary = "；".join(
        f"{item['requested_url']} -> HTTP {item.get('status', item.get('error', '未知错误'))}"
        for item in attempts
    )
    raise RuntimeError(f"网站页面不可用：{summary}；诊断已保存到 {diagnostic}")


# 站点配置校验
def _validated_sites(site_keys):
    sites = [str(site).strip() for site in site_keys if str(site).strip()]
    if not sites:
        raise ValueError("SITES 至少需要配置一个站点")
    duplicates = sorted({site for site in sites if sites.count(site) > 1})
    if duplicates:
        raise ValueError(
            "同一站点不能启动多个进程，重复配置：" + ", ".join(duplicates)
        )
    for site_key in sites:
        get_site(site_key)
    return sites

# 进程工作函数只运行无需终端输入的公开站点。
def _run_site_worker(site_key):
    run_site(site_key)
    return site_key


def _configured_date(value, today):
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("日期必须为 YYYY-MM-DD、today 或 yesterday") from exc


def _export_run(store, site_key, run_dir, keywords):
    state = TenderCollectionState()
    grouped = {keyword: [] for keyword in keywords}
    for keyword, raw_record in store.run_records(site_key, run_dir):
        grouped.setdefault(keyword, []).append(state._normalize_record(raw_record))
    grouped = {key: state._deduplicate_records(value) for key, value in grouped.items()}
    for records in grouped.values():
        for record in records:
            for name in TenderRecord.model_fields:
                value = getattr(record, name)
                setattr(record, name, re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value))
    path = state.export_json(
        str(Path(run_dir) / "招投标信息.json"), grouped, site_key=site_key,
    )
    store.mark_exported(site_key, run_dir)
    return path, sum(len(records) for records in grouped.values())


def _recover_pending_runs(store, site_key):
    for run_dir in store.pending_runs(site_key):
        keywords = list(dict.fromkeys(
            keyword for keyword, _ in store.run_records(site_key, run_dir)
        ))
        path, count = _export_run(store, site_key, run_dir, keywords)
        print(f"[{site_key}] 已恢复上次未导出的 {count} 条：{path}")

# 多站点多进程调度函数
def run_configured_sites():
    sites = _validated_sites(CONFIGURED_SITES)
    # 在派生多个站点进程前完成共享SQLite的WAL设置和建表，避免子进程
    # 同时初始化数据库时争用独占锁。
    cache_store = AccessStore(CACHE_DB)
    cache_store.close()
    if len(sites) == 1:
        run_site(sites[0])
        return

    worker_count = max(1, min(int(MAX_SITE_PROCESSES), len(sites)))
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=context,
    ) as executor:
        futures = {
            executor.submit(_run_site_worker, site_key): site_key
            for site_key in sites
        }

        for future in as_completed(futures):
            site_key = futures[future]
            try:
                future.result()
                print(f"\n[{site_key}] 采集进程已结束")
            except Exception as exc:
                print(f"\n[{site_key}] 采集进程异常：{exc}")


def run_site(site_key):

    site = get_site(site_key)
    RUN_DIR = OUTPUT_DIR / (
        datetime.now().strftime("%Y%m%d_%H%M%S_%f") + f"_{site_key}"
    )
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone(timedelta(hours=8))).date()

    try:

        since = today.replace(year=today.year - 1)

    except ValueError:

        since = today.replace(year=today.year - 1, day=28)

    if site_key in ("shandong_ggzy", "ccgp"):
        configured_start = _configured_date(START_DATE, today)
        configured_end = _configured_date(END_DATE, today)
        if configured_start > configured_end:
            raise ValueError("START_DATE 不能晚于 END_DATE")
        start_date = configured_start.isoformat()
        end_date = configured_end.isoformat()
    else:
        start_date = since.isoformat()
        end_date = today.isoformat()

    crawler = None
    store = AccessStore(CACHE_DB)
    _recover_pending_runs(store, site_key)
    settings = {
        "zycg": (ZYCG_REQUEST_INTERVAL, ZYCG_REQUEST_JITTER, "zycg.gov.cn"),
        "shandong_ggzy": (SHANDONG_GGZY_REQUEST_INTERVAL,
                           SHANDONG_GGZY_REQUEST_JITTER,
                           "ggzyjyzx.shandong.gov.cn"),
        "ccgp": (CCGP_REQUEST_INTERVAL, CCGP_REQUEST_JITTER, "ccgp.gov.cn"),
    }
    interval, jitter, allowed_domain = settings[site_key]
    policy = AccessPolicy(
        store,
        site_key,
        interval=interval,
        jitter=jitter,
        max_requests=MAX_HTTP_REQUESTS,
        max_details=MAX_DETAIL_ATTEMPTS,
        cooldown=COOLDOWN_SECONDS,
        native_requests=True,
        allowed_domain=allowed_domain,
        passthrough_subresources=True,
    )

    print(
        f"[{site_key}] 公告正文采集：每个关键词最多保存 {PER_KEYWORD} 条，完整正文写入JSON。"
    )
    print(f"[{site_key}] 本次日期范围：{start_date} 至 {end_date}")
    print(
        "本次会打开Playwright Chromium独立浏览器，请勿关闭浏览器。"
    )
    print("遇到验证码由你在浏览器中完成；不采集被隐藏的正文。")

    try:

        policy.check()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context_options = {
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            context_options["user_agent"] = CRAWLER_USER_AGENT
            context = browser.new_context(**context_options)
            context.set_default_timeout(60000)
            page = context.new_page()

            open_login_entry(page, site.entry_url, RUN_DIR)
            policy.check()

            print(f"\n[{site_key}] 公开站点已打开，开始自动采集。")

            # 页面正常加载后进入自动采集阶段，并从此刻开始执行访问策略。
            policy.check_url(context, site.entry_url)
            policy.attach(context)

            crawler = site.crawler(
                context, page, RUN_DIR, policy=policy, cache=store, site_key=site_key
            )

            try:

                for keyword in KEYWORDS:

                    crawler.active_keyword = keyword

                    try:

                        crawler.search(
                            keyword,
                            start_date=start_date,
                            end_date=end_date,
                            limit=PER_KEYWORD,
                        )

                    except AccessStopped:
                        raise
                    except Exception as exc:
                        policy.check()

                        print(f"关键词 {keyword} 失败：{exc}")

                        crawler.audit.append({"关键词": keyword, "状态": str(exc)})

            finally:
                try:
                    crawler.close()
                finally:
                    context.close()
                    browser.close()

    except AccessStopped as exc:
        pass  # AccessPolicy.stop 已输出原因，finally 保存访问日志。
    except KeyboardInterrupt:

        print("已停止，保存此前完成的结果。")

    except Exception as exc:

        if not policy.reason:
            print(f"运行错误：{exc}")

    finally:
        (RUN_DIR / "访问控制日志.json").write_text(
            json.dumps(
                {
                    "http_requests": policy.count,
                    "detail_attempts": policy.details,
                    "stopped_reason": policy.reason,
                    "events": policy.events,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (RUN_DIR / "采集日志.json").write_text(
            json.dumps(crawler.audit if crawler else [], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        audit = crawler.audit if crawler else []
        statuses = [item.get("状态", "") for item in audit]
        page_stats = {}
        for item in audit:
            if site_key in ("shandong_ggzy", "ccgp") and item.get("状态") == "列表页":
                key = (item.get("关键词", ""), item["页码"], item.get("访问轮次", 1))
                page_stats[key] = {"关键词": key[0], "页码": key[1], "访问轮次": key[2],
                                   "候选数": item["候选数"],
                                   "已记录去向数": 0, "去向统计": {}}
            elif site_key in ("shandong_ggzy", "ccgp") and "页内序号" in item:
                page = page_stats[(item.get("关键词", ""), item["页码"],
                                   item.get("访问轮次", 1))]
                page["已记录去向数"] += 1
                status = item["状态"]
                page["去向统计"][status] = page["去向统计"].get(status, 0) + 1
        for page in page_stats.values():
            page["数量核对一致"] = page["候选数"] == page["已记录去向数"]
        pagination = {}
        if site_key in ("shandong_ggzy", "ccgp"):
            for keyword in KEYWORDS:
                state = store.load_progress(site_key, keyword, start_date, end_date)
                last_page = state["last_page"]
                pagination[keyword] = {
                    "下一页": state["next_page"],
                    "预计末页": last_page,
                    "待补采页": state["pending_pages"],
                    "待补详情": state["pending_details"],
                    "分页完整": last_page is not None
                    and state["next_page"] > last_page
                    and not state["pending_pages"]
                    and not state["pending_details"],
                }
        stats = {
            "reported_total": crawler.reported_total if crawler else None,
            "found": len(crawler.found_urls) if crawler else 0,
            "cached": statuses.count("跨次缓存命中，跳过详情"),
            "success": len(store.run_records(site_key, RUN_DIR)),
            "failed": sum(item.get("状态") not in (
                "成功", "跨次缓存命中，跳过详情", "本次运行链接重复",
                "发布日期超出范围", "详情页发布日期超出指定范围",
                "搜索结果无发布日期", "列表中无发布日期",
                "达到PER_KEYWORD上限，未处理", "重复列表页，停止翻页",
            ) for item in audit if "页内序号" in item) if site_key in
            ("shandong_ggzy", "ccgp") else sum(bool(status) and status not in (
                "列表页", "成功", "跨次缓存命中，跳过详情", "发布日期超出范围",
                "搜索结果无发布日期", "列表中无发布日期",
            ) for status in statuses),
            "detail_attempts": policy.details,
            "pages": list(page_stats.values()),
            "pagination": pagination,
        }
        (RUN_DIR / "采集统计.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"[{site_key}] 统计：网站总量{stats['reported_total']}、发现{stats['found']}、缓存{stats['cached']}、"
              f"成功{stats['success']}、失败{stats['failed']}")

        try:
            path, count = _export_run(store, site_key, RUN_DIR, KEYWORDS)
            print(f"\n已导出 {count} 条：{path}")

        except Exception as exc:

            print(f"JSON导出失败：{exc}")

        store.close()
        print(f"输出目录：{RUN_DIR}")
