"""Talking to the phone over adb: finding/reconnecting the device, running commands, locating scrcpy-server."""
import glob
import os
import re
import subprocess
import threading

from . import config, mdns
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
            for parts in mdns_services():
                if parts[1] == "_adb-tls-connect._tcp":
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

def set_adb_identity(name, computer=""):
    """Name this computer in the phone's Wireless debugging → Paired devices list.

    adb sends "<LOGNAME>@<HOSTNAME>" when pairing (not the comment in adbkey.pub), and Android cuts the name
    at the first space, so name should have none. Applies to new pairings; restarts the adb server so it
    picks up the environment.
    """
    os.environ["LOGNAME"] = name.replace(" ", "-")
    if computer:
        os.environ["HOSTNAME"] = computer.replace(" ", "-")
    subprocess.run(["adb", "kill-server"], capture_output=True, timeout=15)


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
    """Wireless debugging services seen on the network: [[instance, service, "ip:port"], ...].

    adb's own mDNS (Google's adb has it; Debian's, used in the Docker image, doesn't) plus our own query
    (mdns.py), which also works with Debian's adb wherever multicast reaches the helper.
    """
    out = subprocess.run(["adb", "mdns", "services"], capture_output=True, text=True, timeout=10).stdout
    found = [line.split() for line in out.splitlines() if "._tcp" in line and len(line.split()) >= 3]
    near = settings.get("phoneIp") or (settings.get("lastAddress") or "").rpartition(":")[0] or None
    seen = {(f[1], f[2]) for f in found}
    for instance, service, addr in mdns.browse(near=near):
        if (service, addr) not in seen:
            found.append([instance, service, addr])
    return found
