"""Live readiness: is the phone connected, and is Blackmagic Camera in front? Pushed to the page as it changes.

Nothing here polls the phone quickly:
  - adb connection: adb's own device-tracking stream (host:track-devices-l) reports every change at once.
  - app in front:   a logcat stream of Android's "top resumed activity" events; each event triggers one exact
                    foreground query. A slow re-check (every few seconds) catches anything an event missed.
  - the app's HTTP API: probed directly over Wi-Fi (no adb): every 0.3 s while the app is in front but its
                    server isn't answering yet (it restarts ~2 s after the app comes back), otherwise every 2 s.
  - everything else (tools, app installed, port owner, network): setup_status() every 10 s and right after
    any change above.
The combined state is what /setup returns and /events pushes ("setup" events), and what decides setup screen vs. dashboard.
"""
import queue
import socket
import subprocess
import threading
import time
import urllib.request

from . import config, events
from .adb import phone_host
from .setup import _check, foreground_app, setup_status
from .util import recv_exact

ADB_SERVER = ("127.0.0.1", 5037)
FULL_CHECK_EVERY = 10
FOREGROUND_RECHECK_EVERY = 3


class Readiness:
    def __init__(self):
        self.lock = threading.Lock()
        self.devices = []          # [{"serial", "state", "model"}] from adb's tracking stream
        self.in_front = None       # True/False once known for the connected phone
        self.api_up = None         # True/False: does the app's HTTP API answer right now?
        self.full = None           # last setup_status() result
        self.clients = []
        self.state = None
        self.recheck = threading.Event()
        self.watching = None       # serial whose foreground is being watched
        for target in (self._track_devices, self._full_checks, self._foreground_recheck, self._api_probe):
            threading.Thread(target=target, daemon=True).start()

    # ---- subscribers (Server-Sent Events) ----

    def subscribe(self, q=None):
        q = q or queue.Queue(maxsize=100)
        with self.lock:
            self.clients.append(q)
            state = self.state
        return q, state

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def snapshot(self):
        with self.lock:
            return self.state

    # ---- combining live values with the last full check ----

    def _ready_device(self):
        return next((d for d in self.devices if d["state"] == "device"), None)

    def _publish(self):
        with self.lock:
            full = self.full
            dev = self._ready_device()
            if full is None:
                return
            groups = {k: [dict(c) for c in v] for k, v in full["groups"].items()}
            # The connection check always reflects the live device list.
            if dev:
                wireless = ":" in dev["serial"] or "_adb-tls-connect" in dev["serial"]
                conn = _check(True, "Connected over wireless debugging" if wireless else "Connected (over USB)",
                              f"{dev['model'] or dev['serial']} · {dev['serial']}", id="adb")
            elif any(d["state"] == "unauthorized" for d in self.devices):
                conn = _check(False, "Connected over wireless debugging", "the phone hasn't allowed this computer yet",
                              "Unlock the phone and confirm the debugging prompt, or pair again below.", id="adb")
            else:
                conn = _check(False, "Connected over wireless debugging", "no phone connected",
                              "Turn on Wireless debugging on the phone and pair it below (QR code or pairing code).", id="adb")
            phone = [c for c in groups["phone"] if c.get("id") != "adb"]
            groups["phone"] = [conn] + (phone if dev else [])
            if not dev:
                groups["app"] = [_check(False, "Blackmagic Camera", "connect the phone first")]
            elif self.in_front is not None:
                for c in groups["app"]:
                    if c.get("id") == "app-front":
                        c.update(ok=self.in_front, detail="in front on the phone" if self.in_front else "not in front",
                                 fix=None if self.in_front else "Bring Blackmagic Camera to the front on the phone (or use the button below). "
                                                                "The dashboard only runs while the app is open in front.")
            if dev and self.api_up is not None:  # the live API probe beats the last full check
                for c in groups["app"]:
                    if c.get("id") in ("http-server", "reachable") and self.api_up:
                        c.update(ok=True, fix=None, detail="answering" if c["id"] == "reachable" else f"port {config.API_PORT}")
                    elif c.get("id") == "reachable" and not self.api_up:
                        c.update(ok=False, detail="not answering",
                                 fix="Wait a moment after opening Blackmagic Camera; if it stays like this, check Enable HTTP Server.")
            ready = all(c["ok"] or c["optional"] for g in groups.values() for c in g)
            state = {"ready": ready, "groups": groups, "devices": list(self.devices), "updated": time.time(),
                     "live": {"connected": bool(dev), "model": dev["model"] if dev else None,
                              "wireless": bool(dev) and (":" in dev["serial"] or "_adb-tls-connect" in dev["serial"]),
                              "inFront": bool(dev) and bool(self.in_front)}}
            changed = self.state is None or {k: v for k, v in state.items() if k != "updated"} != \
                {k: v for k, v in self.state.items() if k != "updated"}
            self.state = state
            clients = list(self.clients) if changed else []
        for q in clients:
            try:
                q.put_nowait(("@setup", state))
            except queue.Full:
                pass

    # ---- adb connection: tracking stream ----

    def _track_devices(self):
        while True:
            try:
                subprocess.run(["adb", "start-server"], capture_output=True, timeout=20)
                with socket.create_connection(ADB_SERVER, timeout=10) as s:
                    req = b"host:track-devices-l"
                    s.sendall(b"%04x" % len(req) + req)
                    if s.recv(4) != b"OKAY":
                        raise RuntimeError("adb refused track-devices")
                    s.settimeout(None)
                    while True:
                        n = int(recv_exact(s, 4), 16)
                        self._on_devices(recv_exact(s, n).decode(errors="replace") if n else "")
            except Exception as e:
                print("readiness: adb tracking restarted:", e, flush=True)
                with self.lock:
                    self.devices = []
                self._publish()
                time.sleep(1)

    def _on_devices(self, payload):
        devices = []
        for line in payload.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                model = next((p[6:] for p in parts if p.startswith("model:")), "").replace("_", " ")
                devices.append({"serial": parts[0], "state": parts[1], "model": model})
        with self.lock:
            before = self._ready_device()
            self.devices = devices
            dev = self._ready_device()
            if not dev:
                self.in_front = None
        if dev and (not before or before["serial"] != dev["serial"]):
            self._watch_foreground(dev["serial"])
        self._publish()
        self.recheck.set()

    # ---- app in front: logcat events ----

    def _watch_foreground(self, serial):
        if self.watching == serial:
            return
        self.watching = serial
        threading.Thread(target=self._foreground_loop, args=(serial,), daemon=True).start()

    def _query_foreground(self):
        try:
            front = foreground_app() == config.APP_PACKAGE
        except Exception:
            return
        with self.lock:
            changed = front != self.in_front
            self.in_front = front
        if changed:
            self._publish()
            self.recheck.set()

    def _foreground_loop(self, serial):
        self._query_foreground()
        tags = ["wm_on_top_resumed_gained_called:I", "wm_on_top_resumed_lost_called:I",
                "wm_on_resume_called:I", "wm_on_paused_called:I", "*:S"]
        while self.watching == serial:
            p = subprocess.Popen(["adb", "-s", serial, "logcat", "-b", "events", "-T", "1", *tags],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                for line in p.stdout:
                    if self.watching != serial:
                        break
                    if "wm_on_" in line:
                        self._query_foreground()
            finally:
                p.kill()
            with self.lock:
                still = any(d["serial"] == serial and d["state"] == "device" for d in self.devices)
            if not still:
                break
            time.sleep(1)
        if self.watching == serial:
            self.watching = None

    def _foreground_recheck(self):
        while True:
            time.sleep(FOREGROUND_RECHECK_EVERY)
            with self.lock:
                dev = self._ready_device()
            if dev:
                self._query_foreground()

    # ---- the app's HTTP API: direct probe ----

    def _api_probe(self):
        while True:
            with self.lock:
                dev, front, was = self._ready_device(), self.in_front, self.api_up
            if not dev:
                if was is not None:
                    with self.lock:
                        self.api_up = None
                time.sleep(0.5)
                continue
            try:
                url = f"https://{phone_host()}:{config.API_PORT}/control/api/v1/system"
                with urllib.request.urlopen(url, context=config.PHONE_SSL, timeout=1):
                    up = True
            except Exception:
                up = False
            if up != was:
                with self.lock:
                    self.api_up = up
                self._publish()
                self.recheck.set()
                if up and events.hub:
                    events.hub.wake()  # reconnect the live camera state right away
            time.sleep(0.3 if front and not up else 2)

    # ---- everything else ----

    def _full_checks(self):
        while True:
            try:
                st = setup_status()
                with self.lock:
                    self.full = st
                self._publish()
            except Exception as e:
                print("readiness: setup check failed:", e, flush=True)
            self.recheck.wait(FULL_CHECK_EVERY)
            self.recheck.clear()



readiness = None  # created by server.py
