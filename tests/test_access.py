import tempfile
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tender_app.access import (AccessPolicy, AccessStopped, AccessStore, RobotsRules)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AccessStore(Path(self.tmp.name)/'cache.db')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def policy(self, **kwargs):
        return AccessPolicy(self.store, 'site', interval=0, jitter=0, **kwargs)

    def test_robots_specific_and_wildcards(self):
        r = RobotsRules('User-agent: *\nDisallow: /*.do\nAllow: /search.do*\nDisallow: /private/\nAllow: /private/public$')
        self.assertTrue(r.allows('https://x/search.do?q=a'))
        self.assertFalse(r.allows('https://x/test.do'))
        self.assertTrue(r.allows('https://x/private/public'))
        self.assertFalse(r.allows('https://x/private/public/extra'))

    def test_agent_merge_unicode_and_allow_tie(self):
        r = RobotsRules('User-agent: *\nDisallow: /\nUser-agent: TenderPracticeBot\nDisallow: /中文\nUser-agent: TenderPracticeBot\nDisallow: /same\nAllow: /same')
        self.assertTrue(r.allows('https://x/other'))
        self.assertFalse(r.allows('https://x/%E4%B8%AD%E6%96%87'))
        self.assertTrue(r.allows('https://x/same'))

    def test_budget_and_detail_limit(self):
        p = self.policy(max_requests=1, max_details=1)
        p.slot()
        with self.assertRaises(AccessStopped): p.slot()
        self.assertEqual(p.count,1)
        p = self.policy(max_details=1)
        p.before_detail()
        with self.assertRaises(AccessStopped): p.before_detail()

    def test_interval_and_crawl_delay(self):
        p = self.policy()
        p.last = 100
        with patch('tender_app.access.time.monotonic', return_value=101), patch('tender_app.access.time.sleep') as sleep:
            p.slot(delay=7)
            sleep.assert_called_once_with(6)

    def test_retry_after_persists(self):
        p = self.policy()
        with self.assertRaises(AccessStopped):
            p.status(SimpleNamespace(status=429, headers={'retry-after':'7200'}))
        self.assertGreater(self.store.cooldown('site'), time.time()+7190)
        self.store.close()
        self.store=AccessStore(Path(self.tmp.name)/'cache.db')
        with self.assertRaises(AccessStopped): self.policy().check()

    def test_cache_site_and_expiry(self):
        self.store.save('a','1',{'title':'公告'})
        self.assertTrue(self.store.cached('a','1',100))
        self.assertFalse(self.store.cached('b','1',100))
        self.assertFalse(self.store.cached('a','1',0))

    def test_robots_unknown_fail_closed(self):
        response=SimpleNamespace(status=503, headers={}, dispose=lambda:None)
        context=SimpleNamespace(request=SimpleNamespace(get=lambda *a,**k:response))
        with self.assertRaises(AccessStopped): self.policy().robots(context,'https://x/page')

    def test_robots_retries_transient_failure_with_longer_timeout(self):
        response = SimpleNamespace(
            status=200,
            headers={'content-type': 'text/plain'},
            text=lambda: 'User-agent: *\nAllow: /',
            dispose=lambda: None,
        )
        calls = []

        def get(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) < 3:
                raise TimeoutError('temporary timeout')
            return response

        context = SimpleNamespace(request=SimpleNamespace(get=get))
        policy = self.policy(robots_timeout=60000, robots_attempts=3)
        rules = policy.robots(context, 'https://x/page')
        self.assertTrue(rules.allows('https://x/page'))
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call['timeout'] == 60000 for call in calls))
        failures = [event for event in policy.events
                    if event['event'] == 'robots_request_failed']
        self.assertEqual([event['attempt'] for event in failures], [1, 2])
        self.assertEqual(policy.count, 3)

    def test_robots_stops_after_all_retry_attempts_fail(self):
        calls = []

        def get(*args, **kwargs):
            calls.append(kwargs)
            raise TimeoutError('still unavailable')

        context = SimpleNamespace(request=SimpleNamespace(get=get))
        policy = self.policy(robots_attempts=3)
        with self.assertRaises(AccessStopped):
            policy.robots(context, 'https://x/page')
        self.assertEqual(len(calls), 3)
        self.assertIn('连续失败 3 次', policy.reason)

    def test_route_block_and_response_stop(self):
        p=self.policy()
        p.rules['https://x']=RobotsRules('User-agent: *\nDisallow: /deny')
        handlers={}
        context=SimpleNamespace(route=lambda pattern,fn:handlers.update(route=fn),route_web_socket=lambda *a:None)
        p.attach(context)
        calls=[]
        route=SimpleNamespace(request=SimpleNamespace(url='https://x/deny',method='GET'),abort=lambda:calls.append('abort'),fetch=lambda **k:calls.append('fetch'))
        handlers['route'](route)
        self.assertEqual(calls,['abort'])
        p=self.policy();p.rules['https://x']=RobotsRules('')
        p.attach(context)
        route.request.url='https://x/ok'
        route.fetch=lambda **k:SimpleNamespace(status=403,headers={},dispose=lambda:None)
        handlers['route'](route)
        self.assertIn('403',p.reason)

    def test_subresource_fetch_error_does_not_stop_collection(self):
        p = self.policy()
        p.rules['https://x'] = RobotsRules('')
        handlers, calls = {}, []
        context = SimpleNamespace(route=lambda pattern, fn: handlers.update(route=fn),
                                  route_web_socket=lambda *a: None)
        p.attach(context)
        def fail(**kwargs):
            raise RuntimeError('request canceled')
        route = SimpleNamespace(
            request=SimpleNamespace(url='https://x/favicon.ico', method='GET',
                                    resource_type='other'),
            abort=lambda: calls.append('abort'), fetch=fail)
        handlers['route'](route)
        self.assertEqual(calls, ['abort'])
        self.assertEqual(p.reason, '')
        self.assertEqual(p.events[-1]['event'], 'request_failed')
        self.assertEqual(p.events[-1]['error'], 'request canceled')

    def test_native_request_preserves_browser_request(self):
        p = self.policy(native_requests=True)
        p.rules['https://x'] = RobotsRules('')
        handlers, calls = {}, []
        context = SimpleNamespace(
            route=lambda pattern, fn: handlers.update(route=fn),
            route_web_socket=lambda *a: None,
            on=lambda event, fn: handlers.update(response=fn))
        p.attach(context)
        class EarlyNavigation:
            url = 'https://x/s'
            method = 'POST'
            resource_type = 'document'

            @property
            def frame(self):
                raise RuntimeError('Frame for this navigation request is not available')

        route = SimpleNamespace(
            request=EarlyNavigation(),
            abort=lambda: calls.append('abort'),
            continue_=lambda: calls.append('continue'),
            fetch=lambda **kwargs: calls.append('fetch'))
        handlers['route'](route)
        self.assertEqual(calls, ['continue'])
        self.assertEqual(p.reason, '')

    def test_passthrough_subresources_are_not_fetched_or_delayed(self):
        p = self.policy(native_requests=True, passthrough_subresources=True)
        handlers, calls = {}, []
        context = SimpleNamespace(
            route=lambda pattern, fn: handlers.update(route=fn),
            on=lambda event, fn: handlers.update(response=fn),
            route_web_socket=lambda *args: calls.append('websocket'))
        p.attach(context)
        route = SimpleNamespace(
            request=SimpleNamespace(
                url='https://cdn.example.test/static/app.js',
                method='GET', resource_type='script'),
            continue_=lambda: calls.append('continue'),
            fetch=lambda **kwargs: calls.append('fetch'),
            abort=lambda: calls.append('abort'))
        handlers['route'](route)
        self.assertEqual(calls, ['continue'])
        self.assertEqual(p.count, 0)

    def test_external_resource_never_requests_robots_or_cools_target(self):
        p = self.policy(native_requests=True, allowed_domain='example.test')
        handlers, calls = {}, []
        context = SimpleNamespace(
            route=lambda pattern, fn: handlers.update(route=fn),
            route_web_socket=lambda *a: None,
            on=lambda event, fn: handlers.update(response=fn))
        p.attach(context)
        for url in ('https://aqyzmedia.yunaq.com/logo.png',
                    'https://example.test.other.example/logo.png'):
            handlers['route'](SimpleNamespace(
                request=SimpleNamespace(url=url, method='GET', resource_type='image'),
                abort=lambda: calls.append('abort')))
        self.assertEqual(calls, ['abort', 'abort'])
        self.assertEqual(p.count, 0)
        handlers['response'](SimpleNamespace(
            url='https://aqyzmedia.yunaq.com/logo.png', status=403))
        self.assertEqual(p.reason, '')
        self.assertEqual(self.store.cooldown('site'), 0)
        handlers['response'](SimpleNamespace(
            url='https://zb.example.test/detail.html', status=403, headers={}))
        self.assertIn('https://zb.example.test/detail.html', p.reason)
        self.assertGreater(self.store.cooldown('site'), time.time())

    def test_resources_follow_origin_rules_not_resource_type(self):
        p = self.policy(native_requests=True)
        p.rules['https://www.example.test'] = RobotsRules(
            'User-agent: *\nDisallow: /*.js\nDisallow: /*.css')
        p.rules['https://static.example'] = RobotsRules('User-agent: *\nAllow: /')
        handlers, calls = {}, []
        context = SimpleNamespace(
            route=lambda pattern, fn: handlers.update(route=fn),
            route_web_socket=lambda *a: None, on=lambda *a: None)
        p.attach(context)
        cases = [
            ('https://www.example.test/logo.png', 'image', 'continue'),
            ('https://www.example.test/main.css', 'stylesheet', 'abort'),
            ('https://www.example.test/main.js', 'script', 'abort'),
            ('https://static.example/main.css', 'stylesheet', 'continue'),
            ('https://static.example/main.js', 'script', 'continue'),
        ]
        for url, kind, expected in cases:
            with self.subTest(url=url):
                calls.clear()
                handlers['route'](SimpleNamespace(
                    request=SimpleNamespace(url=url, method='GET', resource_type=kind),
                    abort=lambda: calls.append('abort'),
                    continue_=lambda: calls.append('continue')))
                self.assertEqual(calls, [expected])
                self.assertEqual(p.reason, '')
        self.assertEqual(p.count, 3)




    def test_robots_521_records_diagnostics_without_credentials(self):
        p = self.policy()
        p.rules['https://www.example.test'] = RobotsRules('')
        response = SimpleNamespace(status=521, headers={
            'content-type': 'text/html', 'server': 'test',
            'set-cookie': 'secret'}, dispose=lambda: None)
        context = SimpleNamespace(request=SimpleNamespace(get=lambda *a, **k: response))
        with self.assertRaises(AccessStopped):
            p.robots(context, 'https://zb.example.test/detail.html')
        self.assertNotIn('https://zb.example.test', p.rules)
        event = p.events[0]
        self.assertEqual(event['url'], 'https://zb.example.test/robots.txt')
        self.assertEqual(event['status'], 521)
        self.assertNotIn('set-cookie', event['headers'])


if __name__ == '__main__':
    unittest.main()
