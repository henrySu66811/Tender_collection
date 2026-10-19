from abc import ABC, abstractmethod
from typing import Any
import re


def reported_total(page):
    """Read a search page's explicitly displayed result total, if present."""
    try:
        text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return None
    match = re.search(r"(?:共|总计|找到)\s*([\d,]+)\s*条(?:结果|记录|数据|信息)?", text)
    return int(match.group(1).replace(",", "")) if match else None

class TenderCrawler(ABC):

    """
    Interface for tender crawler implementations.
    A crawler for a specific website should inherit this class
    and implement the search method.
    """

    @abstractmethod
    def search(
        self,
        keyword: str,
        regions: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 50,

    ) -> list[dict[str, Any]]:

        """
        Search tender information for one keyword.
        Returns:

            A list of raw tender information dictionaries.

        """
        raise NotImplementedError
