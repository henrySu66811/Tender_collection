"""Browser-context access controls. Fail closed when policy cannot be determined."""
import json
import random
import re
import sqlite3
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urljoin, quote


class AccessStopped(RuntimeError):
    pass




def origin(url):
    u = urlsplit(url)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
        raise ValueError('Unsupported URL')
    return f'{u.scheme}://{u.netloc.lower()}'


def normalized_path(value):
    value = quote(value, safe="/%*?$=&;:+,~!@()'-._")
    def decode(m):
        c = chr(int(m[1], 16))
        return c if c.isascii() and (c.isalnum() or c in '-._~') else '%' + m[1].upper()
    return re.sub(r'%([0-9a-fA-F]{2})', decode, value)


class RobotsRules:
    def __init__(self, text, agent='TenderPracticeBot'):
        groups, agents, rules, delay = [], [], [], 0.0
        started = False
        for raw in text.lstrip('\ufeff').splitlines():
            line = raw.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (x.strip() for x in line.split(':', 1))
            key = key.lower()
            if key == 'user-agent':
                if started:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, started = [], [], 0.0, False
                agents.append(value.lower())
            elif agents and key in ('allow', 'disallow', 'crawl-delay'):
                started = True
                if key == 'crawl-delay':
                    try:
                        delay = max(delay, float(value))
                    except ValueError:
                        pass
                elif value.startswith('/'):
                    rules.append((normalized_path(value), key == 'allow'))
        groups.append((agents, rules, delay))
        selected = [g for g in groups if agent.lower() in g[0]]
        if not selected:
            selected = [g for g in groups if '*' in g[0]]
        self.rules = [r for g in selected for r in g[1]]
        self.delay = max([g[2] for g in selected] + [0.0])

    def allows(self, url):
        u = urlsplit(url)
        path = normalized_path((u.path or '/') + ('?' + u.query if u.query else ''))
        matches = []
        for pattern, allow in self.rules:
            end = pattern.endswith('$')
            raw = pattern[:-1] if end else pattern
            regex = '^' + '.*'.join(re.escape(x) for x in raw.split('*')) + ('$' if end else '')
            if re.search(regex, path):
                matches.append((len(raw.replace('*', '').encode()), allow))
        return max(matches)[1] if matches else True


class AccessStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=60)
        self.db.execute("PRAGMA busy_timeout=60000")
        self._initialize()

    def _initialize(self):
        """Initialize the shared cache safely when site workers start together."""
        for attempt in range(6):
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute(
                    'CREATE TABLE IF NOT EXISTS notices '
                    '(site TEXT, key TEXT, saved REAL, data TEXT, '
                    'PRIMARY KEY(site,key))'
                )
                self.db.execute(
                    'CREATE TABLE IF NOT EXISTS cooldowns '
                    '(site TEXT PRIMARY KEY, until_time REAL)'
                )
                self.db.execute(
                    'CREATE TABLE IF NOT EXISTS run_records '
                    '(run_dir TEXT, site TEXT, keyword TEXT, key TEXT, data TEXT, '
                    'exported INTEGER NOT NULL DEFAULT 0, '
                    'PRIMARY KEY(run_dir, site, keyword, key))'
                )
                self.db.execute(
                    'CREATE TABLE IF NOT EXISTS page_progress '
                    '(site TEXT, keyword TEXT, start_date TEXT, end_date TEXT, '
                    'next_page INTEGER NOT NULL, last_page INTEGER, '
                    'pending_pages TEXT NOT NULL, pending_details TEXT NOT NULL, '
                    'PRIMARY KEY(site, keyword, start_date, end_date))'
                )
                self.db.commit()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 5:
                    raise
                time.sleep(0.25 * (attempt + 1))

    def cached(self, site, key, ttl):
        row = self.db.execute('SELECT saved FROM notices WHERE site=? AND key=?', (site, key)).fetchone()
        return bool(row and time.time() - row[0] < ttl)

    def load_progress(self, site, keyword, start_date, end_date):
        row = self.db.execute(
            'SELECT next_page, last_page, pending_pages, pending_details '
            'FROM page_progress WHERE site=? AND keyword=? AND start_date=? AND end_date=?',
            (site, keyword, start_date or '', end_date or ''),
        ).fetchone()
        if row is None:
            return {'next_page': 1, 'last_page': None, 'pending_pages': [],
                    'pending_details': []}
        return {'next_page': row[0], 'last_page': row[1],
                'pending_pages': json.loads(row[2]),
                'pending_details': json.loads(row[3])}

    def save_progress(self, site, keyword, start_date, end_date, progress):
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO page_progress VALUES(?,?,?,?,?,?,?,?)',
                (site, keyword, start_date or '', end_date or '',
                 progress['next_page'], progress['last_page'],
                 json.dumps(progress['pending_pages']),
                 json.dumps(progress['pending_details'], ensure_ascii=False)),
            )

    def save(self, site, key, record):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO notices VALUES(?,?,?,?)', (site, key, time.time(), json.dumps(record, ensure_ascii=False)))

    def save_completed(self, site, key, record, run_dir, keyword):
        """Commit the cache entry and its recoverable output record together."""
        data = json.dumps(record, ensure_ascii=False)
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO run_records '
                '(run_dir, site, keyword, key, data, exported) VALUES(?,?,?,?,?,0)',
                (str(run_dir), site, keyword, key, data),
            )
            self.db.execute(
                'INSERT OR REPLACE INTO notices VALUES(?,?,?,?)',
                (site, key, time.time(), data),
            )

    def pending_runs(self, site):
        return [row[0] for row in self.db.execute(
            'SELECT DISTINCT run_dir FROM run_records WHERE site=? AND exported=0',
            (site,),
        )]

    def run_records(self, site, run_dir):
        return [
            (keyword, json.loads(data)) for keyword, data in self.db.execute(
                'SELECT keyword, data FROM run_records WHERE site=? AND run_dir=? ORDER BY rowid',
                (site, str(run_dir)),
            )
        ]

    def mark_exported(self, site, run_dir):
        with self.db:
            self.db.execute(
                'UPDATE run_records SET exported=1 WHERE site=? AND run_dir=?',
                (site, str(run_dir)),
            )

    def cooldown(self, site):
        row = self.db.execute('SELECT until_time FROM cooldowns WHERE site=?', (site,)).fetchone()
        return row[0] if row else 0

    def set_cooldown(self, site, until):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cooldowns VALUES(?,?)', (site, max(until, self.cooldown(site))))

    def close(self):
        self.db.close()




class AccessPolicy:
    def __init__(self, store, site, *, interval=3, jitter=2, max_requests=500,
                 max_details=20, cooldown=3600,
                 native_requests=False, allowed_domain=None,
                 passthrough_subresources=False,
                 robots_timeout=60000, robots_attempts=3):
        self.store, self.site = store, site
        self.interval, self.jitter = interval, jitter
        self.max_requests, self.max_details, self.cooldown = max_requests, max_details, cooldown
        self.native_requests = native_requests
        self.allowed_domain = allowed_domain
        self.passthrough_subresources = passthrough_subresources
        self.robots_timeout = robots_timeout
        self.robots_attempts = max(1, robots_attempts)
        self.count = self.details = 0
        self.last = 0.0
        self.reason = ''
        self.rules = {}
        self.events = []

    def stop(self, reason):
        if not self.reason:
            self.reason = reason
            self.events.append({'event': 'stopped', 'reason': reason})
            print('\n访问控制暂停：' + reason)
        raise AccessStopped(self.reason)

    def check(self):
        if self.reason:
            raise AccessStopped(self.reason)
        until = self.store.cooldown(self.site)
        if until > time.time():
            self.stop(f'网站仍在冷却期，至少还需等待 {int(until-time.time())+1} 秒；不会自动重试')

    def slot(self, delay=0):
        self.check()
        if self.count >= self.max_requests:
            self.stop(f'已达到本次HTTP请求上限 {self.max_requests}')
        wait = max(self.interval, delay) + random.uniform(0, self.jitter) - (time.monotonic()-self.last)
        if wait > 0:
            time.sleep(wait)
        self.count += 1
        self.last = time.monotonic()

    def before_detail(self):
        self.check()
        if self.details >= self.max_details:
            self.stop(f'已达到本次详情尝试上限 {self.max_details}')
        self.details += 1

    def in_scope(self, url):
        if not self.allowed_domain:
            return True
        host = (urlsplit(url).hostname or '').lower()
        return host == self.allowed_domain or host.endswith('.' + self.allowed_domain)

    def status(self, response):
        if response.status not in (403, 429):
            return
        seconds = self.cooldown
        retry = response.headers.get('retry-after', '')
        try:
            seconds = max(seconds, float(retry))
        except ValueError:
            try:
                seconds = max(seconds, parsedate_to_datetime(retry).timestamp()-time.time())
            except (ValueError, TypeError, OverflowError):
                pass
        self.store.set_cooldown(self.site, time.time()+seconds)
        address = getattr(response, 'url', '')
        self.events.append({'event': 'http_denied', 'url': address, 'status': response.status})
        self.stop(f'收到HTTP {response.status}（{address or "地址未提供"}）；冷却至少 {int(seconds)} 秒，结束本次采集')

    def robots(self, context, url):
        host = origin(url)
        if host in self.rules:
            return self.rules[host]
        robots_url = host + '/robots.txt'
        response = None
        for attempt in range(1, self.robots_attempts + 1):
            self.slot()
            try:
                response = context.request.get(
                    robots_url,
                    max_redirects=0,
                    timeout=self.robots_timeout,
                )
                break
            except Exception as exc:
                self.events.append({
                    'event': 'robots_request_failed',
                    'url': robots_url,
                    'attempt': attempt,
                    'max_attempts': self.robots_attempts,
                    'timeout_ms': self.robots_timeout,
                    'error_type': type(exc).__name__,
                    'error': str(exc),
                })
                if attempt >= self.robots_attempts:
                    self.stop(
                        f'robots请求连续失败 {self.robots_attempts} 次：{robots_url}；'
                        f'{type(exc).__name__}；详见访问控制日志.json'
                    )
        try:
            # 只保存诊断需要的响应头，不记录Cookie或认证信息。
            self.events.append({'event': 'robots_response', 'url': robots_url,
                                'status': response.status,
                                'headers': {k: v for k, v in response.headers.items()
                                            if k.lower() in ('content-type', 'server', 'retry-after')}})
            self.status(response)
            if response.status in (404, 410):
                text = ''
            elif response.status == 200:
                text = response.text()
                if '<html' in text[:1024].lower() or len(text.encode()) > 512000:
                    self.stop(f'{host} robots响应不是可接受的规则文本')
                meaningful = '\n'.join(line.split('#', 1)[0].strip() for line in text.splitlines()).strip()
                if meaningful and not re.search(r'(?im)^\s*(?:user-agent|sitemap)\s*:', meaningful):
                    self.stop(f'{host} robots响应无法识别，未按允许访问处理')
            else:
                self.stop(f'{robots_url} 返回HTTP {response.status}，无法确认规则；'
                          '这不是明确禁止访问，也不会按允许处理。详见访问控制日志.json')
            self.rules[host] = RobotsRules(text)
            self.events.append({'event': 'robots', 'origin': host, 'status': response.status,
                                'rules_text': text})
            return self.rules[host]
        finally:
            response.dispose()

    def check_url(self, context, url):
        rules = self.robots(context, url)
        if not rules.allows(url):
            self.stop(f'robots禁止访问：{origin(url)}{urlsplit(url).path or "/"}；'
                      '该入口不能按当前规则自动采集，请使用获准的数据接口或导入本地公告')
        return rules

    def attach(self, context):
        if self.native_requests:
            def observe_response(response):
                if (self.passthrough_subresources and
                        response.request.resource_type != 'document'):
                    return
                if not self.in_scope(response.url):
                    return
                if response.status in (403, 429):
                    try:
                        self.status(response)
                    except AccessStopped:
                        # 事件回调不能把异常抛回页面；reason已设置，下一次
                        # 请求或爬虫循环会立即停止。
                        pass
            context.on('response', observe_response)

        def handle(route):
            response = None
            try:
                self.check()
                url = route.request.url
                method = route.request.method
                kind = getattr(route.request, 'resource_type', 'document')
                if self.passthrough_subresources and kind != 'document':
                    route.continue_()
                    return
                if not self.in_scope(url):
                    self.events.append({'event': 'out_of_scope_blocked', 'url': url,
                                        'type': getattr(route.request, 'resource_type', '')})
                    route.abort()
                    return
                # 文档和静态资源均按各自来源的robots规则判断，不按类型一律屏蔽。
                for hop in range(6):
                    rules = self.robots(context, url)
                    if not rules.allows(url):
                        # 禁止的子资源只中止该资源；主文档禁止时停止采集。
                        if getattr(route.request, 'resource_type', 'document') != 'document':
                            self.events.append({'event': 'robots_blocked_resource',
                                                'url': url,
                                                'type': route.request.resource_type})
                            route.abort()
                            return
                        self.stop(f'robots禁止访问：{origin(url)}{urlsplit(url).path or "/"}；'
                                  '该入口不能按当前规则自动采集，请使用获准的数据接口或导入本地公告')
                    self.slot(rules.delay)
                    if self.native_requests:
                        # 保留浏览器原生POST正文、Cookie、重定向及挑战处理。
                        # 该请求已经完成robots检查和限速。
                        route.continue_()
                        return
                    options = dict(url=url, method=method, max_redirects=0, max_retries=0, timeout=20000)
                    if method != route.request.method and method == 'GET':
                        options['post_data'] = ''
                    response = route.fetch(**options)
                    self.status(response)
                    if response.status in (301,302,303,307,308) and response.headers.get('location'):
                        target = urljoin(url, response.headers['location'])
                        # 不把原请求凭据转发到其他来源。
                        if origin(target) != origin(url):
                            self.stop('检测到跨来源重定向，需要为该站点单独适配，已暂停')
                        if response.status == 303 or (response.status in (301,302) and method == 'POST'):
                            method = 'GET'
                        url = target
                        response.dispose()
                        response = None
                        continue
                    route.fulfill(response=response)
                    return
                self.stop('重定向次数超限')
            except AccessStopped:
                route.abort()
            except Exception as exc:
                kind = getattr(route.request, 'resource_type', 'document')
                message = str(exc).strip() or repr(exc)
                event = {'event': 'request_failed',
                         'url': getattr(route.request, 'url', ''),
                         'method': getattr(route.request, 'method', ''),
                         'type': kind,
                         'error_type': type(exc).__name__,
                         'error': message}
                self.events.append(event)
                # 浏览器会主动取消favicon、统计、广告等非正文请求；这不表示
                # 搜索页或详情页失败，不应因此终止整次采集。
                if kind != 'document':
                    route.abort()
                    return
                try:
                    self.stop(f'主页面请求失败：{type(exc).__name__}: {message}')
                except AccessStopped:
                    route.abort()
            finally:
                if response is not None:
                    response.dispose()
        context.route('**/*', handle)
        if not self.passthrough_subresources:
            # WebSocket不在HTTP路由控制范围内，严格模式下不启用。
            context.route_web_socket('**/*', lambda ws: ws.close())
