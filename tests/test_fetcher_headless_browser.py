"""Headless browser fetcher logic with scripted CDP replies and events."""
import base64
import collections
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fanficfare import exceptions
from fanficfare.fetchers import cache_browser
from fanficfare.fetchers.base_fetcher import FetcherResponse
from fanficfare.fetchers.cache_browser import BrowserCacheDecorator
from fanficfare.fetchers.cdp import CDPCommandError
from fanficfare.fetchers.fetcher_headless_browser import (HeadlessBrowser, HeadlessBrowserFetcher,
                                                          encode_text, find_browser, is_challenge,
                                                          wait_for_document)

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

    def call(self, method, params=None, session_id=None, timeout=30):
        self.calls.append(method)
        reply = self.replies.get(method, {})
        if callable(reply):
            reply = reply(self)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def next_event(self, deadline):
        if self.events:
            return self.events.popleft()
        if self.script:
            return self.script.popleft()
        return None


class WaitForDocumentTest(unittest.TestCase):
    def wait(self, *script):
        return wait_for_document(FakeConnection(script=script), SESSION, FRAME, 'L1', 0)

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
        browser = HeadlessBrowser('chrome', 'profile')
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
        with self.assertRaises(exceptions.HTTPErrorFFF) as raised:
            self.navigate(replies, [document('R1', 'L1', 403, CHALLENGE), finished('R1')])
        self.assertEqual(raised.exception.status_code, 428)
        self.assertIn('Cloudflare', raised.exception.error_msg)


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
