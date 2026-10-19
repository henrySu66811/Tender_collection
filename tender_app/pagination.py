"""Persisted, bounded pagination for public search sites."""

import math
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .config import PAGE_TIMEOUT_RETRIES, PAGE_RETRY_BACKOFF_SECONDS


class PageProgress:
    def __init__(self, store, site, keyword, start_date, end_date):
        self.store = store
        self.key = (site, keyword, start_date, end_date)
        self.data = store.load_progress(*self.key)
        self.repaired_this_run = set()
        self.consecutive_timeouts = 0

    def save(self):
        self.store.save_progress(*self.key, self.data)

    def target(self):
        page = self.data['next_page']
        last = self.data['last_page']
        if last is None or page <= last:
            return page, False
        for pending in self.data['pending_pages']:
            if pending not in self.repaired_this_run:
                self.repaired_this_run.add(pending)
                return pending, True
        return None, False

    def set_total(self, total, page_size=20):
        if total is not None:
            self.data['last_page'] = max(1, math.ceil(total / page_size))
            self.save()

    def end_at(self, page):
        self.data['last_page'] = max(0, page - 1)
        self.data['next_page'] = max(self.data['next_page'], page + 1)
        self.save()

    def failed(self, page, repairing=False):
        if page not in self.data['pending_pages']:
            self.data['pending_pages'].append(page)
            self.data['pending_pages'].sort()
        if not repairing:
            self.data['next_page'] = page + 1
        self.consecutive_timeouts += 1
        self.save()

    def completed(self, page, repairing=False, retry_needed=False):
        retry_needed = retry_needed or any(
            item['page'] == page for item in self.data['pending_details']
        )
        if retry_needed:
            if page not in self.data['pending_pages']:
                self.data['pending_pages'].append(page)
                self.data['pending_pages'].sort()
        else:
            self.data['pending_pages'] = [value for value in self.data['pending_pages']
                                          if value != page]
        if not repairing:
            self.data['next_page'] = page + 1
        self.consecutive_timeouts = 0
        self.save()

    def failed_detail(self, page, item):
        url = item['url']
        if not any(value['url'] == url for value in self.data['pending_details']):
            self.data['pending_details'].append({
                'page': page, 'url': url, 'title': item.get('title', ''),
            })
            self.save()

    def resolved_detail(self, url):
        remaining = [item for item in self.data['pending_details']
                     if item['url'] != url]
        if len(remaining) != len(self.data['pending_details']):
            self.data['pending_details'] = remaining
            self.save()


def open_with_timeout_retries(open_page, policy, page_number, audit, keyword):
    """Retry only navigation timeouts; never retry a policy stop or HTTP denial."""
    for attempt in range(PAGE_TIMEOUT_RETRIES + 1):
        policy.check()
        try:
            open_page()
            return True
        except PlaywrightTimeoutError as exc:
            policy.check()
            audit.append({'关键词': keyword, '页码': page_number,
                          '状态': '列表页超时', '尝试次数': attempt + 1,
                          '原因': str(exc)})
            if attempt == PAGE_TIMEOUT_RETRIES:
                return False
            time.sleep(PAGE_RETRY_BACKOFF_SECONDS * (attempt + 1))
