"""Phone information from adb: health (temperature, Wi-Fi, storage, battery), screen rotation, viewfinder position."""
import re

from .adb import adb
from .screen import screen


THERMAL = ["none", "light", "moderate", "severe", "critical", "emergency", "shutdown"]

def phone_status():
    cmd = ("dumpsys battery; echo @@; dumpsys thermalservice | grep -E 'Thermal Status|mType=3,'; echo @@; "
           "cmd wifi status | grep -m1 WifiInfo; echo @@; df -k /storage/emulated | tail -1; echo @@; settings get global zen_mode")
    out = adb("shell", cmd, timeout=15).stdout.decode(errors="replace").split("@@")
    bat, therm, wifi, df, zen = (out + [""] * 5)[:5]
    g = lambda pat, txt: (re.search(pat, txt) or [None, None])[1]
    temp = g(r"temperature: (\d+)", bat)
    # Android's skin temperature sensors have type 3 (TYPE_SKIN); names differ per phone (Pixel: VIRTUAL-SKIN).
    skin = g(r"Temperature\{mValue=([\d.]+), mType=3,", therm)
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

def orientation():
    """The phone's current screen rotation (0/90/180/270), as Android reports it."""
    out = adb("shell", "dumpsys display | grep -m1 -o 'orientation=[0-9]'", timeout=10).stdout.decode(errors="replace")
    m = re.search(r"orientation=(\d)", out)
    return {"rotation": int(m.group(1)) * 90 if m else None}

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
