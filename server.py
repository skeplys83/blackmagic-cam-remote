#!/usr/bin/env python3
"""Pixel Cam Remote: a local control panel for the Blackmagic Camera app on an Android phone.

http://localhost:8080 serves:
  /             the control page (index.html)
  /api/*        proxied to the phone's REST API (https://<phone>:4444/control/api/v1/*)
  /mirror       live screen + mic stream (scrcpy-server over adb, framed for the page)
  /viewport     where the camera preview sits on the phone screen (from a UI dump)
  /orientation  the phone's current screen rotation
  /clips        recordings in /sdcard/DCIM/Blackmagic Camera (via adb)
  /clips/file   one recording, with HTTP Range support (?download=1 to save)
  /clips/thumb  JPEG thumbnail (ffmpeg, cached)
  /events       live camera state (Server-Sent Events relayed from the phone's event websocket)
  /phone        phone health via adb: temperature, thermal state, Wi-Fi, storage, battery, DND, screen
  /screen       POST {"on": bool}: phone display panel on/off (camera keeps running)
  /automation   GET/PUT {"screenOffWhileRecording", "dndWhileRecording"}

  /setup        readiness checks (computer, phone connection, Blackmagic Camera) + pair/connect/launch

Needs adb, scrcpy-server (from scrcpy) and optionally ffmpeg (thumbnails). The Docker image bundles all
of them; see README.md.

Usage: python3 server.py [--phone IP] [--host 127.0.0.1] [--port 8080]
Environment: PCR_PHONE, PCR_HOST, PCR_PORT, PCR_DATA (settings + cache dir),
             SCRCPY_SERVER (path to scrcpy-server), SCRCPY_VERSION (its version)
"""
import argparse
import base64
import glob
import hashlib
import http.server
import json
import os
import pathlib
import platform
import queue
import random
import re
import secrets
import string
import shlex
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get("PCR_DATA") or HERE)
CACHE = DATA / ".cache"
SETTINGS_FILE = DATA / "settings.json"
CLIPS_DIR = "/sdcard/DCIM/Blackmagic Camera"
APP_PACKAGE = "com.blackmagicdesign.android.blackmagiccam"
API_PORT = 4444
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------- adb ----------

_serial_lock = threading.Lock()
_serial = None


def adb_serial():
    """Serial of the connected phone; reconnects over wireless debugging if needed."""
    global _serial
    with _serial_lock:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
        devices = [l.split("\t")[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]
        if _serial in devices:
            return _serial
        if not devices:
            mdns = subprocess.run(["adb", "mdns", "services"], capture_output=True, text=True).stdout
            for line in mdns.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "_adb-tls-connect._tcp":
                    subprocess.run(["adb", "connect", parts[2]], capture_output=True, timeout=10)
            out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
            devices = [l.split("\t")[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]
        if not devices and automation.settings.get("lastAddress"):
            subprocess.run(["adb", "connect", automation.settings["lastAddress"]], capture_output=True, timeout=10)
            out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
            devices = [l.split("\t")[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]
        if not devices:
            raise RuntimeError("phone not connected over adb (see setup)")
        _serial = devices[0]
        return _serial


_phone_ip = {"serial": None, "ip": None}


def phone_host():
    """The phone's Wi-Fi IP (PCR_PHONE / --phone overrides; otherwise asked over adb)."""
    if Handler.phone:
        return Handler.phone
    serial = adb_serial()
    if _phone_ip["serial"] != serial or not _phone_ip["ip"]:
        out = adb("shell", "ip -4 -o addr show wlan0", timeout=10).stdout.decode(errors="replace")
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if not m:
            raise RuntimeError("phone has no Wi-Fi address")
        _phone_ip.update(serial=serial, ip=m.group(1))
    return _phone_ip["ip"]


def adb(*args, **kw):
    return subprocess.run(["adb", "-s", adb_serial(), *args], capture_output=True, **kw)


def scrcpy_server():
    """Path and version of scrcpy-server (env SCRCPY_SERVER/SCRCPY_VERSION, else a Homebrew/Linux install)."""
    if os.environ.get("SCRCPY_SERVER") and os.path.exists(os.environ["SCRCPY_SERVER"]):
        return os.environ["SCRCPY_SERVER"], os.environ.get("SCRCPY_VERSION", "")
    for pattern in ("/opt/homebrew/Cellar/scrcpy/*/share/scrcpy/scrcpy-server",
                    "/usr/local/Cellar/scrcpy/*/share/scrcpy/scrcpy-server"):
        jars = sorted(glob.glob(pattern))
        if jars:
            return jars[-1], jars[-1].split("/scrcpy/")[1].split("/")[0]
    for path in ("/usr/local/share/scrcpy/scrcpy-server", "/usr/share/scrcpy/scrcpy-server"):
        if os.path.exists(path):
            out = subprocess.run(["scrcpy", "--version"], capture_output=True, text=True).stdout
            m = re.search(r"scrcpy (\S+)", out)
            return path, m.group(1) if m else ""
    raise RuntimeError("scrcpy-server not found (install scrcpy or use the Docker image)")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- mirror (scrcpy-server) ----------

# Frames sent to the page: type u8, pts u64 (µs), size u32, payload.
T_SESSION, T_CONFIG, T_KEY, T_DELTA, T_AUDIO, T_REPLACED = 0, 1, 2, 3, 4, 5
F_SESSION, F_CONFIG, F_KEY = 1 << 63, 1 << 62, 1 << 61
PTS_MASK = (1 << 61) - 1
# Live view quality presets: name -> (longest side in px, video bit rate). All capped at 24 fps.
PRESETS = {"native": (2424, 12_000_000), "medium": (1600, 6_000_000), "low": (1024, 3_000_000), "ultralow": (640, 1_000_000)}
MAX_FPS = 24


def _rx(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return bytes(buf)


class Mirror:
    """One scrcpy-server session (video + mic) relayed to one HTTP response."""

    def __init__(self, max_size, bit_rate, audio_source):
        self.max_size, self.bit_rate, self.audio_source = max_size, bit_rate, audio_source
        self.scid = "%08x" % random.randrange(1 << 31)
        self.port = free_port()
        self.proc = None
        self.socks = []
        self.replaced = False  # set when another viewer takes over the stream

    def start(self):
        jar, version = scrcpy_server()
        adb("push", jar, "/data/local/tmp/scrcpy-server.jar", check=True)
        adb("forward", f"tcp:{self.port}", f"localabstract:scrcpy_{self.scid}", check=True)
        self.proc = subprocess.Popen(
            ["adb", "-s", adb_serial(), "shell", "CLASSPATH=/data/local/tmp/scrcpy-server.jar",
             "app_process", "/", "com.genymobile.scrcpy.Server", version, f"scid={self.scid}",
             "log_level=warn", "tunnel_forward=true", "control=false", "stay_awake=true",
             "video_codec=h264", f"max_size={self.max_size}", f"video_bit_rate={self.bit_rate}", f"max_fps={MAX_FPS}",
             "audio=true", "audio_codec=raw", f"audio_source={self.audio_source}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        video = self._connect(first=True)
        audio = self._connect(first=False)
        _rx(video, 64)  # device name
        if _rx(video, 4) != b"h264":
            raise RuntimeError("unexpected video codec")
        self.audio_ok = _rx(audio, 4) == b"\0raw"
        return video, audio

    def _connect(self, first):
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                if not first:
                    self.socks.append(s)
                    return s
                if s.recv(1):  # dummy byte: server is up
                    self.socks.append(s)
                    return s
                s.close()
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError("scrcpy-server did not start")

    def stop(self):
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        if self.proc:
            self.proc.terminate()
        try:
            adb("forward", "--remove", f"tcp:{self.port}")
        except RuntimeError:
            pass

    def relay(self, write):
        video, audio = self.start()
        lock = threading.Lock()
        done = threading.Event()

        def send(t, pts, payload):
            with lock:
                write(struct.pack(">BQI", t, pts, len(payload)) + payload)

        def video_loop():
            try:
                while not done.is_set():
                    hdr = _rx(video, 12)
                    head, size = struct.unpack(">QI", hdr)
                    if head & F_SESSION:
                        send(T_SESSION, 0, hdr[4:12])  # width, height
                        continue
                    payload = _rx(video, size)
                    t = T_CONFIG if head & F_CONFIG else T_KEY if head & F_KEY else T_DELTA
                    send(t, head & PTS_MASK, payload)
            except Exception:
                done.set()

        def audio_loop():
            try:
                while not done.is_set():
                    head, size = struct.unpack(">QI", _rx(audio, 12))
                    payload = _rx(audio, size)
                    if not head & F_CONFIG:
                        send(T_AUDIO, head & PTS_MASK, payload)
            except Exception:
                done.set()

        threads = [threading.Thread(target=video_loop, daemon=True)]
        if self.audio_ok:
            threads.append(threading.Thread(target=audio_loop, daemon=True))
        for th in threads:
            th.start()
        done.wait()
        self.stop()


_mirror_lock = threading.Lock()
_mirror_current = None


def kill_stale_mirrors():
    """Stop live-view scrcpy-server sessions left over from earlier streams (not the screen-power one)."""
    adb("shell", "pkill -f 'com.genymobile.scrcpy.Server .*video_codec=h264'", timeout=10)
    keep = screen.forward_port()
    out = adb("forward", "--list", timeout=10).stdout.decode(errors="replace")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].startswith("localabstract:scrcpy_") and parts[1] != keep:
            adb("forward", "--remove", parts[1], timeout=10)


# ---------- screen power (scrcpy-server control channel) ----------

class ScreenPower:
    """Turns the phone's display panel off/on while apps keep running (scrcpy SET_DISPLAY_POWER).

    Uses its own control-only scrcpy-server session; if that session ends for any reason the
    server's cleanup turns the display back on. While the screen is off, an edge swipe on the
    phone (the back gesture) turns it back on.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.sock = self.proc = self.port = None
        self.off = False
        self.watcher = None
        self.watch_proc = None

    def forward_port(self):
        return f"tcp:{self.port}" if self.port and self.sock else None

    def _start(self):
        jar, version = scrcpy_server()
        scid = "%08x" % random.randrange(1 << 31)
        self.port = free_port()
        adb("push", jar, "/data/local/tmp/scrcpy-server.jar", check=True)
        adb("forward", f"tcp:{self.port}", f"localabstract:scrcpy_{scid}", check=True)
        self.proc = subprocess.Popen(
            ["adb", "-s", adb_serial(), "shell", "CLASSPATH=/data/local/tmp/scrcpy-server.jar",
             "app_process", "/", "com.genymobile.scrcpy.Server", version, f"scid={scid}", "log_level=warn",
             "tunnel_forward=true", "video=false", "audio=false", "control=true", "send_device_meta=false"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                if s.recv(1):  # dummy byte: server is up
                    self.sock = s
                    threading.Thread(target=self._drain, args=(s,), daemon=True).start()
                    return
                s.close()
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError("scrcpy-server (screen control) did not start")

    def _drain(self, s):
        """Read (and ignore) device messages; notice when the session ends."""
        try:
            while s.recv(4096):
                pass
        except OSError:
            pass
        with self.lock:
            if self.sock is s:
                self.sock = None
                self.off = False  # server cleanup restores the display

    def set(self, on):
        with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.sock:
                        self._start()
                    self.sock.sendall(bytes([10, 1 if on else 0]))  # TYPE_SET_DISPLAY_POWER
                    break
                except OSError:
                    self.sock = None
                    if attempt == 2:
                        raise
            self.off = not on
        if not on:
            self._watch_back_gesture()
        elif self.watch_proc:
            self.watch_proc.kill()  # stop the back-gesture watcher (here and on the phone)
            adb("shell", "pkill -f 'getevent -l /dev/input'", timeout=10)

    def _watch_back_gesture(self):
        if self.watcher and self.watcher.is_alive():
            return
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()

    def _watch(self):
        """While the screen is off, turn it on when a swipe starts at a screen edge and moves inward."""
        info = adb("shell", "getevent -p", timeout=10).stdout.decode(errors="replace")
        dev, maxes = None, {}
        for block in info.split("add device")[1:]:
            m = re.search(r"\d+: (/dev/input/event\d+)", block)
            x = re.search(r"0035\s+: value \d+, min \d+, max (\d+)", block)
            y = re.search(r"0036\s+: value \d+, min \d+, max (\d+)", block)
            if m and x and y:
                dev, maxes = m.group(1), {"X": int(x.group(1)), "Y": int(y.group(1))}
                break
        if not dev:
            return
        p = subprocess.Popen(["adb", "-s", adb_serial(), "shell", "getevent", "-l", dev],
                             stdout=subprocess.PIPE, text=True)
        self.watch_proc = p
        start, cur = {}, {}
        try:
            for line in p.stdout:
                if not self.off:
                    break
                parts = line.split()
                if len(parts) < 3 or parts[0] != "EV_ABS":
                    if parts[:2] == ["EV_KEY", "BTN_TOUCH"] and parts[2] == "UP":
                        start, cur = {}, {}
                    continue
                axis = {"ABS_MT_POSITION_X": "X", "ABS_MT_POSITION_Y": "Y"}.get(parts[1])
                if not axis:
                    continue
                v = int(parts[2], 16)
                start.setdefault(axis, v)
                cur[axis] = v
                if len(start) < 2:
                    continue
                for a in ("X", "Y"):
                    mx, s0, d = maxes[a], start[a], cur[a] - start[a]
                    if (s0 < 0.05 * mx and d > 0.08 * mx) or (s0 > 0.95 * mx and -d > 0.08 * mx):
                        threading.Thread(target=self.set, args=(True,), daemon=True).start()
                        return
        finally:
            p.kill()


screen = ScreenPower()


# ---------- phone status (adb) ----------

THERMAL = ["none", "light", "moderate", "severe", "critical", "emergency", "shutdown"]


def phone_status():
    cmd = ("dumpsys battery; echo @@; dumpsys thermalservice | grep -E 'Thermal Status|mName=VIRTUAL-SKIN,'; echo @@; "
           "cmd wifi status | grep -m1 WifiInfo; echo @@; df -k /storage/emulated | tail -1; echo @@; settings get global zen_mode")
    out = adb("shell", cmd, timeout=15).stdout.decode(errors="replace").split("@@")
    bat, therm, wifi, df, zen = (out + [""] * 5)[:5]
    g = lambda pat, txt: (re.search(pat, txt) or [None, None])[1]
    temp = g(r"temperature: (\d+)", bat)
    skin = g(r"mValue=([\d.]+), mType=\S+, mName=VIRTUAL-SKIN,", therm)
    status = g(r"Thermal Status: (\d)", therm)
    rssi = g(r"RSSI: (-?\d+)", wifi)
    dfp = df.split()
    return {
        "battery": {"level": int(g(r"level: (\d+)", bat) or 0),
                    "charging": bool(re.search(r"(AC|USB|Wireless) powered: true", bat)),
                    "temperature": int(temp) / 10 if temp else None},
        "skinTemperature": round(float(skin), 1) if skin else None,
        "thermal": THERMAL[int(status)] if status else None,
        "wifi": {"ssid": g(r'SSID: "([^"]*)"', wifi), "rssi": int(rssi) if rssi else None,
                 "linkMbps": int(g(r"Link speed: (\d+)Mbps", wifi) or 0) or None,
                 "frequencyMHz": int(g(r"Frequency: (\d+)MHz", wifi) or 0) or None},
        "storage": {"freeBytes": int(dfp[3]) * 1024, "totalBytes": int(dfp[1]) * 1024} if len(dfp) >= 4 and dfp[1].isdigit() else None,
        "dnd": (zen.strip() or "0") != "0",
        "screenOff": screen.off,
    }


# ---------- automation (record start/stop) ----------

class Automation:
    def __init__(self):
        self.lock = threading.Lock()
        self.settings = {"screenOffWhileRecording": False, "dndWhileRecording": False, "lastAddress": ""}
        try:
            self.settings.update(json.loads(SETTINGS_FILE.read_text()))
        except (OSError, ValueError):
            pass
        self.recording = None
        self.dnd_set_by_us = False

    def update(self, changes):
        with self.lock:
            for k in self.settings:
                if k in changes:
                    self.settings[k] = str(changes[k]) if k == "lastAddress" else bool(changes[k])
            DATA.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
            return dict(self.settings)

    def on_record(self, recording):
        if recording == self.recording:
            return
        first = self.recording is None
        self.recording = recording
        if first and not recording:
            return
        threading.Thread(target=self._apply, args=(recording,), daemon=True).start()

    def _apply(self, recording):
        with self.lock:
            s = dict(self.settings)
        try:
            if recording:
                if s["dndWhileRecording"]:
                    zen = adb("shell", "settings get global zen_mode", timeout=10).stdout.decode().strip()
                    if zen in ("", "0"):
                        adb("shell", "cmd notification set_dnd priority", timeout=10)
                        self.dnd_set_by_us = True
                if s["screenOffWhileRecording"]:
                    screen.set(False)
            else:
                if self.dnd_set_by_us:
                    adb("shell", "cmd notification set_dnd off", timeout=10)
                    self.dnd_set_by_us = False
                if screen.off:
                    screen.set(True)
        except Exception as e:
            print("automation:", e)


automation = Automation()


# ---------- live events (phone websocket -> Server-Sent Events) ----------

class EventHub:
    """Keeps one websocket to the phone subscribed to every property; caches and fans out changes."""

    def __init__(self):
        self.cache = {}
        self.clients = []
        self.lock = threading.Lock()
        self.connected = False
        threading.Thread(target=self._run, daemon=True).start()

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
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
            time.sleep(3)

    def _session(self):
        host = phone_host()
        with urllib.request.urlopen(f"https://{host}:{API_PORT}/control/api/v1/event/list", context=SSL_CTX, timeout=5) as r:
            props = json.load(r)
        s = SSL_CTX.wrap_socket(socket.create_connection((host, API_PORT), timeout=10), server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET /control/api/v1/event/websocket HTTP/1.1\r\nHost: {host}:{API_PORT}\r\nUpgrade: websocket\r\n"
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
                b0, b1 = _rx(s, 2)
            except (TimeoutError, socket.timeout):
                send(9, b"")  # ping; a dead link fails the next read/write
                continue
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", _rx(s, 2))[0]
            elif n == 127:
                n = struct.unpack(">Q", _rx(s, 8))[0]
            payload = _rx(s, n)
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
                        self._publish(prop, value)
                elif data.get("action") == "propertyValueChanged":
                    self._publish(data.get("property"), data.get("value"))


hub = None


# ---------- setup checks ----------

def _check(ok, title, detail="", fix=None, optional=False):
    return {"ok": bool(ok), "title": title, "detail": detail, "fix": fix, "optional": optional}


def setup_status():
    """Three groups of checks; the dashboard only starts when every required one passes."""
    computer = [_check(True, "Python", f"{platform.python_version()} on {platform.system()} {platform.machine()}")]
    adb_ok = bool(shutil.which("adb"))
    computer.append(_check(adb_ok, "adb (Android platform tools)", "" if adb_ok else "not found",
                           None if adb_ok else "Install Android platform tools, or use the Docker image."))
    try:
        path, version = scrcpy_server()
        computer.append(_check(True, "scrcpy-server", f"{version} ({path})"))
    except RuntimeError as e:
        computer.append(_check(False, "scrcpy-server", str(e), "Install scrcpy, or use the Docker image."))
    ff = bool(shutil.which("ffmpeg"))
    computer.append(_check(ff, "ffmpeg", "for recording thumbnails" if ff else "not found: recordings show without thumbnails",
                           None if ff else "Install ffmpeg for thumbnails.", optional=True))

    phone, app = [], []
    devices = []
    if adb_ok:
        out = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                devices.append({"serial": parts[0], "state": parts[1],
                                "model": (re.search(r"model:(\S+)", line) or [None, ""])[1].replace("_", " ")})
    ready_devs = [d for d in devices if d["state"] == "device"]
    if ready_devs:
        d = ready_devs[0]
        wireless = ":" in d["serial"] or "_adb-tls-connect" in d["serial"]
        phone.append(_check(True, "Connected over wireless debugging" if wireless else "Connected (over USB)",
                            f"{d['model'] or d['serial']} · {d['serial']}" + ("" if wireless else " · wireless debugging is recommended")))
    elif any(d["state"] == "unauthorized" for d in devices):
        phone.append(_check(False, "Connected over wireless debugging", "the phone hasn't allowed this computer yet",
                            "Unlock the phone and confirm the debugging prompt, or pair again below."))
    else:
        phone.append(_check(False, "Connected over wireless debugging", "no phone connected",
                            "Turn on Wireless debugging on the phone and pair it below (QR code or pairing code)."))

    if ready_devs:
        try:
            sdk = int(adb("shell", "getprop ro.build.version.sdk", timeout=10).stdout.decode().strip() or 0)
            phone.append(_check(sdk >= 33, "Android 13 or newer", f"Android API {sdk}",
                                None if sdk >= 33 else "Blackmagic Camera needs Android 13 or newer."))
        except Exception as e:
            phone.append(_check(False, "Android version", str(e)))

        pkg = adb("shell", f"dumpsys package {APP_PACKAGE} | grep -m1 versionName", timeout=10).stdout.decode().strip()
        installed = "versionName=" in pkg
        app.append(_check(installed, "Blackmagic Camera installed", pkg.split("=", 1)[-1] if installed else "not installed",
                          None if installed else "Install Blackmagic Camera from Google Play."))
        if installed:
            running = bool(adb("shell", f"pidof {APP_PACKAGE}", timeout=10).stdout.strip())
            app.append(_check(running, "Blackmagic Camera open", "" if running else "not running",
                              None if running else "Open Blackmagic Camera on the phone (or use the button below).",))
            uid = (re.search(r"uid:(\d+)", adb("shell", f"pm list packages -U {APP_PACKAGE}", timeout=10).stdout.decode()) or [None, None])[1]
            owner = None
            tcp = adb("shell", "cat /proc/net/tcp /proc/net/tcp6", timeout=10).stdout.decode(errors="replace")
            for line in tcp.splitlines()[1:]:
                f = line.split()
                if len(f) > 7 and f[3] == "0A" and int(f[1].split(":")[1], 16) == API_PORT:
                    owner = f[7]
            if owner is None:
                app.append(_check(False, "HTTP server on", f"nothing is listening on port {API_PORT}",
                                  "In Blackmagic Camera: Settings → Network Access → turn on Enable HTTP Server."))
            elif owner != uid:
                name = adb("shell", f"cmd package list packages -U | grep 'uid:{owner}$'", timeout=10).stdout.decode().strip()
                name = name.split()[0].replace("package:", "") if name else f"uid {owner}"
                app.append(_check(False, "HTTP server on", f"port {API_PORT} is taken by another app: {name}",
                                  f"Close or uninstall {name}, then turn Enable HTTP Server off and on in Blackmagic Camera."))
            else:
                app.append(_check(True, "HTTP server on", f"port {API_PORT}"))
                try:
                    host = phone_host()
                    with urllib.request.urlopen(f"https://{host}:{API_PORT}/control/api/v1/system", context=SSL_CTX, timeout=4):
                        pass
                    app.append(_check(True, "Reachable over the network", f"https://{host}:{API_PORT}"))
                except Exception as e:
                    app.append(_check(False, "Reachable over the network", f"can't reach the phone's API ({e})",
                                      "Put the phone and this computer on the same Wi-Fi network; guest networks often block devices from talking to each other."))
    else:
        app.append(_check(False, "Blackmagic Camera", "connect the phone first"))

    groups = {"computer": computer, "phone": phone, "app": app}
    ready = all(c["ok"] or c["optional"] for g in groups.values() for c in g)
    return {"ready": ready, "groups": groups, "devices": devices}


# QR pairing: the phone scans WIFI:T:ADB;S:<name>;P:<password>;; then advertises _adb-tls-pairing._tcp
# under <name> via mDNS; we find it, run `adb pair`, then connect to its _adb-tls-connect service.
_qr = {"name": None, "password": None, "state": "idle", "message": "", "started": 0}
_qr_lock = threading.Lock()


def mdns_services():
    out = subprocess.run(["adb", "mdns", "services"], capture_output=True, text=True, timeout=10).stdout
    return [line.split() for line in out.splitlines() if "._tcp" in line and len(line.split()) >= 3]


def qr_start():
    if not shutil.which("qrencode"):
        raise RuntimeError("qrencode is not installed; pair with a code instead")
    name = "pcr-" + secrets.token_hex(3)
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
    payload = f"WIFI:T:ADB;S:{name};P:{password};;"
    svg = subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-o", "-", payload],
                         capture_output=True, text=True, timeout=10).stdout
    with _qr_lock:
        _qr.update(name=name, password=password, state="waiting", message="Scan the code with the phone", started=time.time())
    threading.Thread(target=_qr_watch, args=(name, password), daemon=True).start()
    return {"svg": svg, "name": name}


def _qr_watch(name, password):
    def set_state(state, message):
        with _qr_lock:
            if _qr["name"] == name:
                _qr.update(state=state, message=message)
    seen_any = False
    deadline = time.time() + 180
    while time.time() < deadline:
        with _qr_lock:
            if _qr["name"] != name:
                return  # replaced by a newer code
        services = mdns_services()
        seen_any = seen_any or bool(services)
        pairing = [s for s in services if s[0] == name and s[1] == "_adb-tls-pairing._tcp"]
        if pairing:
            addr = pairing[0][2]
            set_state("pairing", f"Pairing with {addr}…")
            out = subprocess.run(["adb", "pair", addr, password], capture_output=True, text=True, timeout=30)
            msg = (out.stdout + out.stderr).strip()
            if "Successfully paired" not in msg:
                return set_state("failed", msg or "pairing failed")
            ip = addr.rsplit(":", 1)[0]
            set_state("connecting", "Paired. Connecting…")
            for _ in range(20):  # the phone advertises its connect port right after pairing
                for s in mdns_services():
                    if s[1] == "_adb-tls-connect._tcp" and s[2].startswith(ip + ":"):
                        subprocess.run(["adb", "connect", s[2]], capture_output=True, timeout=15)
                        automation.update({"lastAddress": s[2]})
                        return set_state("done", f"Paired and connected to {s[2]}")
                time.sleep(1)
            return set_state("done", "Paired. Enter the IP address & port below to connect.")
        if not seen_any and time.time() - _qr["started"] > 25:
            set_state("waiting", "Nothing found on the network yet. If the phone already shows \"Pairing\", this setup can't see "
                                 "local-network discovery (Docker Desktop on macOS/Windows): pair with a code instead.")
        time.sleep(1)
    set_state("timeout", "The code expired. Show a new one to try again.")


def setup_action(action, body):
    if action == "qr":
        return qr_start()
    if action == "qr-status":
        with _qr_lock:
            return {"state": _qr["state"], "message": _qr["message"], "name": _qr["name"]}
    if action == "pair":
        addr, code = str(body.get("address", "")).strip(), str(body.get("code", "")).strip()
        if not re.fullmatch(r"[\w.\-]+:\d+", addr) or not re.fullmatch(r"\d{6}", code):
            raise ValueError("enter the pairing address as IP:port and the 6-digit code")
        out = subprocess.run(["adb", "pair", addr, code], capture_output=True, text=True, timeout=30)
        msg = (out.stdout + out.stderr).strip()
        if "Successfully paired" not in msg:
            raise RuntimeError(msg or "pairing failed")
        return {"message": msg}
    if action == "connect":
        addr = str(body.get("address", "")).strip()
        if not re.fullmatch(r"[\w.\-]+:\d+", addr):
            raise ValueError("enter the address as IP:port (Wireless debugging → IP address & Port)")
        out = subprocess.run(["adb", "connect", addr], capture_output=True, text=True, timeout=15)
        msg = (out.stdout + out.stderr).strip()
        if "connected to" not in msg:
            raise RuntimeError(msg or "connect failed")
        automation.update({"lastAddress": addr})
        return {"message": msg}
    if action == "launch":
        adb("shell", f"monkey -p {APP_PACKAGE} -c android.intent.category.LAUNCHER 1", timeout=15)
        return {"message": "started"}
    raise ValueError("unknown action")


# ---------- orientation ----------

def orientation():
    """The phone's current screen rotation (0/90/180/270), as Android reports it."""
    out = adb("shell", "dumpsys display | grep -m1 -o 'orientation=[0-9]'", timeout=10).stdout.decode(errors="replace")
    m = re.search(r"orientation=(\d)", out)
    return {"rotation": int(m.group(1)) * 90 if m else None}


# ---------- viewport ----------

def viewport():
    """Screen size and the camera preview's bounds, from a UI dump."""
    xml = adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=15).stdout.decode(errors="replace")
    boxes = [(cls, tuple(map(int, b)))
             for cls, *b in re.findall(r'class="([^"]+)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)]
    if not boxes:
        raise RuntimeError("UI dump failed")
    screen = max((b for _, b in boxes), key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    surfaces = [b for cls, b in boxes if cls == "android.view.SurfaceView"]
    view = max(surfaces, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if surfaces else screen
    return {"screen": [screen[2], screen[3]], "viewport": list(view)}


# ---------- recordings ----------

def safe_name(name):
    if not name or "/" in name or name.startswith(".") or "'" in name or "\n" in name:
        raise ValueError("bad clip name")
    return name


def clip_path(name):
    return f"{CLIPS_DIR}/{safe_name(name)}"


def list_clips():
    cmd = f"cd {shlex.quote(CLIPS_DIR)} 2>/dev/null && for f in *; do [ -f \"$f\" ] && stat -c '%s|%Y|%n' \"$f\"; done"
    out = adb("shell", cmd, timeout=15).stdout.decode(errors="replace")
    clips = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            clips.append({"name": parts[2], "size": int(parts[0]), "mtime": int(parts[1])})
    clips.sort(key=lambda c: c["mtime"], reverse=True)
    return clips


def clip_size(name):
    out = adb("shell", f"stat -c %s '{clip_path(name)}'", timeout=10).stdout.decode().strip()
    if not out.isdigit():
        raise FileNotFoundError(name)
    return int(out)


def read_range(name, start, length, write):
    """Stream bytes [start, start+length) of a clip from the phone."""
    cmd = f"tail -c +{start + 1} '{clip_path(name)}' | head -c {length}"
    p = subprocess.Popen(["adb", "-s", adb_serial(), "exec-out", cmd], stdout=subprocess.PIPE)
    try:
        while True:
            chunk = p.stdout.read(256 * 1024)
            if not chunk:
                break
            write(chunk)
    finally:
        p.kill()
        p.wait()


_thumb_lock = threading.Lock()


def thumbnail(name, size, mtime, port):
    CACHE.mkdir(exist_ok=True)
    key = hashlib.sha1(f"{name}|{size}|{mtime}".encode()).hexdigest()
    out = CACHE / f"{key}.jpg"
    if not out.exists():
        with _thumb_lock:  # one ffmpeg at a time; each one reads over adb
            if not out.exists():
                url = f"http://127.0.0.1:{port}/clips/file?name={urllib.parse.quote(name)}"
                tmp = out.with_suffix(".tmp.jpg")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", url, "-frames:v", "1",
                                "-vf", "scale=480:-2", "-q:v", "4", str(tmp)], timeout=120)
                if not tmp.exists():
                    raise RuntimeError("thumbnail failed")
                tmp.rename(out)
    return out.read_bytes()


# ---------- HTTP ----------

class Handler(http.server.BaseHTTPRequestHandler):
    phone = ""
    port = 8080
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/api/", "/clips/thumb", "/clips/file")):
            super().log_message(fmt, *args)

    def _send(self, code, body=b"", ctype="application/json", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _error(self, e, code=502):
        self._json({"error": str(e)}, code)

    def _proxy(self):
        try:
            host = phone_host()
        except Exception as e:
            return self._error(e)
        url = f"https://{host}:{API_PORT}/control/api/v1/{self.path[len('/api/'):]}"
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=data, method=self.command)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=5) as r:
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._error(f"phone unreachable: {e}")

    def _mirror(self, q):
        max_size, bit_rate = PRESETS.get(q.get("quality", ["medium"])[0], PRESETS["medium"])
        source = q.get("audio", ["mic"])[0]
        if source not in ("mic", "mic-camcorder", "mic-unprocessed", "output", "playback"):
            source = "mic"
        global _mirror_current
        m = Mirror(max_size, bit_rate, source)
        with _mirror_lock:  # one stream at a time: a new viewer replaces the old one
            if _mirror_current:
                _mirror_current.replaced = True
                _mirror_current.stop()
            kill_stale_mirrors()
            _mirror_current = m
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(b):
            self.wfile.write(b)
            self.wfile.flush()

        try:
            m.relay(write)
        except Exception as e:
            self.log_message("mirror ended: %s", e)
        finally:
            m.stop()
        if m.replaced:  # tell this page not to reconnect and fight the new viewer
            try:
                write(struct.pack(">BQI", T_REPLACED, 0, 0))
            except OSError:
                pass

    def _clip_file(self, q):
        name = q["name"][0]
        size = clip_size(name)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if m else 200)
        ctype = "video/quicktime" if name.lower().endswith(".mov") else "video/mp4"
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if "download" in q:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.end_headers()
        if self.command != "HEAD":
            try:
                read_range(name, start, length, self.wfile.write)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path.startswith("/api/"):
                return self._proxy()
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/mirror":
                return self._mirror(q)
            if u.path == "/setup":
                return self._json(setup_status())
            if u.path == "/events":
                return self._events()
            if u.path == "/phone":
                return self._json(phone_status())
            if u.path == "/automation":
                return self._json(automation.settings)
            if u.path == "/orientation":
                return self._json(orientation())
            if u.path == "/viewport":
                return self._json(viewport())
            if u.path == "/clips":
                return self._json({"clips": list_clips()})
            if u.path == "/clips/file":
                return self._clip_file(q)
            if u.path == "/clips/thumb":
                body = thumbnail(q["name"][0], q.get("size", [""])[0], q.get("mtime", [""])[0], self.port)
                return self._send(200, body, "image/jpeg", [("Cache-Control", "max-age=86400")])
        except (KeyError, ValueError) as e:
            return self._error(e, 400)
        except FileNotFoundError as e:
            return self._error(f"not found: {e}", 404)
        except Exception as e:
            return self._error(e)
        self._send(404, b"not found", "text/plain")

    def _events(self):
        q, snapshot = hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            snapshot["@connected"] = hub.connected
            self.wfile.write(f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    prop, value = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps({'p': prop, 'v': value})}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            hub.unsubscribe(q)

    def _local_write(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if u.path == "/screen" and self.command == "POST":
                screen.set(bool(body.get("on")))
                return self._json({"screenOff": screen.off})
            if u.path == "/automation" and self.command in ("PUT", "POST"):
                return self._json(automation.update({k: v for k, v in body.items() if k != "lastAddress"}))
            if u.path.startswith("/setup/") and self.command == "POST":
                return self._json(setup_action(u.path[len("/setup/"):], body))
        except ValueError as e:
            return self._error(e, 400)
        except Exception as e:
            return self._error(e, 502)
        self._send(404, b"not found", "text/plain")

    def _write(self):
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._local_write()

    do_HEAD = do_GET
    do_PUT = do_POST = do_DELETE = _write


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default=os.environ.get("PCR_PHONE", ""), help="phone IP (default: ask the phone over adb)")
    ap.add_argument("--host", default=os.environ.get("PCR_HOST", "127.0.0.1"), help="address to listen on")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PCR_PORT", "8080")))
    args = ap.parse_args()
    Handler.phone, Handler.port = args.phone, args.port
    global hub
    try:  # leftovers from a previous run (the display comes back on when they exit)
        adb("shell", "pkill -f com.genymobile.scrcpy.Server", timeout=10)
    except Exception:
        pass
    hub = EventHub()
    srv = Server((args.host, args.port), Handler)
    print(f"Pixel cam remote listening on http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
