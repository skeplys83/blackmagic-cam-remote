"""Changing the recording resolution and color space (neither is in the app's REST API).

Blackmagic Camera (Android) accepts a new resolution on /system/format but reverts it about a second later: it
keeps its own resolution and "Open Gate" settings and rebuilds the format from them. Presets carry those settings
(appsettings.resolutionSize, appsettings.isOpenGate), and loading a preset applies them properly. So: save the current
state as a temporary preset, change its resolution fields, upload it, load it (the app stores the upload without
the .bmcpreset extension), delete it.
Everything else in the camera stays as it was, because the preset is a snapshot of the current state.

Color space works the same way (appsettings.colorSpace), but only profiles the phone's cameras support may be set:
an unsupported one can crash the app's video encoder in a loop, after which the app resets all its settings
(seen with "Vendor Log HDR" on a Pixel 10). So the options come from Android's camera service: the 10-bit dynamic
range profiles every camera lists (availableDynamicRangeProfilesMap), plus standard (Rec.709). The current value is
the active stream's "Dynamic Range Profile" in `dumpsys media.camera`; a watcher publishes it as `@colorSpace`.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import config
from . import events
from .adb import adb, phone_host

TEMP = "bcr-resolution"  # the page hides presets starting with "bcr-"


def _api(method, path, data=None, headers=None):
    req = urllib.request.Request(f"https://{phone_host()}:{config.API_PORT}/control/api/v1/{path}",
                                 data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, context=config.PHONE_SSL, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _cleanup():
    for name in (TEMP, f"{TEMP}.bmcpreset"):
        _api("DELETE", f"presets/{name}")


def _apply(change, done, what):
    """Save the current state as a temporary preset, let change(appsettings) edit it, load it, wait for done()."""
    status, raw = _api("GET", "transports/0/record")
    if status == 200 and json.loads(raw).get("recording"):
        raise RuntimeError("stop recording first")
    _cleanup()  # a leftover from an interrupted change would block saving
    status, _ = _api("PUT", f"presets/{TEMP}.bmcpreset")  # snapshot of the current settings
    if status >= 300:
        raise RuntimeError(f"couldn't save a temporary preset ({status})")
    try:
        status, raw = _api("GET", f"presets/{TEMP}.bmcpreset")
        if status != 200:
            raise RuntimeError(f"couldn't read the temporary preset ({status})")
        preset = json.loads(raw)
        change(preset["appsettings"])
        _api("DELETE", f"presets/{TEMP}.bmcpreset")
        status, raw = _api("POST", "presets", json.dumps(preset, indent=2).encode(),
                           {"Content-Type": "application/octet-stream",
                            "Content-Disposition": f'attachment; filename="{TEMP}.bmcpreset"'})
        if status >= 300:
            raise RuntimeError(f"the camera rejected the change ({status}: {raw.decode(errors='replace')[:200]})")
        stored = json.loads(raw).get("presetAdded") or TEMP
        status, _ = _api("PUT", "presets/active", json.dumps({"preset": stored}).encode(), {"Content-Type": "application/json"})
        if status >= 300:
            raise RuntimeError(f"couldn't apply the change ({status})")
        for _ in range(24):
            time.sleep(0.25)
            result = done()
            if result is not None:
                return result
        raise RuntimeError(f"the camera didn't switch to the new {what}")
    finally:
        _cleanup()


def set_resolution(width, height, open_gate):
    def change(a):
        a["resolutionSize"] = f"{int(width)}x{int(height)}"
        a["isOpenGate"] = bool(open_gate)

    def done():  # the camera reports the new size
        status, raw = _api("GET", "system/videoFormat")
        f = json.loads(raw) if status == 200 else {}
        return f if (f.get("width"), f.get("height")) == (int(width), int(height)) else None

    return _apply(change, done, "resolution")


# ---------- color space ----------
# Android dynamic range profile (CameraCharacteristics/DynamicRangeProfiles) → the app's preset value and a label.
# Only these are offered; other app profiles (P3, sRGB, Log, HDR10(+), Dolby Vision) are untested or crash-prone.
COLOR_SPACES = {1: (0, "Rec.709"), 2: (1, "HLG10 (BT.2020)")}
_supported = None  # profile codes all cameras support, read once per connection


def _dumpsys_camera(pattern, extra=""):  # static info (capabilities) only
    r = adb("shell", f"dumpsys media.camera | grep {extra} -E '{pattern}'", timeout=10)
    return r.stdout.decode(errors="replace")


def supported_profiles():
    global _supported
    if _supported is None:
        out = _dumpsys_camera("availableDynamicRangeProfilesMap", "-A1")
        # "android.request.availableDynamicRangeProfilesMap (c0013): int64[3]" then the values on the next line, one
        # entry per camera; entries are (profile, compatible profiles, latency) triples. (The key's name also shows
        # up in key lists, which don't have this shape.)
        sets = [set([int(n) for n in re.findall(r"\d+", vals)][0::3])
                for vals in re.findall(r"availableDynamicRangeProfilesMap \([0-9a-f]+\):[^\n]*\n\s*\[([^\]]*)\]", out)]
        profiles = set.intersection(*sets) if sets else set()
        _supported = sorted({1} | (profiles & set(COLOR_SPACES)))
    return _supported


def current_profile():
    """The open camera's stream dynamic range profile, or None while no camera is open.

    Only the live "Camera device N dynamic info" sections count: the same lines also appear in old snapshots under
    "Camera error traces". The camera is closed while the app shows its own settings or another screen.
    """
    out = adb("shell", "dumpsys media.camera | sed -n '/^== Camera device [0-9]* dynamic info/,/^== Camera Provider/p'",
              timeout=10).stdout.decode(errors="replace")
    m = re.search(r"Dynamic Range Profile: 0x([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


def color_space_state():
    cur = current_profile()
    return {"current": COLOR_SPACES[cur][1] if cur in COLOR_SPACES else None,
            "options": [COLOR_SPACES[p][1] for p in supported_profiles()]}


def set_color_space(label):
    profile = next((p for p in supported_profiles() if COLOR_SPACES[p][1] == label), None)
    if profile is None:
        raise ValueError(f"{label} isn't supported by this phone")
    status, raw = _api("GET", "system/format")
    if profile != 1 and status == 200 and not json.loads(raw).get("codec", "").startswith("H265"):
        raise ValueError(f"{label} needs the H.265 codec")

    def change(a):
        a["colorSpace"] = COLOR_SPACES[profile][0]

    def done():
        return color_space_state() if current_profile() == profile else None

    if current_profile() is None:
        raise RuntimeError("Blackmagic Camera isn't showing its camera view (camera closed); go back to it first")
    state = _apply(change, done, "color space")
    _publish(state)
    return state


_last = None


def _publish(state):
    global _last
    if state["current"] and state != _last and events.hub:
        _last = state
        events.hub.publish("@colorSpace", state)


def watch(interval=5):
    """Publish the color space as `@colorSpace` whenever it changes (also when changed on the phone).

    The app announces no event for it (it's set in the app's own settings), so this is checked every few seconds,
    except while recording.
    """
    def run():
        global _supported, _last
        while True:
            time.sleep(interval)
            if not (events.hub and events.hub.connected):
                _supported = _last = None  # re-read after a reconnect (another phone, app update)
                continue
            if events.hub.recording():
                continue  # can't change while recording; no adb load then
            try:
                _publish(color_space_state())
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()
