"""Readiness checks shown on the setup screen, plus pairing (QR code or pairing code), connecting and launching the app."""
import asyncio
import platform
import queue
import re
import secrets
import shutil
import string
import subprocess
import threading
import time
import urllib.request

from . import config
from .adb import adb, mdns_services, phone_host, scrcpy_server
from .settings import settings


def _check(ok, title, detail="", fix=None, optional=False, id=None):
    return {"ok": bool(ok), "title": title, "detail": detail, "fix": fix, "optional": optional, "id": id}


def foreground_app():
    """Package of the app currently in front on the phone (its top resumed activity), or None."""
    out = adb("shell", "dumpsys activity activities | grep -E 'topResumedActivity=|ResumedActivity: ' | head -3",
              timeout=10).stdout.decode(errors="replace")
    m = re.search(r"ActivityRecord\{\S+ u\d+ ([\w.]+)/", out)
    return m.group(1) if m else None

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
                            f"{d['model'] or d['serial']} · {d['serial']}" + ("" if wireless else " · wireless debugging is recommended"), id="adb"))
    elif any(d["state"] == "unauthorized" for d in devices):
        phone.append(_check(False, "Connected over wireless debugging", "the phone hasn't allowed this computer yet",
                            "Unlock the phone and confirm the debugging prompt, or pair again below.", id="adb"))
    else:
        phone.append(_check(False, "Connected over wireless debugging", "no phone connected",
                            "Turn on Wireless debugging on the phone and pair it below (QR code or pairing code).", id="adb"))

    if ready_devs:
        try:
            sdk = int(adb("shell", "getprop ro.build.version.sdk", timeout=10).stdout.decode().strip() or 0)
            phone.append(_check(sdk >= 33, "Android 13 or newer", f"Android API {sdk}",
                                None if sdk >= 33 else "Blackmagic Camera needs Android 13 or newer."))
        except Exception as e:
            phone.append(_check(False, "Android version", str(e)))

        pkg = adb("shell", f"dumpsys package {config.APP_PACKAGE} | grep -m1 versionName", timeout=10).stdout.decode().strip()
        installed = "versionName=" in pkg
        app.append(_check(installed, "Blackmagic Camera installed", pkg.split("=", 1)[-1] if installed else "not installed",
                          None if installed else "Install Blackmagic Camera from Google Play."))
        if installed:
            front = foreground_app() == config.APP_PACKAGE
            if front:
                app.append(_check(True, "Blackmagic Camera open", "in front on the phone", id="app-front"))
            else:
                running = bool(adb("shell", f"pidof {config.APP_PACKAGE}", timeout=10).stdout.strip())
                app.append(_check(False, "Blackmagic Camera open",
                                  "in the background" if running else "not running",
                                  "Bring Blackmagic Camera to the front on the phone (or use the button below). "
                                  "The dashboard only runs while the app is open in front.", id="app-front"))
            uid = (re.search(r"uid:(\d+)", adb("shell", f"pm list packages -U {config.APP_PACKAGE}", timeout=10).stdout.decode()) or [None, None])[1]
            owner = None
            tcp = adb("shell", "cat /proc/net/tcp /proc/net/tcp6", timeout=10).stdout.decode(errors="replace")
            for line in tcp.splitlines()[1:]:
                f = line.split()
                if len(f) > 7 and f[3] == "0A" and int(f[1].split(":")[1], 16) == config.API_PORT:
                    owner = f[7]
            if owner is None:
                app.append(_check(False, "HTTP server on", f"nothing is listening on port {config.API_PORT}",
                                  "In Blackmagic Camera: Settings → Network Access → turn on Enable HTTP Server."))
            elif owner != uid:
                name = adb("shell", f"cmd package list packages -U | grep 'uid:{owner}$'", timeout=10).stdout.decode().strip()
                name = name.split()[0].replace("package:", "") if name else f"uid {owner}"
                app.append(_check(False, "HTTP server on", f"port {config.API_PORT} is taken by another app: {name}",
                                  f"Close or uninstall {name}, then turn Enable HTTP Server off and on in Blackmagic Camera."))
            else:
                app.append(_check(True, "HTTP server on", f"port {config.API_PORT}"))
                try:
                    host = phone_host()
                    with urllib.request.urlopen(f"https://{host}:{config.API_PORT}/control/api/v1/system", context=config.PHONE_SSL, timeout=4):
                        pass
                    app.append(_check(True, "Reachable over the network", f"https://{host}:{config.API_PORT}"))
                except Exception as e:
                    app.append(_check(False, "Reachable over the network", f"can't reach the phone's API ({e})",
                                      "Put the phone and this computer on the same Wi-Fi network; guest networks often block devices from talking to each other."))
    else:
        app.append(_check(False, "Blackmagic Camera", "connect the phone first"))

    groups = {"computer": computer, "phone": phone, "app": app}
    ready = all(c["ok"] or c["optional"] for g in groups.values() for c in g)
    return {"ready": ready, "groups": groups, "devices": devices}

_qr = {"name": None, "password": None, "state": "idle", "message": "", "started": 0}

_qr_lock = threading.Lock()

def known_phone_ip():
    """The phone's IP from an earlier pairing/connection, if any."""
    return settings.get("phoneIp") or (settings.get("lastAddress") or "").rpartition(":")[0]


# Wireless debugging listens on random high ports; every one seen so far was in 32768-49999, so scan that first.
SCAN_ORDER = list(range(32768, 50000)) + list(range(50000, 65536)) + list(range(30000, 32768))


def open_ports(ip, on_port=None, keep_going=lambda: True):
    """Open TCP ports on ip, calling on_port(port) as soon as each is found.

    400 probes at a time with a 1 s timeout: fast on a LAN, and still reliable through Docker Desktop's
    network layer (which drops everything when flooded). A full sweep takes about 10-30 s.
    """
    async def scan():
        sem = asyncio.Semaphore(400)
        found = set()

        async def probe(port):
            if not keep_going():
                return
            async with sem:
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 1.0)
                    w.close()
                    found.add(port)
                    if on_port:
                        on_port(port)
                except Exception:
                    pass
        await asyncio.gather(*(probe(p) for p in SCAN_ORDER))
        return found
    return asyncio.run(scan())


def qr_start(ip=""):
    if not shutil.which("qrencode"):
        raise RuntimeError("qrencode is not installed; pair with a code instead")
    ip = (ip or "").strip() or known_phone_ip()
    if ip and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        raise ValueError("enter the phone's IP address, e.g. 192.168.1.20")
    if ip:
        settings.update({"phoneIp": ip})
    name = "bcr-" + secrets.token_hex(3)
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
    payload = f"WIFI:T:ADB;S:{name};P:{password};;"
    svg = subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-o", "-", payload],
                         capture_output=True, text=True, timeout=10).stdout
    with _qr_lock:
        _qr.update(name=name, password=password, state="waiting", message="Scan the code with the phone", started=time.time())
    threading.Thread(target=_qr_watch, args=(name, password, ip), daemon=True).start()
    return {"svg": svg, "name": name, "ip": ip}


def _qr_watch(name, password, ip):
    """Find the phone's pairing port after it scans the QR code, pair, then connect.

    Two ways to find it, whichever answers first: mDNS (the phone advertises _adb-tls-pairing._tcp under the
    QR's name), and, when the phone's IP is known, scanning that IP: every open port found gets a pairing
    attempt right away (wrong ports just refuse). The scan works where mDNS can't reach (Docker Desktop).
    """
    def current():
        with _qr_lock:
            return _qr["name"] == name

    def set_state(state, message):
        with _qr_lock:
            if _qr["name"] == name:
                _qr.update(state=state, message=message)

    found = queue.Queue()
    # Ports that can't be the pairing port: the camera's API and its remote-control port, and the
    # phone's connect port if we know it from an earlier connection.
    skip = {config.API_PORT, 2222}
    last = settings.get("lastAddress") or ""
    if last.startswith(ip + ":"):
        skip.add(int(last.rpartition(":")[2]))

    def scan_loop():
        seen = set()

        def on_port(port):
            if port not in seen and port not in skip:
                seen.add(port)
                found.put(f"{ip}:{port}")
        try:
            while current():
                open_ports(ip, on_port, current)
                seen.clear()  # next sweep: offer ports again (e.g. a pairing port that opened late)
        except Exception as e:
            print("qr: port scan failed:", e, flush=True)

    if ip:
        threading.Thread(target=scan_loop, daemon=True).start()
        set_state("waiting", f"Scan the code with the phone (watching {ip})")
    seen_mdns, tried, refused = False, set(), []
    deadline = time.time() + 180
    while time.time() < deadline and current():
        services = mdns_services()
        seen_mdns = seen_mdns or bool(services)
        candidates = [s[2] for s in services if s[0] == name and s[1] == "_adb-tls-pairing._tcp"]
        try:
            candidates.append(found.get(timeout=1))
            while not found.empty():
                candidates.append(found.get_nowait())
        except queue.Empty:
            pass
        for addr in candidates:
            if addr in tried:
                continue
            tried.add(addr)
            set_state("pairing", f"Trying to pair with {addr}…")
            try:
                out = subprocess.run(["adb", "pair", addr, password], capture_output=True, text=True, timeout=8)
                msg = (out.stdout + out.stderr).strip()
            except subprocess.TimeoutExpired:
                msg = "no answer"
            if "Successfully paired" in msg:
                return _connect_after_pairing(addr, refused, set_state)
            refused.append(addr)
            set_state("waiting", f"Scan the code with the phone (watching {ip or 'the network'})")
        if not seen_mdns and not ip and time.time() - _qr["started"] > 20:
            set_state("waiting", "The phone isn't found automatically here (no local-network discovery, e.g. Docker Desktop). "
                                 "Enter the phone's IP address below and show a new code, or pair with a code instead.")
    if current():
        set_state("timeout", "The code expired. Show a new one to try again.")


def _connect_after_pairing(pair_addr, refused, set_state):
    ip, _, pair_port = pair_addr.rpartition(":")
    settings.update({"phoneIp": ip})
    set_state("connecting", "Paired. Connecting…")

    def connect(addr):
        try:
            out = subprocess.run(["adb", "connect", addr], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return False
        if "connected to" in out.stdout + out.stderr and "failed" not in out.stdout + out.stderr:
            settings.update({"lastAddress": addr})
            set_state("done", f"Paired and connected to {addr}")
            return True
        return False

    for _ in range(3):  # mDNS: the phone advertises its connect port right after pairing
        for s in mdns_services():
            if s[1] == "_adb-tls-connect._tcp" and s[2].startswith(ip + ":") and connect(s[2]):
                return
        time.sleep(1)
    # Without mDNS: the connect port is one of the open ports that refused pairing, or the last known one.
    last = settings.get("lastAddress") or ""
    for addr in ([last] if last.startswith(ip + ":") else []) + refused:
        if connect(addr):
            return
    for port in sorted(open_ports(ip) - {int(pair_port), config.API_PORT, 2222}):
        if connect(f"{ip}:{port}"):
            return
    set_state("done", "Paired. Enter the IP address & port from the Wireless debugging screen below to connect.")


def setup_action(action, body):
    if action == "qr":
        return qr_start(str(body.get("ip", "")))
    if action == "qr-status":
        with _qr_lock:
            return {"state": _qr["state"], "message": _qr["message"], "name": _qr["name"], "knownIp": known_phone_ip()}
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
        settings.update({"lastAddress": addr})
        return {"message": msg}
    if action == "launch":
        adb("shell", f"monkey -p {config.APP_PACKAGE} -c android.intent.category.LAUNCHER 1", timeout=15)
        return {"message": "started"}
    raise ValueError("unknown action")
