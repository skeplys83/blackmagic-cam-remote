"""Live view: one scrcpy-server session (screen video + microphone) relayed to the browser.

Frames sent to the page: type u8, pts u64 (microseconds), size u32, payload.
"""
import random
import socket
import struct
import subprocess
import threading
import time

from .adb import adb, adb_serial, scrcpy_server
from .screen import screen
from .util import free_port, recv_exact


T_SESSION, T_CONFIG, T_KEY, T_DELTA, T_AUDIO, T_REPLACED = 0, 1, 2, 3, 4, 5

F_SESSION, F_CONFIG, F_KEY = 1 << 63, 1 << 62, 1 << 61

PTS_MASK = (1 << 61) - 1

PRESETS = {"native": (2424, 12_000_000), "medium": (1600, 6_000_000), "low": (1024, 3_000_000), "ultralow": (640, 1_000_000)}

MAX_FPS = 24

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
        recv_exact(video, 64)  # device name
        if recv_exact(video, 4) != b"h264":
            raise RuntimeError("unexpected video codec")
        self.audio_ok = recv_exact(audio, 4) == b"\0raw"
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
                    hdr = recv_exact(video, 12)
                    head, size = struct.unpack(">QI", hdr)
                    if head & F_SESSION:
                        send(T_SESSION, 0, hdr[4:12])  # width, height
                        continue
                    payload = recv_exact(video, size)
                    t = T_CONFIG if head & F_CONFIG else T_KEY if head & F_KEY else T_DELTA
                    send(t, head & PTS_MASK, payload)
            except Exception:
                done.set()

        def audio_loop():
            try:
                while not done.is_set():
                    head, size = struct.unpack(">QI", recv_exact(audio, 12))
                    payload = recv_exact(audio, size)
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

def take_over(m):
    """Make m the only live-view stream; an older one is told it was replaced."""
    global _mirror_current
    with _mirror_lock:
        if _mirror_current:
            _mirror_current.replaced = True
            _mirror_current.stop()
        kill_stale_mirrors()
        _mirror_current = m
