"""WebSocket framing and CDP messaging, against a local fake browser endpoint."""
import base64
import hashlib
import json
import re
import socket
import struct
import threading
import unittest

from fanficfare.fetchers.cdp import (CDPConnection, CDPCommandError, CDPError,
                                     WebSocket, WS_GUID, apply_mask)


def server_frame(opcode, payload, fin=True):
    """Unmasked frame, as a server sends it."""
    count = len(payload)
    first = (0x80 if fin else 0) | opcode
    if count < 126:
        header = struct.pack('>BB', first, count)
    elif count < (1 << 16):
        header = struct.pack('>BBH', first, 126, count)
    else:
        header = struct.pack('>BBQ', first, 127, count)
    return header + payload


def recv_exact(conn, count):
    data = b''
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise EOFError()
        data += chunk
    return data


def read_client_frame(conn):
    """Returns (masked, opcode, payload) of one frame from the client."""
    (byte1, byte2) = recv_exact(conn, 2)
    count = byte2 & 0x7F
    if count == 126:
        count = struct.unpack('>H', recv_exact(conn, 2))[0]
    elif count == 127:
        count = struct.unpack('>Q', recv_exact(conn, 8))[0]
    masked = bool(byte2 & 0x80)
    mask = recv_exact(conn, 4) if masked else None
    payload = recv_exact(conn, count)
    if masked:
        payload = apply_mask(payload, mask)
    return (masked, byte1 & 0x0F, payload)


class FakeBrowserEndpoint(object):
    """Accepts one WebSocket connection and runs script(conn) on it."""
    def __init__(self, script, status=b'101 Switching Protocols'):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.url = 'ws://127.0.0.1:%s/devtools/browser/test' % self.server.getsockname()[1]
        self.error = None
        self.thread = threading.Thread(target=self.run, args=(script, status))
        self.thread.daemon = True
        self.thread.start()

    def run(self, script, status):
        (conn, addr) = self.server.accept()
        try:
            request = b''
            while b'\r\n\r\n' not in request:
                request += conn.recv(4096)
            key = re.search(rb'Sec-WebSocket-Key: (\S+)', request).group(1)
            accept = base64.b64encode(hashlib.sha1(key + WS_GUID).digest())
            conn.sendall(b'HTTP/1.1 ' + status + b'\r\nUpgrade: websocket\r\n'
                         b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            script(conn)
        except Exception as e:
            self.error = e
        finally:
            conn.close()
            self.server.close()

    def join(self):
        self.thread.join(5)
        if self.error:
            raise self.error


class ApplyMaskTest(unittest.TestCase):
    def test_rfc6455_example(self):
        self.assertEqual(apply_mask(b'Hello', b'\x37\xfa\x21\x3d'), b'\x7f\x9f\x4d\x51\x58')

    def test_empty(self):
        self.assertEqual(apply_mask(b'', b'\x01\x02\x03\x04'), b'')


class WebSocketTest(unittest.TestCase):
    def test_text_round_trip_is_masked(self):
        seen = []
        def script(conn):
            seen.append(read_client_frame(conn))
            conn.sendall(server_frame(0x1, 'réponse'.encode('utf-8')))
        endpoint = FakeBrowserEndpoint(script)
        ws = WebSocket(endpoint.url)
        ws.send('hello')
        self.assertEqual(ws.recv(5), 'réponse')
        ws.close()
        endpoint.join()
        self.assertEqual(seen, [(True, 0x1, b'hello')])

    def test_fragmented_message_and_ping(self):
        seen = []
        def script(conn):
            conn.sendall(server_frame(0x9, b'are you there'))
            conn.sendall(server_frame(0x1, b'first ', fin=False))
            conn.sendall(server_frame(0x0, b'second'))
            seen.append(read_client_frame(conn))
        endpoint = FakeBrowserEndpoint(script)
        ws = WebSocket(endpoint.url)
        self.assertEqual(ws.recv(5), 'first second')
        endpoint.join()
        ws.close()
        self.assertEqual(seen, [(True, 0xA, b'are you there')])

    def test_extended_lengths(self):
        medium = 'm' * 300
        large = 'L' * 70000
        def script(conn):
            conn.sendall(server_frame(0x1, medium.encode('ascii')))
            conn.sendall(server_frame(0x1, large.encode('ascii')))
            read_client_frame(conn)
        endpoint = FakeBrowserEndpoint(script)
        ws = WebSocket(endpoint.url)
        self.assertEqual(ws.recv(5), medium)
        self.assertEqual(ws.recv(5), large)
        ws.send('x' * 70000)
        ws.close()
        endpoint.join()

    def test_recv_timeout_returns_none(self):
        done = threading.Event()
        endpoint = FakeBrowserEndpoint(lambda conn: done.wait(5))
        ws = WebSocket(endpoint.url)
        self.assertIsNone(ws.recv(0.2))
        done.set()
        ws.close()
        endpoint.join()

    def test_closed_by_browser(self):
        endpoint = FakeBrowserEndpoint(lambda conn: conn.sendall(server_frame(0x8, b'')))
        ws = WebSocket(endpoint.url)
        with self.assertRaises(CDPError):
            ws.recv(5)
        ws.close()
        endpoint.join()

    def test_handshake_refused(self):
        endpoint = FakeBrowserEndpoint(lambda conn: None, status=b'404 Not Found')
        with self.assertRaises(CDPError):
            WebSocket(endpoint.url)
        endpoint.join()


class CDPConnectionTest(unittest.TestCase):
    def test_reply_matched_and_events_queued(self):
        def script(conn):
            command = json.loads(read_client_frame(conn)[2])
            event = {'method':'Page.loadEventFired', 'params':{}, 'sessionId':'S1'}
            conn.sendall(server_frame(0x1, json.dumps(event).encode('utf-8')))
            reply = {'id':command['id'], 'result':{'frameId':'F1'}}
            conn.sendall(server_frame(0x1, json.dumps(reply).encode('utf-8')))
            command = json.loads(read_client_frame(conn)[2])
            reply = {'id':command['id'], 'error':{'code':-32000, 'message':'Cannot navigate'}}
            conn.sendall(server_frame(0x1, json.dumps(reply).encode('utf-8')))
        endpoint = FakeBrowserEndpoint(script)
        conn = CDPConnection(endpoint.url)
        self.assertEqual(conn.call('Page.navigate', {'url':'x'}, 'S1'), {'frameId':'F1'})
        self.assertEqual(conn.next_event(0)['method'], 'Page.loadEventFired')
        with self.assertRaises(CDPCommandError):
            conn.call('Page.navigate', {'url':'y'}, 'S1')
        conn.close()
        endpoint.join()

    def test_call_timeout(self):
        done = threading.Event()
        endpoint = FakeBrowserEndpoint(lambda conn: done.wait(5))
        conn = CDPConnection(endpoint.url)
        with self.assertRaises(CDPError) as raised:
            conn.call('Browser.getVersion', timeout=0.2)
        self.assertNotIsInstance(raised.exception, CDPCommandError)
        done.set()
        conn.close()
        endpoint.join()


if __name__ == '__main__':
    unittest.main()
