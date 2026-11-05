"""Changing the recording resolution.

Blackmagic Camera (Android) accepts a new resolution on /system/format but reverts it about a second later: it
keeps its own resolution and "Open Gate" settings and rebuilds the format from them. Presets carry those settings
(appsettings.resolutionSize, appsettings.isOpenGate), and loading a preset applies them properly. So: save the current
state as a temporary preset, change its resolution fields, upload it, load it (the app stores the upload without
the .bmcpreset extension), delete it.
Everything else in the camera stays as it was, because the preset is a snapshot of the current state.
"""
import json
import time
import urllib.error
import urllib.request

from . import config
from .adb import phone_host

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


def set_resolution(width, height, open_gate):
    _cleanup()  # a leftover from an interrupted change would block saving
    status, _ = _api("PUT", f"presets/{TEMP}.bmcpreset")  # snapshot of the current settings
    if status >= 300:
        raise RuntimeError(f"couldn't save a temporary preset ({status})")
    try:
        status, raw = _api("GET", f"presets/{TEMP}.bmcpreset")
        if status != 200:
            raise RuntimeError(f"couldn't read the temporary preset ({status})")
        preset = json.loads(raw)
        preset["appsettings"]["resolutionSize"] = f"{int(width)}x{int(height)}"
        preset["appsettings"]["isOpenGate"] = bool(open_gate)
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
        for _ in range(20):  # wait until the camera reports the new size
            time.sleep(0.25)
            status, raw = _api("GET", "system/videoFormat")
            if status == 200 and (lambda f: (f["width"], f["height"]) == (int(width), int(height)))(json.loads(raw)):
                return json.loads(raw)
        raise RuntimeError("the camera didn't switch to the new resolution")
    finally:
        _cleanup()
