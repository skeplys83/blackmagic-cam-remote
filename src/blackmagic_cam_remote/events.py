"""Live camera state: one websocket to the app's event API, cached and fanned out to browsers.

The app doesn't push every change (e.g. switching lens on the phone updates `/lens/cameras/active` but not the
`isActive` flags in `/lens/cameras`), so a second thread re-reads all properties over REST in a loop and publishes
the ones that changed. That bounds how long the dashboard can show a stale value to one pass (~4 s).
"""
import base64
import http.client
import json
import os
import queue
import socket
import struct
import threading
import time
import urllib.request

from . import config
from .adb import phone_host
from .automation import automation
from .util import recv_exact


class EventHub:
    """Keeps one websocket to the phone subscribed to every property; caches and fans out changes."""

    def __init__(self):
        self.cache = {}
        self.clients = []
        self.lock = threading.Lock()
        self.connected = False
        self.retry = threading.Event()
        self.props = []        # every property the app offers (from /event/list)
        self.event_at = {}     # property -> time of its last pushed event
        self.rest_seen = {}    # property -> last value read over REST (event and REST shapes can differ)
        self.unreadable = set()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._reconcile, daemon=True).start()

    def wake(self):
        """Retry the websocket now (e.g. the app's API just came back)."""
        self.retry.set()

    def subscribe(self, q=None):
        q = q or queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.append(q)
            snapshot = dict(self.cache)
        return q, snapshot

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def _publish(self, prop, value):
        with self.lock:
            self.cache[prop] = value
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait((prop, value))
            except queue.Full:
                pass
        if prop == "/transports/0/record" and isinstance(value, dict):
            automation.on_record(bool(value.get("recording")))

    def _status(self, connected):
        self.connected = connected
        self._publish("@connected", connected)

    def _run(self):
        while True:
            try:
                self._session()
            except Exception as e:
                if self.connected:
                    print("events: websocket closed:", e)
            self._status(False)
            self.retry.wait(1)
            self.retry.clear()

    def _session(self):
        host = phone_host()
        with urllib.request.urlopen(f"https://{host}:{config.API_PORT}/control/api/v1/event/list", context=config.PHONE_SSL, timeout=5) as r:
            props = json.load(r)
        self.props = props
        s = config.PHONE_SSL.wrap_socket(socket.create_connection((host, config.API_PORT), timeout=10), server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET /control/api/v1/event/websocket HTTP/1.1\r\nHost: {host}:{config.API_PORT}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = s.recv(1)
            if not c:
                raise EOFError("handshake")
            head += c
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(head.split(b"\r\n")[0].decode(errors="replace"))

        def send(opcode, data):
            mask = os.urandom(4)
            n = len(data)
            hdr = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else
                  bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
            s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

        send(1, json.dumps({"type": "request", "data": {"action": "subscribe", "properties": props}, "id": 1}).encode())
        self._status(True)
        s.settimeout(20)
        buf = b""
        while True:
            try:
                b0, b1 = recv_exact(s, 2)
            except (TimeoutError, socket.timeout):
                send(9, b"")  # ping; a dead link fails the next read/write
                continue
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", recv_exact(s, 2))[0]
            elif n == 127:
                n = struct.unpack(">Q", recv_exact(s, 8))[0]
            payload = recv_exact(s, n)
            op = b0 & 0x0F
            if op == 8:
                raise EOFError("closed by phone")
            if op == 9:
                send(10, payload)
                continue
            if op in (1, 0):
                buf += payload
                if not b0 & 0x80:
                    continue
                msg, buf = json.loads(buf), b""
                data = msg.get("data") or {}
                if msg.get("type") == "response":
                    for prop, value in (data.get("values") or {}).items():
                        self.event_at[prop] = time.monotonic()
                        self._publish(prop, value)
                elif data.get("action") == "propertyValueChanged":
                    self.event_at[data.get("property")] = time.monotonic()
                    self._publish(data.get("property"), data.get("value"))

    PASS_PAUSE = 2.0  # seconds between REST passes (a pass over ~80 properties takes ~2 s)

    def _reconcile(self):
        """Re-read every property over REST and publish what changed since the last read.

        A value is published only when its REST reading changed (so differing event/REST shapes don't flip back and
        forth) and no event arrived while it was being read (so an older reading never overwrites a newer event).
        """
        conn = None
        while True:
            time.sleep(self.PASS_PAUSE)
            if not self.connected:
                conn = None
                self.rest_seen.clear()
                self.unreadable.clear()
                continue
            try:
                host = phone_host()
                if conn is None or conn.host != host:
                    conn = http.client.HTTPSConnection(host, config.API_PORT, context=config.PHONE_SSL, timeout=5)
                for prop in list(self.props):
                    if prop in self.unreadable or not self.connected:
                        continue
                    started = time.monotonic()
                    conn.request("GET", "/control/api/v1" + prop.replace("/monitoring/LCD/", "/monitoring/Device/"))
                    r = conn.getresponse()
                    body = r.read()
                    if r.status != 200:
                        if r.status in (400, 404, 501):
                            self.unreadable.add(prop)  # not implemented on this phone/app
                        continue
                    value = json.loads(body)
                    first = prop not in self.rest_seen
                    if not first and self.rest_seen[prop] == value:
                        continue
                    self.rest_seen[prop] = value
                    if self.event_at.get(prop, 0) >= started:
                        continue  # a pushed event is newer than this reading
                    if first and self.cache.get(prop) is not None and not self._differs(prop, value):
                        continue
                    self._publish(prop, value)
            except Exception:
                if conn:
                    conn.close()
                conn = None

    def _differs(self, prop, rest_value):
        """First REST reading vs. the cached (event) value: compare only the fields both have."""
        cached = self.cache.get(prop)
        if isinstance(cached, dict) and isinstance(rest_value, dict):
            return any(k in cached and cached[k] != v for k, v in rest_value.items())
        return cached != rest_value

hub = None
