"""Phone screen on/off while the camera keeps running (scrcpy's display-power control)."""
import random
import re
import socket
import subprocess
import threading
import time

from .adb import adb, adb_serial, scrcpy_server
from .util import free_port


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
