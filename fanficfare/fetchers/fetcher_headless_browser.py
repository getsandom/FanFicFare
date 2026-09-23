# -*- coding: utf-8 -*-

# Copyright 2026 FanFicFare team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

'''
Fetch pages with a headless Chrome or Edge, driven over the Chrome
DevTools Protocol, for sites that block direct requests.

The browser runs with its own profile: Chrome 136+ refuses remote
debugging on the default profile, and a profile can only be open in
one browser process anyway.  So logins and cookies are separate from
the user's own browser.

One browser serves every FFF process using the same profile (the
Calibre GUI and its background jobs): the first one launches it, the
others attach through the DevToolsActivePort file Chrome writes in the
profile.  Each process keeps one tab open while it's fetching and
closes it after IDLE_SECONDS or at exit.  The browser is closed when
no tab with a DevTools client attached is left, which also ignores
the startup tab and tabs of FFF processes that died.
'''

import atexit
import base64
import os
import shutil
import subprocess
import sys
import threading
import time

from .. import exceptions
from .base_fetcher import FetcherResponse, Fetcher
from .cdp import CDPConnection, CDPError, CDPCommandError
from .log import make_log

import logging
logger = logging.getLogger(__name__)

IDLE_SECONDS = 60
LAUNCH_TIMEOUT = 30
## Chrome's exit code when it hands its command line to a browser
## already running with the same profile.
EXIT_PROCESS_NOTIFIED = 21

class HeadlessBrowserFetcher(Fetcher):
    def __init__(self,getConfig_fn,getConfigList_fn):
        super(HeadlessBrowserFetcher,self).__init__(getConfig_fn,getConfigList_fn)
        logger.debug("using HeadlessBrowserFetcher")

    def request(self,method,url,headers=None,parameters=None):
        '''Returns a FetcherResponse regardless of mechanism'''
        if method != 'GET':
            raise exceptions.HTTPErrorFFF(
                url,
                428, # 404 & 410 trip StoryDoesNotExist
                     # 428 ('Precondition Required') gets the
                     # error_msg through to the user.
                "use_headless_browser can only make GET requests, not %s"%method)
        logger.debug(make_log('HeadlessBrowserFetcher',method,url,hit='REQ',bar='-'))
        timeout = 60.0
        try:
            timeout = float(self.getConfig('headless_browser_timeout',timeout))
        except Exception as e:
            logger.error("headless_browser_timeout setting failed: %s -- Using default value(%s)"%(e,timeout))
        browser = get_browser(find_browser(self.getConfig('headless_browser_path')),
                              self.getConfig('headless_browser_profile_path') or default_profile_dir())
        return browser.fetch(url,(headers or {}).get('Referer'),timeout)

_browsers = {}
_browsers_lock = threading.Lock()

def get_browser(browser_path,profile_dir):
    '''One HeadlessBrowser per browser/profile in this process.'''
    key = (browser_path, os.path.realpath(profile_dir))
    with _browsers_lock:
        if key not in _browsers:
            _browsers[key] = HeadlessBrowser(*key)
            atexit.register(_browsers[key].close)
        return _browsers[key]

def find_browser(path=None):
    if path:
        if os.path.isfile(path):
            return path
        raise exceptions.FailedToDownload("headless_browser_path not found: %s"%path)
    if sys.platform.startswith('win'):
        candidates = [ os.path.join(os.environ[env], *parts)
                       for parts in (('Google','Chrome','Application','chrome.exe'),
                                     ('Microsoft','Edge','Application','msedge.exe'))
                       for env in ('PROGRAMFILES','PROGRAMFILES(X86)','LOCALAPPDATA')
                       if os.environ.get(env) ]
    elif sys.platform == 'darwin':
        candidates = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                      '/Applications/Chromium.app/Contents/MacOS/Chromium']
    else:
        candidates = [ shutil.which(name) or ''
                       for name in ('google-chrome','google-chrome-stable','chromium',
                                    'chromium-browser','microsoft-edge') ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise exceptions.FailedToDownload("use_headless_browser needs Chrome or Edge, none found.  Set headless_browser_path.")

def default_profile_dir():
    if sys.platform.startswith('win'):
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base,'FanFicFare','headless-browser')
    elif sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/FanFicFare/headless-browser')
    base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base,'fanficfare','headless-browser')

class HeadlessBrowser(object):
    '''This process's connection and tab in the profile's shared browser.'''
    def __init__(self,browser_path,profile_dir):
        self.browser_path = browser_path
        self.profile_dir = profile_dir
        self.lock = threading.RLock()
        self.proc = None # only if launched by this process
        self.conn = None
        self.target_id = None
        self.session_id = None
        self.idle_timer = None
        self.last_used = 0

    def fetch(self,url,referer,timeout):
        with self.lock:
            self.cancel_idle_timer()
            try:
                for attempt in (1,2):
                    try:
                        if self.conn is None:
                            self.connect()
                        if self.session_id is None:
                            self.open_tab()
                        return self.navigate(url,referer,timeout)
                    except (CDPError,OSError) as e:
                        ## browser closed or crashed since the last request.
                        logger.warning("Headless browser connection failed(%s), attempt %s"%(e,attempt))
                        self.drop()
                        if attempt == 2:
                            raise exceptions.HTTPErrorFFF(url,428,"Headless browser failed: %s"%e)
            finally:
                self.last_used = time.time()
                self.start_idle_timer()

    def port_file_url(self):
        try:
            with open(os.path.join(self.profile_dir,'DevToolsActivePort')) as port_file:
                (port, path) = port_file.read().split('\n')[:2]
            return 'ws://127.0.0.1:%s%s'%(int(port),path.strip())
        except (OSError,ValueError):
            return None

    def connect(self):
        '''Attach to the browser running with this profile, or launch it.'''
        url = self.port_file_url()
        if url:
            try:
                self.conn = CDPConnection(url)
                logger.debug("Attached to headless browser at %s"%url)
                return
            except (CDPError,OSError) as e:
                ## Chrome leaves DevToolsActivePort behind when it exits.
                logger.debug("No headless browser at %s(%s)"%(url,e))
        self.launch(url)

    def launch(self,stale_url):
        os.makedirs(self.profile_dir,exist_ok=True)
        args = [self.browser_path,
                '--headless',
                '--remote-debugging-port=0',
                '--user-data-dir='+self.profile_dir,
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-blink-features=AutomationControlled',
                'about:blank']
        logger.debug("Launching headless browser: %s"%args)
        proc = subprocess.Popen(args,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        deadline = time.time() + LAUNCH_TIMEOUT
        while time.time() < deadline:
            code = proc.poll()
            url = self.port_file_url()
            ## another process may have launched it first (code 21).
            if url and (url != stale_url or code is not None):
                try:
                    self.conn = CDPConnection(url)
                    logger.debug("Launched headless browser at %s"%url)
                    if code is None:
                        self.proc = proc
                    return
                except (CDPError,OSError) as e:
                    logger.debug("Headless browser not ready at %s(%s)"%(url,e))
            if code is not None and code != EXIT_PROCESS_NOTIFIED:
                raise exceptions.FailedToDownload("Headless browser(%s) exited with code %s"%(self.browser_path,code))
            time.sleep(0.2)
        raise exceptions.FailedToDownload("Headless browser(%s) did not start in %s seconds"%(self.browser_path,LAUNCH_TIMEOUT))

    def attached_tabs(self):
        return [ t for t in self.conn.call('Target.getTargets')['targetInfos']
                 if t['type'] == 'page' and t['attached'] ]

    def open_tab(self):
        if self.target_id is None:
            self.target_id = self.conn.call('Target.createTarget',{'url':'about:blank'})['targetId']
        self.session_id = self.conn.call('Target.attachToTarget',
                                         {'targetId':self.target_id,'flatten':True})['sessionId']
        ## Only the User-Agent string gives headless away: it says
        ## HeadlessChrome, the client hint brands don't.  Set per tab,
        ## this also reaches the page's workers.
        user_agent = self.conn.call('Browser.getVersion')['userAgent'].replace('HeadlessChrome','Chrome')
        self.conn.call('Network.setUserAgentOverride',{'userAgent':user_agent},self.session_id)
        self.conn.call('Network.enable',{},self.session_id)
        self.conn.call('Page.enable',{},self.session_id)

    def navigate(self,url,referer,timeout):
        conn = self.conn
        session_id = self.session_id
        deadline = time.time() + timeout
        params = {'url':url}
        if referer:
            params['referrer'] = referer
        try:
            conn.events.clear()
            result = conn.call('Page.navigate',params,session_id,timeout=timeout)
            if not result.get('loaderId'):
                ## same page, new #fragment: no new document is
                ## loaded unless the tab leaves the page first.
                conn.call('Page.navigate',{'url':'about:blank'},session_id,timeout=timeout)
                conn.events.clear()
                result = conn.call('Page.navigate',params,session_id,timeout=timeout)
        except CDPCommandError as e:
            ## eg, invalid URL.  Other CDPErrors are connection
            ## problems, retried by fetch().
            raise exceptions.HTTPErrorFFF(url,428,"Headless browser: %s"%e)
        if result.get('errorText'):
            ## Chrome shows its own error page, eg for a 404 with an
            ## empty body.  Report the HTTP status if there was one.
            status = 428
            for event in conn.events:
                event_params = event.get('params',{})
                if( event['method'] == 'Network.responseReceived' and
                    event_params.get('loaderId') == result.get('loaderId') and
                    event_params.get('type') == 'Document' ):
                    status = event_params['response']['status']
            raise exceptions.HTTPErrorFFF(url,status,"Headless browser: %s"%result['errorText'])
        document = wait_for_document(conn,session_id,result['frameId'],result['loaderId'],deadline)
        if document is None:
            raise exceptions.HTTPErrorFFF(url,428,"Headless browser: no page after %s seconds"%timeout)
        if is_challenge(document['response']):
            raise exceptions.HTTPErrorFFF(url,428,"Headless browser didn't pass the Cloudflare challenge in %s seconds (see headless_browser_timeout)"%timeout)
        response = document['response']
        body = conn.call('Network.getResponseBody',{'requestId':document['requestId']},session_id)
        if body['base64Encoded']:
            content = base64.b64decode(body['body'])
        else:
            content = encode_text(body['body'],response.get('charset'))
        logger.debug("response code:%s"%response['status'])
        if response['status'] >= 400:
            raise exceptions.HTTPErrorFFF(url,
                                          response['status'],
                                          response.get('statusText') or "HTTP %s"%response['status'],
                                          content)
        return FetcherResponse(content,response['url'],False)

    def idle_close(self):
        with self.lock:
            ## a fetch may have run while this timer waited for the
            ## lock; it started a new timer.
            if time.time() - self.last_used >= IDLE_SECONDS:
                self.close()

    def close(self):
        '''Close this process's tab, and the browser if no other FFF process has one.'''
        with self.lock:
            self.cancel_idle_timer()
            if self.conn is None:
                return
            try:
                if self.target_id:
                    self.conn.call('Target.closeTarget',{'targetId':self.target_id})
                if not self.attached_tabs():
                    logger.debug("Closing headless browser")
                    self.conn.call('Browser.close')
                    if self.proc is not None:
                        self.proc.wait(10)
            except (CDPError,OSError,subprocess.TimeoutExpired) as e:
                logger.debug("Headless browser close failed: %s"%e)
            self.drop()

    def drop(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self.proc = None
        self.target_id = None
        self.session_id = None

    def start_idle_timer(self):
        self.idle_timer = threading.Timer(IDLE_SECONDS,self.idle_close)
        self.idle_timer.daemon = True
        self.idle_timer.start()

    def cancel_idle_timer(self):
        if self.idle_timer is not None:
            self.idle_timer.cancel()
            self.idle_timer = None

def wait_for_document(conn,session_id,frame_id,loader_id,deadline):
    '''
    Return the Network.responseReceived params of the page's final
    document once it has loaded.  Cloudflare challenge documents are
    skipped while they run, until their page replaces them (a new
    navigation, with its own loaderId); if that doesn't happen by the
    deadline, the challenge is returned.
    '''
    document = None
    challenge = None
    while True:
        event = conn.next_event(deadline)
        if event is None:
            return challenge
        if event.get('sessionId') != session_id:
            continue
        method = event['method']
        params = event.get('params',{})
        if method == 'Network.responseReceived':
            if( params.get('type') == 'Document' and params.get('frameId') == frame_id and
                (params.get('loaderId') == loader_id or challenge is not None) ):
                document = params
        elif document is None or params.get('requestId') != document['requestId']:
            continue
        elif method == 'Network.loadingFinished':
            if not is_challenge(document['response']):
                return document
            logger.debug("Waiting for Cloudflare challenge in headless browser")
            challenge = document
            document = None
        elif method == 'Network.loadingFailed':
            if not params.get('canceled'):
                raise exceptions.HTTPErrorFFF(document['response']['url'],428,
                                              "Headless browser: %s"%params.get('errorText'))
            ## replaced by another navigation before it finished.
            document = None

def is_challenge(response):
    ## Cloudflare marks its challenge pages, eg fanfiction.net's.
    for (name, value) in response.get('headers',{}).items():
        if name.lower() == 'cf-mitigated' and value == 'challenge':
            return True
    return False

def encode_text(text,charset):
    ## Chrome returns text decoded.  Give FFF bytes in the page's own
    ## charset so website_encodings works the same as with requests.
    try:
        return text.encode(charset or 'utf-8')
    except (LookupError,UnicodeEncodeError):
        return text.encode('utf-8')
