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
Minimal Chrome DevTools Protocol (CDP) client over a WebSocket
(RFC 6455).  Only what the headless browser fetcher needs: text
messages to and from a browser listening on localhost.  Kept here
rather than depending on a WebSocket package because both the CLI
and the Calibre plugin would have to carry it.
'''

import base64
import collections
import hashlib
import json
import os
import select
import socket
import struct
import time
from urllib.parse import urlparse

import logging
logger = logging.getLogger(__name__)

WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

## once a frame or fragmented message has started, allow this long
## for the rest of it.
FRAME_TIMEOUT = 60

class CDPError(Exception):
    pass

class CDPCommandError(CDPError):
    '''The browser answered a command with an error.'''
    pass

class WebSocket(object):
    def __init__(self, url, timeout=10):
        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout)
        self.buf = bytearray()
        key = base64.b64encode(os.urandom(16))
        path = parsed.path + ('?'+parsed.query if parsed.query else '')
        self.sock.sendall(b'GET ' + path.encode('ascii') + b' HTTP/1.1\r\n' +
                          b'Host: ' + parsed.netloc.encode('ascii') + b'\r\n' +
                          b'Upgrade: websocket\r\n' +
                          b'Connection: Upgrade\r\n' +
                          b'Sec-WebSocket-Key: ' + key + b'\r\n' +
                          b'Sec-WebSocket-Version: 13\r\n\r\n')
        while b'\r\n\r\n' not in self.buf:
            self._fill(timeout)
        (head, sep, rest) = bytes(self.buf).partition(b'\r\n\r\n')
        self.buf = bytearray(rest)
        status = head.split(b'\r\n')[0]
        accept = base64.b64encode(hashlib.sha1(key + WS_GUID).digest())
        if status.split(b' ')[1:2] != [b'101'] or accept not in head:
            self.sock.close()
            raise CDPError("WebSocket handshake failed: %s" % status.decode('latin-1'))

    def _fill(self, timeout):
        if not select.select([self.sock], [], [], timeout)[0]:
            raise socket.timeout()
        data = self.sock.recv(1 << 16)
        if not data:
            raise CDPError("WebSocket connection closed")
        self.buf.extend(data)

    def _take(self, count, timeout):
        while len(self.buf) < count:
            self._fill(timeout)
        data = bytes(self.buf[:count])
        del self.buf[:count]
        return data

    def _read_frame(self, timeout):
        (byte1, byte2) = self._take(2, timeout)
        try:
            length = byte2 & 0x7F
            if length == 126:
                length = struct.unpack('>H', self._take(2, FRAME_TIMEOUT))[0]
            elif length == 127:
                length = struct.unpack('>Q', self._take(8, FRAME_TIMEOUT))[0]
            mask = self._take(4, FRAME_TIMEOUT) if byte2 & 0x80 else None
            payload = self._take(length, FRAME_TIMEOUT)
        except socket.timeout:
            raise CDPError("WebSocket frame incomplete")
        if mask:
            payload = apply_mask(payload, mask)
        return (byte1 & 0x80, byte1 & 0x0F, payload)

    def send_frame(self, opcode, payload):
        count = len(payload)
        if count < 126:
            header = struct.pack('>BB', 0x80 | opcode, 0x80 | count)
        elif count < (1 << 16):
            header = struct.pack('>BBH', 0x80 | opcode, 0x80 | 126, count)
        else:
            header = struct.pack('>BBQ', 0x80 | opcode, 0x80 | 127, count)
        ## clients must mask every frame
        mask = os.urandom(4)
        self.sock.sendall(header + mask + apply_mask(payload, mask))

    def send(self, text):
        self.send_frame(OP_TEXT, text.encode('utf-8'))

    def recv(self, timeout):
        '''Return the next text message, or None if nothing arrives in time.'''
        deadline = time.time() + timeout
        parts = []
        while True:
            wait = max(0, deadline - time.time()) if not parts else FRAME_TIMEOUT
            try:
                (fin, opcode, payload) = self._read_frame(wait)
            except socket.timeout:
                if parts:
                    raise CDPError("WebSocket message incomplete")
                return None
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                pass
            elif opcode == OP_CLOSE:
                raise CDPError("WebSocket closed by browser")
            else:
                parts.append(payload)
                if fin:
                    return b''.join(parts).decode('utf-8')

    def close(self):
        try:
            self.send_frame(OP_CLOSE, b'')
        except Exception:
            pass
        self.sock.close()

def apply_mask(data, mask):
    ## XOR the payload with the repeated 4 byte mask, as one big int.
    count = len(data)
    if not count:
        return data
    repeated = (mask * (count // 4 + 1))[:count]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(repeated, 'big')).to_bytes(count, 'big')

class CDPConnection(object):
    '''
    One WebSocket connection to a browser.  Commands for a page are
    sent with the sessionId from Target.attachToTarget(flatten=True).
    Events received while waiting for a reply are queued for
    next_event().
    '''
    def __init__(self, ws_url, timeout=10):
        self.ws = WebSocket(ws_url, timeout)
        self.last_id = 0
        self.events = collections.deque()

    def call(self, method, params=None, session_id=None, timeout=30):
        self.last_id += 1
        message = {'id':self.last_id, 'method':method, 'params':params or {}}
        if session_id:
            message['sessionId'] = session_id
        self.ws.send(json.dumps(message))
        deadline = time.time() + timeout
        while True:
            reply = self._recv(deadline)
            if reply is None:
                raise CDPError("Timed out waiting for %s" % method)
            if reply.get('id') == message['id']:
                if 'error' in reply:
                    raise CDPCommandError("%s failed: %s" % (method, reply['error'].get('message')))
                return reply.get('result', {})
            if 'method' in reply:
                self.events.append(reply)

    def next_event(self, deadline):
        '''Return the next queued or incoming event, or None at deadline.'''
        if self.events:
            return self.events.popleft()
        while True:
            message = self._recv(deadline)
            if message is None:
                return None
            ## replies to calls that already timed out are dropped.
            if 'method' in message:
                return message

    def _recv(self, deadline):
        text = self.ws.recv(max(0, deadline - time.time()))
        return None if text is None else json.loads(text)

    def close(self):
        self.ws.close()
