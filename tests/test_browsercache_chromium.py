"""Chromium browser cache keys and entry age, using synthetic cache entries."""
import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fanficfare.browsercache import browsercache_blockfile
from fanficfare.browsercache.base_chromium import BaseChromiumCache, EPOCH_DIFFERENCE
from fanficfare.browsercache.browsercache_blockfile import BlockfileCache, _read_response_time
from fanficfare.browsercache.chromagnon.cacheData import CacheData

SITE = 'archiveofourown.org'
URL = 'https://archiveofourown.org/works/1'
KEY = '1/0/_dk_https://archiveofourown.org https://archiveofourown.org ' + URL
CROSS_SITE_KEY = '1/0/_dk_cn_https://archiveofourown.org https://archiveofourown.org ' + URL
HOUR = 3600


def chrome_time(unix_seconds):
    """Microseconds since 1601, as Chromium stores times."""
    return int((unix_seconds + EPOCH_DIFFERENCE) * 1000000)


def response_info(request_time, response_time, extra_flags=None,
                  original_response_time=None, version=3):
    """Pickled HttpResponseInfo, as Chromium saves it in stream 0."""
    flags = version
    fields = b''
    if extra_flags is not None:
        flags |= 1 << 31
        fields += struct.pack('<L', extra_flags)
    fields += struct.pack('<QQ', request_time, response_time)
    if original_response_time is not None:
        fields += struct.pack('<Q', original_response_time)
    headers = b'HTTP/1.1 200\x00content-type: text/html\x00\x00'
    payload = struct.pack('<L', flags) + fields + struct.pack('<L', len(headers)) + headers
    return struct.pack('<L', len(payload)) + payload


class FakeStream(object):
    """Stands in for chromagnon's CacheData."""
    def __init__(self, raw, headers=None):
        self.raw = raw
        if headers is not None:
            self.headers = headers
            self.type = CacheData.HTTP_HEADER
        else:
            self.type = CacheData.UNKNOWN

    def data(self):
        return self.raw


def fake_entry(key, created_ago, responded_ago, body=b'<html>story</html>', header_raw=None):
    now = time.time()
    if header_raw is None:
        responded = chrome_time(now - responded_ago)
        header_raw = response_info(responded - 1000000, responded, extra_flags=0)
    header = FakeStream(header_raw, headers={b'content-type': b'text/html'})
    return SimpleNamespace(key=key, keyToStr=lambda: key,
                           hash=0, usageCounter=0, reuseCounter=0,
                           creationTime=chrome_time(now - created_ago),
                           httpHeader=header,
                           data=[header, FakeStream(body)])


class ReadResponseTimeTest(unittest.TestCase):
    def test_with_extra_flags(self):
        self.assertEqual(_read_response_time(response_info(100, 200, extra_flags=0)), 200)

    def test_without_extra_flags(self):
        self.assertEqual(_read_response_time(response_info(100, 200)), 200)

    def test_original_response_time_not_used(self):
        raw = response_info(100, 200, extra_flags=1 << 2, original_response_time=50)
        self.assertEqual(_read_response_time(raw), 200)

    def test_unknown_version_rejected(self):
        with self.assertRaises(ValueError):
            _read_response_time(response_info(100, 200, extra_flags=0, version=4))


class ChromiumKeysTest(unittest.TestCase):
    def test_keys_include_cross_site_navigation(self):
        config = {'browser_cache_path': '.'}
        cache = BaseChromiumCache(SITE,
                                  lambda key, default=None: config.get(key, default),
                                  lambda key, default=None: [])
        keys = cache.make_keys(URL + '#anchor')
        self.assertIn(KEY, keys)
        self.assertIn(CROSS_SITE_KEY, keys)


class BlockfileLookupTest(unittest.TestCase):
    def make_cache(self, entries, age_limit='4.0'):
        config = {'browser_cache_path': '.', 'browser_cache_age_limit': age_limit}
        with patch.object(browsercache_blockfile, 'CacheBlock') as cacheblock:
            cacheblock.return_value.type = cacheblock.INDEX
            cache = BlockfileCache(SITE,
                                   lambda key, default=None: config.get(key, default),
                                   lambda key, default=None: [])
        by_key = dict((entry.key, entry) for entry in entries)
        patcher = patch.object(browsercache_blockfile, 'parse',
                               lambda path, keys: [by_key[k] for k in keys if k in by_key])
        patcher.start()
        self.addCleanup(patcher.stop)
        return cache

    def test_refreshed_entry_uses_response_time(self):
        cache = self.make_cache([fake_entry(KEY, created_ago=30*HOUR, responded_ago=1*HOUR)])
        self.assertEqual(cache.get_data(URL), b'<html>story</html>')

    def test_stale_response_still_rejected(self):
        cache = self.make_cache([fake_entry(KEY, created_ago=30*HOUR, responded_ago=10*HOUR)])
        self.assertIsNone(cache.get_data(URL))

    def test_unreadable_response_info_falls_back_to_creation_time(self):
        garbage = b'\x00' * 64
        cache = self.make_cache([fake_entry(KEY, created_ago=1*HOUR, responded_ago=0, header_raw=garbage)])
        self.assertEqual(cache.get_data(URL), b'<html>story</html>')
        cache = self.make_cache([fake_entry(KEY, created_ago=30*HOUR, responded_ago=0, header_raw=garbage)])
        self.assertIsNone(cache.get_data(URL))

    def test_cross_site_navigation_entry_found(self):
        cache = self.make_cache([fake_entry(CROSS_SITE_KEY, created_ago=1*HOUR, responded_ago=1*HOUR)])
        self.assertEqual(cache.get_data(URL), b'<html>story</html>')

    def test_newest_of_both_key_forms_used(self):
        cache = self.make_cache([
            fake_entry(KEY, created_ago=3*HOUR, responded_ago=3*HOUR, body=b'older'),
            fake_entry(CROSS_SITE_KEY, created_ago=1*HOUR, responded_ago=1*HOUR, body=b'newer'),
        ])
        self.assertEqual(cache.get_data(URL), b'newer')


if __name__ == '__main__':
    unittest.main()
