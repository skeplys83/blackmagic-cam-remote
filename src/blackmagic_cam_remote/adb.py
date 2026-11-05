"""Talking to the phone over adb: finding/reconnecting the device, running commands, locating scrcpy-server."""
import glob
import os
import pathlib
import re
import subprocess
import threading

from . import config
from .settings import settings


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
        if not devices and settings.get("lastAddress"):
            subprocess.run(["adb", "connect", settings.get("lastAddress")], capture_output=True, timeout=10)
            out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
            devices = [l.split("\t")[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]
        if not devices:
            raise RuntimeError("phone not connected over adb (see setup)")
        _serial = devices[0]
        return _serial

_phone_ip = {"serial": None, "ip": None}

def phone_host():
    """The phone's Wi-Fi IP (BCR_PHONE / --phone overrides; otherwise asked over adb)."""
    if config.PHONE:
        return config.PHONE
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

def name_adb_key(name):
    """Give this computer's adb key a readable name: the phone lists it under Wireless debugging → Paired devices.

    The name is the comment at the end of ~/.android/adbkey.pub, sent to the phone when pairing (it doesn't rename
    a phone that is already paired). Must run before the adb server starts, which reads the key once.
    """
    d = pathlib.Path(os.path.expanduser("~")) / ".android"
    key, pub = d / "adbkey", d / "adbkey.pub"
    subprocess.run(["adb", "kill-server"], capture_output=True, timeout=15)
    if not key.exists():
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["adb", "keygen", str(key)], capture_output=True, timeout=30)
    if pub.exists():
        named = pub.read_text().split()[0] + " " + name + "\n"
        if pub.read_text() != named:
            pub.write_text(named)


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

def mdns_services():
    out = subprocess.run(["adb", "mdns", "services"], capture_output=True, text=True, timeout=10).stdout
    return [line.split() for line in out.splitlines() if "._tcp" in line and len(line.split()) >= 3]
