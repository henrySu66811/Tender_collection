from pathlib import Path

# 添加网站后，在registry.py注册并加入SITES。
# 单站点运行时只保留一个站点。
# 同一站点不能重复，程序也会在启动前进行校验。
SITES = ["zycg"]
MAX_SITE_PROCESSES = 2
KEYWORDS = ["招标"]   
PER_KEYWORD = 22
# 山东公共资源和中国政府采购网使用这两个日期；可用 YYYY-MM-DD、today、yesterday。
# 中央政府采购网（zycg）没有日期检索选项，仍保持最近一年逻辑。
START_DATE = "2026-10-13"  # 可填 today、yesterday 或 2026-10-14 格式
END_DATE = "2026-10-13"
BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "results"
# JSON 已包含完整正文；默认不再额外保存逐条正文 TXT。
SAVE_TEXT_FILES = False

# 公共访问控制：所有HTTP资源共用总预算。
MAX_HTTP_REQUESTS = 7000
MAX_DETAIL_ATTEMPTS = 1500
# 仅列表页导航超时重试；每次仍遵守站点请求间隔和访问控制。
PAGE_TIMEOUT_RETRIES = 2
PAGE_RETRY_BACKOFF_SECONDS = 30
COOLDOWN_SECONDS = 3600
NOTICE_CACHE_TTL = 7 * 24 * 3600
CACHE_DB = BASE_DIR / "cache" / "access.sqlite3"
CRAWLER_USER_AGENT = "Mozilla/5.0 (compatible; TenderPracticeBot/1.0)"


# 中央政府采购网的列表、翻页和详情主文档采用更保守的间隔。
ZYCG_REQUEST_INTERVAL = 10.0
ZYCG_REQUEST_JITTER = 5.0

# 山东交易信息列表翻页和详情采用保守间隔。
SHANDONG_GGZY_REQUEST_INTERVAL = 10.0
SHANDONG_GGZY_REQUEST_JITTER = 5.0

# 中国政府采购网搜索风控较严格，主页面请求保持10–15秒间隔。
CCGP_REQUEST_INTERVAL = 15.0
CCGP_REQUEST_JITTER = 10.0


