"""Headless browser fetcher logic with scripted CDP replies and events."""
import base64
import collections
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from fanficfare import exceptions
from fanficfare.fetchers import cache_browser
from fanficfare.fetchers.base_fetcher import FetcherResponse
from fanficfare.fetchers.cache_browser import BrowserCacheDecorator
from fanficfare.fetchers.cdp import CDPCommandError
from fanficfare.fetchers.fetcher_headless_browser import (
    HEADLESS_CHALLENGE_SECONDS, RECHALLENGE_SECONDS, ChallengeNotPassed,
    HeadlessBrowserFetcher, ManagedBrowser, cookie_param, encode_text, fetch_page,
    find_browser, is_challenge, wait_for_document)

SESSION = 'S1'
FRAME = 'F1'
URL = 'https://www.fanfiction.net/s/1/1/'
CHALLENGE = {'Cf-Mitigated':'challenge', 'server':'cloudflare'}


def document(request_id, loader_id, status=200, headers=None, charset='utf-8',
             session=SESSION, frame=FRAME):
    return {'method':'Network.responseReceived', 'sessionId':session,
            'params':{'requestId':request_id, 'loaderId':loader_id, 'frameId':frame,
                      'type':'Document',
                      'response':{'url':URL, 'status':status, 'statusText':'',
                                  'headers':headers or {}, 'charset':charset}}}


def finished(request_id, session=SESSION):
    return {'method':'Network.loadingFinished', 'sessionId':session,
            'params':{'requestId':request_id}}


def failed(request_id, canceled):
    return {'method':'Network.loadingFailed', 'sessionId':SESSION,
            'params':{'requestId':request_id, 'canceled':canceled, 'errorText':'net::ERR_FAILED'}}


class FakeConnection(object):
    """Stands in for CDPConnection: canned replies, scripted events."""
    def __init__(self, replies=None, script=()):
        self.replies = replies or {}
        self.script = collections.deque(script)
        self.events = collections.deque()
        self.calls = []
        self.deadlines = []

    def call(self, method, params=None, session_id=None, timeout=30):
        self.calls.append(method)
        reply = self.replies.get(method, {})
        if callable(reply):
            reply = reply(self)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def next_event(self, deadline):
        self.deadlines.append(deadline)
        if self.events:
            return self.events.popleft()
        if self.script:
            return self.script.popleft()
        return None


class WaitForDocumentTest(unittest.TestCase):
    def wait(self, *script):
        return wait_for_document(FakeConnection(script=script), SESSION, FRAME, 'L1', 0, 20)

    def test_challenge_shortens_deadline(self):
        conn = FakeConnection(script=[document('R1', 'L1', 403, CHALLENGE), finished('R1')])
        start = time.time()
        wait_for_document(conn, SESSION, FRAME, 'L1', start + 3600, 20)
        self.assertEqual(conn.deadlines[0], start + 3600)
        self.assertLessEqual(conn.deadlines[-1], time.time() + 20)

    def test_plain_page(self):
        found = self.wait(document('R1', 'L1'), finished('R1'))
        self.assertEqual(found['requestId'], 'R1')

    def test_challenge_replaced_by_page(self):
        found = self.wait(document('R1', 'L1', 403, CHALLENGE), finished('R1'),
                          document('R2', 'L2'), finished('R2'))
        self.assertEqual(found['requestId'], 'R2')

    def test_unsolved_challenge_returned_at_deadline(self):
        found = self.wait(document('R1', 'L1', 403, CHALLENGE), finished('R1'))
        self.assertTrue(is_challenge(found['response']))

    def test_other_sessions_frames_and_navigations_ignored(self):
        found = self.wait(document('R0', 'L0'), finished('R0'),
                          document('R9', 'L1', session='S9'), finished('R9', session='S9'),
                          document('R8', 'L1', frame='F8'), finished('R8'),
                          document('R1', 'L1'), finished('R1'))
        self.assertEqual(found['requestId'], 'R1')

    def test_canceled_load_waits_for_next(self):
        found = self.wait(document('R1', 'L1'), failed('R1', canceled=True),
                          document('R2', 'L1'), finished('R2'))
        self.assertEqual(found['requestId'], 'R2')

    def test_failed_load_raises(self):
        with self.assertRaises(exceptions.HTTPErrorFFF):
            self.wait(document('R1', 'L1'), failed('R1', canceled=False))

    def test_nothing_by_deadline(self):
        self.assertIsNone(self.wait())


class NavigateTest(unittest.TestCase):
    def navigate(self, replies, script=()):
        browser = ManagedBrowser('chrome', 'profile')
        browser.conn = FakeConnection(replies, script)
        browser.session_id = SESSION
        return browser.navigate(URL, None, 0)

    def test_text_body_encoded_in_page_charset(self):
        replies = {'Page.navigate':{'frameId':FRAME, 'loaderId':'L1'},
                   'Network.getResponseBody':{'body':'café', 'base64Encoded':False}}
        resp = self.navigate(replies, [document('R1', 'L1', charset='windows-1252'), finished('R1')])
        self.assertEqual(resp.content, b'caf\xe9')
        self.assertEqual(resp.redirecturl, URL)
        self.assertFalse(resp.fromcache)

    def test_binary_body(self):
        replies = {'Page.navigate':{'frameId':FRAME, 'loaderId':'L1'},
                   'Network.getResponseBody':{'body':base64.b64encode(b'\x89PNG').decode(),
                                              'base64Encoded':True}}
        resp = self.navigate(replies, [document('R1', 'L1', charset=''), finished('R1')])
        self.assertEqual(resp.content, b'\x89PNG')

    def test_http_error_status(self):
        replies = {'Page.navigate':{'frameId':FRAME, 'loaderId':'L1'},
                   'Network.getResponseBody':{'body':'gone', 'base64Encoded':False}}
        with self.assertRaises(exceptions.HTTPErrorFFF) as raised:
            self.navigate(replies, [document('R1', 'L1', status=404), finished('R1')])
        self.assertEqual(raised.exception.status_code, 404)

    def test_error_page_keeps_http_status(self):
        def navigate(conn):
            ## the response arrives before Page.navigate's reply
            conn.events.append(document('R1', 'L1', status=404))
            return {'frameId':FRAME, 'loaderId':'L1', 'errorText':'net::ERR_HTTP_RESPONSE_CODE_FAILURE'}
        with self.assertRaises(exceptions.HTTPErrorFFF) as raised:
            self.navigate({'Page.navigate':navigate})
        self.assertEqual(raised.exception.status_code, 404)

    def test_navigation_error(self):
        replies = {'Page.navigate':CDPCommandError('Page.navigate failed: Cannot navigate to invalid URL')}
        with self.assertRaises(exceptions.HTTPErrorFFF) as raised:
            self.navigate(replies)
        self.assertEqual(raised.exception.status_code, 428)

    def test_unsolved_challenge(self):
        replies = {'Page.navigate':{'frameId':FRAME, 'loaderId':'L1'}}
        with self.assertRaises(ChallengeNotPassed) as raised:
            self.navigate(replies, [document('R1', 'L1', 403, CHALLENGE), finished('R1')])
        ## still an HTTPErrorFFF 428 for FFF when there's no window fallback
        self.assertIsInstance(raised.exception, exceptions.HTTPErrorFFF)
        self.assertEqual(raised.exception.status_code, 428)
        self.assertIn('%s seconds' % HEADLESS_CHALLENGE_SECONDS, raised.exception.error_msg)


class CloseTest(unittest.TestCase):
    """The browser closes once no other FFF process has a tab in it."""
    def close(self, *tabs):
        browser = ManagedBrowser('chrome', 'profile')
        browser.conn = conn = FakeConnection({'Target.getTargets':{'targetInfos':[
            {'targetId':target_id, 'type':'page', 'attached':attached}
            for (target_id, attached) in tabs]}})
        browser.target_id = 'MINE'
        browser.session_id = SESSION
        with patch('fanficfare.fetchers.fetcher_headless_browser.time.sleep'):
            browser.close()
        self.assertIsNone(browser.conn)
        return conn.calls

    def test_own_just_closed_tab_ignored(self):
        ## still listed as attached right after Target.closeTarget
        calls = self.close(('MINE', True), ('STARTUP', False))
        self.assertIn('Browser.close', calls)

    def test_other_process_tab_keeps_browser(self):
        calls = self.close(('MINE', True), ('OTHER', True))
        self.assertNotIn('Browser.close', calls)
        self.assertEqual(calls.count('Target.getTargets'), 2)


class FakeBrowser(object):
    """Stands in for ManagedBrowser in fetch_page()."""
    COOKIES = [{'name':'cf_clearance', 'value':'x', 'domain':'.fanfiction.net'}]

    def __init__(self, *results):
        self.results = list(results)
        self.fetched = []
        self.cookies_set = []
        self.closed = 0
        self.keep_visible = False
        self.cookies_copied = 0

    def fetch(self, url, referer, timeout):
        self.fetched.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_cookies(self, url):
        return self.COOKIES

    def set_cookies(self, cookies):
        self.cookies_set.append(cookies)

    def close(self):
        self.closed += 1
        self.keep_visible = False


def page(text):
    return FetcherResponse(text, URL)


def challenged():
    return ChallengeNotPassed(URL, 428, 'challenge')


class ChallengeWindowTest(unittest.TestCase):
    def fetch(self, headless, visible):
        return fetch_page(headless, visible, URL, None, 60, 300)

    def test_headless_page_needs_no_window(self):
        (headless, visible) = (FakeBrowser(page(b'headless')), FakeBrowser())
        self.assertEqual(self.fetch(headless, visible).content, b'headless')
        self.assertEqual(visible.fetched, [])

    def test_challenge_solved_in_window_then_cookies_copied(self):
        (headless, visible) = (FakeBrowser(challenged()), FakeBrowser(page(b'visible')))
        self.assertEqual(self.fetch(headless, visible).content, b'visible')
        self.assertEqual((headless.fetched, visible.fetched), ([60], [300]))
        self.assertEqual(headless.cookies_set, [FakeBrowser.COOKIES])
        self.assertEqual(visible.closed, 1)
        self.assertFalse(visible.keep_visible)

    def test_challenged_again_soon_keeps_window(self):
        headless = FakeBrowser(challenged(), challenged())
        visible = FakeBrowser(page(b'first'), page(b'second'), page(b'third'))
        self.fetch(headless, visible)
        self.assertEqual(self.fetch(headless, visible).content, b'second')
        self.assertTrue(visible.keep_visible)
        self.assertEqual(visible.closed, 1)
        ## from now on straight to the window
        self.assertEqual(self.fetch(headless, visible).content, b'third')
        self.assertEqual(len(headless.fetched), 2)

    def test_challenged_again_later_copies_again(self):
        (headless, visible) = (FakeBrowser(challenged()), FakeBrowser(page(b'visible')))
        visible.cookies_copied = time.time() - RECHALLENGE_SECONDS - 1
        self.fetch(headless, visible)
        self.assertEqual(len(headless.cookies_set), 1)
        self.assertFalse(visible.keep_visible)

    def test_window_disabled(self):
        with self.assertRaises(ChallengeNotPassed):
            self.fetch(FakeBrowser(challenged()), None)

    def test_cookie_param(self):
        persistent = {'name':'cf_clearance', 'value':'v', 'domain':'.fanfiction.net', 'path':'/',
                      'expires':1790000000.5, 'size':42, 'httpOnly':True, 'secure':True,
                      'session':False, 'sameSite':'None', 'priority':'Medium'}
        self.assertEqual(cookie_param(persistent),
                         {'name':'cf_clearance', 'value':'v', 'domain':'.fanfiction.net', 'path':'/',
                          'expires':1790000000.5, 'httpOnly':True, 'secure':True,
                          'sameSite':'None', 'priority':'Medium'})
        session = dict(persistent, session=True, expires=-1)
        self.assertNotIn('expires', cookie_param(session))


class HelpersTest(unittest.TestCase):
    def test_encode_text_falls_back_to_utf8(self):
        self.assertEqual(encode_text('€', 'iso-8859-1'), '€'.encode('utf-8'))
        self.assertEqual(encode_text('x', 'no-such-charset'), b'x')
        self.assertEqual(encode_text('x', None), b'x')

    def test_find_browser_path_setting(self):
        with tempfile.NamedTemporaryFile(delete=False) as browser:
            pass
        self.addCleanup(os.remove, browser.name)
        self.assertEqual(find_browser(browser.name), browser.name)
        with self.assertRaises(exceptions.FailedToDownload):
            find_browser(browser.name + '.missing')

    def test_post_not_supported(self):
        fetcher = HeadlessBrowserFetcher(lambda key, default=None: default, lambda key, default=None: [])
        with self.assertRaises(exceptions.HTTPErrorFFF) as raised:
            fetcher.request('POST', URL, parameters={'a':'b'})
        self.assertEqual(raised.exception.status_code, 428)


class BrowserCacheFallThroughTest(unittest.TestCase):
    """With use_headless_browser, a browser cache miss goes to the next fetcher."""
    def request(self, config, cached=None):
        cache = MagicMock()
        cache.get_data.return_value = cached
        fetcher = MagicMock()
        fetcher.getConfig.side_effect = lambda key, default=None: config.get(key, default)
        chain = MagicMock(return_value=FetcherResponse(b'from headless browser', URL))
        ## open_pages_in_browser sleeps between cache checks.
        with patch.object(cache_browser, 'open_url') as open_url, \
             patch.object(cache_browser.time, 'sleep'), \
             patch.dict(cache_browser.domain_open_tries, clear=True):
            resp = BrowserCacheDecorator(cache).fetcher_do_request(fetcher, chain, 'GET', URL)
        return (resp, chain, open_url)

    CACHE_ONLY = {'use_browser_cache_only':True, 'open_pages_in_browser':True}

    def test_miss_goes_to_headless_browser(self):
        (resp, chain, open_url) = self.request(dict(self.CACHE_ONLY, use_headless_browser='true'))
        self.assertEqual(resp.content, b'from headless browser')
        chain.assert_called_once()
        open_url.assert_not_called()

    def test_hit_still_from_cache(self):
        (resp, chain, open_url) = self.request(dict(self.CACHE_ONLY, use_headless_browser='true'),
                                               cached=b'cached page')
        self.assertEqual(resp.content, b'cached page')
        chain.assert_not_called()

    def test_without_headless_browser_cache_only_still_fails(self):
        with self.assertRaises(exceptions.HTTPErrorFFF) as raised:
            self.request({'use_browser_cache_only':True})
        self.assertEqual(raised.exception.status_code, 428)


if __name__ == '__main__':
    unittest.main()
