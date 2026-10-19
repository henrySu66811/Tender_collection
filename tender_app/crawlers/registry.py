"""网站注册表：各网站独立适配，共享访问控制和导出。"""
from dataclasses import dataclass
from .base import TenderCrawler
from .zycg import ZycgCrawler, SEARCH_URL as ZYCG_SEARCH_URL
from .shandong_ggzy import ShandongGgzyCrawler, SEARCH_URL as SHANDONG_GGZY_SEARCH_URL
from .ccgp import CcgpCrawler, SEARCH_URL as CCGP_SEARCH_URL

@dataclass(frozen=True)
class Site:
    crawler: type[TenderCrawler]
    entry_url: str
    name: str

SITES = {"zycg": Site(ZycgCrawler, ZYCG_SEARCH_URL, "中央政府采购网")}
SITES["shandong_ggzy"] = Site(
    ShandongGgzyCrawler, SHANDONG_GGZY_SEARCH_URL, "山东省公共资源交易中心"
)
SITES["ccgp"] = Site(CcgpCrawler, CCGP_SEARCH_URL, "中国政府采购网")

def get_site(name: str) -> Site:
    if name not in SITES:
        raise ValueError(f"未实现的网站 {name!r}，可用网站：{', '.join(SITES)}")
    return SITES[name]
