"""Readiness checks shown on the setup screen, plus pairing (QR code or pairing code), connecting and launching the app."""
import platform
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

        pkg = adb("shell", f"dumpsys package {config.APP_PACKAGE} | grep -m1 versionName", timeout=10).stdout.decode().strip()
        installed = "versionName=" in pkg
        app.append(_check(installed, "Blackmagic Camera installed", pkg.split("=", 1)[-1] if installed else "not installed",
                          None if installed else "Install Blackmagic Camera from Google Play."))
        if installed:
            running = bool(adb("shell", f"pidof {config.APP_PACKAGE}", timeout=10).stdout.strip())
            app.append(_check(running, "Blackmagic Camera open", "" if running else "not running",
                              None if running else "Open Blackmagic Camera on the phone (or use the button below).",))
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

def qr_start():
    if not shutil.which("qrencode"):
        raise RuntimeError("qrencode is not installed; pair with a code instead")
    name = "bcr-" + secrets.token_hex(3)
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
                        settings.update({"lastAddress": s[2]})
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
        settings.update({"lastAddress": addr})
        return {"message": msg}
    if action == "launch":
        adb("shell", f"monkey -p {config.APP_PACKAGE} -c android.intent.category.LAUNCHER 1", timeout=15)
        return {"message": "started"}
    raise ValueError("unknown action")
