"""The phone's profile: what its cameras and the Blackmagic app can do, kept in a file per phone and app version.

Two sources, kept apart because they answer different questions:
- hardware: Android's camera service (`dumpsys media.camera`), read once per phone without touching the camera.
  Covers every lens at once (focal length, aperture, OIS, minimum focus, sensor, high-speed modes, HDR profiles).
  It's what the hardware can do, which is more than the app may offer.
- app: what Blackmagic Camera offers for a lens (formats, frame rates, off-speed limits, codecs, ISO, shutter, zoom).
  The app's API only describes the active lens, so this is learned whenever a lens is active, or for all lenses at
  once with a scan (switches through them and restores the previous settings from a temporary preset).
The dropdowns keep using the live values; the profile adds what other lenses can do and explains limits.
File: <data>/profiles/<manufacturer>_<model>_<app version>.json (no serials or addresses).
"""
import json
import math
import re
import threading
import time
from datetime import datetime, timezone

from . import config
from . import events
from .adb import adb
from .recformat import _api

APP = "com.blackmagicdesign.android.blackmagiccam"
SCAN_PRESET = "bcr-scan"  # the page hides presets starting with "bcr-"
HDR_PROFILES = {2: "HLG10", 4: "HDR10", 8: "HDR10+", 16: "Dolby Vision", 32: "Dolby Vision", 64: "Dolby Vision",
                128: "Dolby Vision", 256: "Dolby Vision", 512: "Dolby Vision", 1024: "Dolby Vision", 2048: "Dolby Vision"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- hardware (Android camera service) ----------

def parse_camera_dump(text):
    """Static characteristics per camera id from `dumpsys media.camera`: {id: {key: [values], ...}}.

    Logical cameras ("internal/N") have their own block and one block per physical lens ("Physical camera K");
    physical blocks get "parent" set and inherit the facing from it.
    """
    blocks, cur, key = {}, None, None
    for line in text.splitlines():
        m = re.search(r"== Camera HAL device .*/(\d+) \(.*\) (static information|dumpState): ==", line)
        if m:
            cur = m.group(1) if m.group(2) == "static information" else None
            if cur is not None:
                blocks.setdefault(cur, {})
            key = None
            continue
        if cur is None:
            continue
        m = re.match(r"\s+Physical camera (\d+) characteristics:", line)
        if m:
            parent = cur.split("/")[0]
            cur = m.group(1)
            blocks.setdefault(cur, {})["parent"] = [parent]
            key = None
            continue
        m = re.match(r"\s+android\.([\w.]+) \([0-9a-f]+\): \w+\[\d+\]\s*$", line)
        if m:
            key = m.group(1)
            blocks[cur][key] = []
            continue
        m = re.match(r"\s+\[(.*)\]\s*$", line)
        if m and key:
            blocks[cur][key] += m.group(1).split()
        elif not line.startswith("        "):
            key = None
    for b in blocks.values():  # only the facing; other keys of the logical camera don't apply to each lens
        parent = blocks.get((b.get("parent") or [None])[0], {})
        if "lens.facing" in parent:
            b.setdefault("lens.facing", parent["lens.facing"])
    return blocks


def _nums(values, cast=float):
    out = []
    for v in values or []:
        try:
            out.append(cast(v))
        except ValueError:
            pass
    return out


def hardware_lenses(blocks):
    """Per camera: the hardware facts the UI can use."""
    out = {}
    for cid, b in blocks.items():
        if "lens.info.availableFocalLengths" not in b:
            continue
        focal = _nums(b.get("lens.info.availableFocalLengths"))
        size = _nums(b.get("sensor.info.physicalSize"))
        diag = math.hypot(*size) if len(size) == 2 else 0
        hs = _nums(b.get("control.availableHighSpeedVideoConfigurations"), int)
        high = sorted({(hs[i], hs[i + 1], hs[i + 3]) for i in range(0, len(hs) - 4, 5)}, key=lambda x: (-x[0], -x[2]))
        hdr = _nums(b.get("request.availableDynamicRangeProfilesMap"), int)[0::3]
        min_focus = _nums(b.get("lens.info.minimumFocusDistance"))
        pixels = _nums(b.get("sensor.info.pixelArraySize"), int)
        out[cid] = {
            "facing": (b.get("lens.facing") or ["?"])[0].lower(),
            "logical": "parent" not in b and bool(b.get("logicalMultiCamera.physicalIds")),
            "focalLengthMm": focal[0] if focal else None,
            "focalLength35": round(focal[0] * 43.27 / diag) if focal and diag else None,
            "aperture": (_nums(b.get("lens.info.availableApertures")) or [None])[0],
            "ois": 1 in _nums(b.get("lens.info.availableOpticalStabilization"), int),
            "minFocusCm": round(100 / min_focus[0], 1) if min_focus and min_focus[0] > 0 else None,  # diopters → cm
            "sensorMm": [round(x, 2) for x in size] or None,
            "sensorPixels": pixels or None,
            "highSpeed": [{"size": f"{w}x{h}", "fps": fps} for w, h, fps in high],
            "hdr": sorted({HDR_PROFILES[p] for p in hdr if p in HDR_PROFILES}),
        }
    return out


def match_lenses(app_lenses, hw):
    """App lens id → Android camera id, by facing and 35 mm-equivalent focal length (the app reports that)."""
    out = {}
    for lens in app_lenses:
        cands = [(cid, h) for cid, h in hw.items() if h["facing"] == lens["facing"] and h["focalLength35"]
                 and not h["logical"]]
        if lens["id"] in hw and hw[lens["id"]]["facing"] == lens["facing"]:
            out[lens["id"]] = lens["id"]  # the app uses Android's camera ids where it can
        elif cands:
            out[lens["id"]] = min(cands, key=lambda c: abs(c[1]["focalLength35"] - lens["focalLength"]))[0]
    return out


# ---------- the profile ----------

class Profile:
    def __init__(self):
        self.data = None
        self.path = None
        self.lock = threading.Lock()
        self.scan_state = {"state": "idle", "message": ""}
        threading.Thread(target=self._run, daemon=True).start()

    # identity and file
    def _identity(self):
        props = adb("shell", "getprop ro.product.manufacturer; getprop ro.product.model; getprop ro.product.device; "
                    "getprop ro.build.version.release; getprop ro.build.id", timeout=10).stdout.decode().split("\n")
        ver = re.search(r"versionName=(\S+)", adb("shell", f"dumpsys package {APP} | grep -m1 versionName",
                                                  timeout=10).stdout.decode())
        man, model, device, android, build = (props + [""] * 5)[:5]
        return {"manufacturer": man.strip(), "model": model.strip(), "device": device.strip(),
                "android": android.strip(), "build": build.strip(), "appVersion": ver.group(1) if ver else "unknown"}

    def _load(self, ident):
        name = re.sub(r"[^\w.-]+", "-", f"{ident['manufacturer']}_{ident['model']}_{ident['appVersion']}")
        path = config.DATA / "profiles" / f"{name}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {"schema": 1, "phone": ident, "lenses": {}}
        data["phone"] = ident  # Android build may have changed
        self.data, self.path = data, path

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)
        self.publish()

    def publish(self):
        if events.hub and self.data:
            events.hub.publish("@profile", {**self.data, "scan": self.scan_state})

    # hardware part
    def _learn_hardware(self, app_lenses):
        blocks = parse_camera_dump(adb("shell", "dumpsys media.camera", timeout=30).stdout.decode(errors="replace"))
        hw = hardware_lenses(blocks)
        ids = match_lenses(app_lenses, hw)
        for lens in app_lenses:
            entry = self.data["lenses"].setdefault(lens["id"], {})
            entry.update(name=f"{'Front' if lens['facing'] == 'front' else lens['zoomFactor']} · {lens['focalLength']}mm",
                         facing=lens["facing"], focalLength=lens["focalLength"])
            if lens["id"] in ids:
                entry["hardware"] = {**hw[ids[lens["id"]]], "androidId": ids[lens["id"]]}
        self.data["hardwareLearned"] = _now()

    # app part (active lens only)
    def _get(self, path):
        status, raw = _api("GET", path)
        return json.loads(raw) if status == 200 else None

    def _read_app(self):
        """What the app offers for the active lens, read twice to skip values still settling after a switch."""
        reads = []
        for _ in range(2):
            active = self._get("lens/cameras/active")
            formats = self._get("system/supportedFormats")
            reads.append((active and active.get("id"), json.dumps(formats, sort_keys=True)))
            time.sleep(1)
        if reads[0] != reads[1] or not reads[0][0]:
            return None, None
        formats = json.loads(reads[0][1]) or {}
        iris = self._get("lens/iris/description") or {}
        zoom = self._get("lens/zoom/description") or {}
        ois = self._get("lens/opticalImageStabilization") or {}
        app = {
            "formats": [{"group": f["resolutionDescriptor"]["group"], "description": f["resolutionDescriptor"]["description"],
                         "size": f"{f['recordResolution']['width']}x{f['recordResolution']['height']}",
                         "sensor": f"{f['sensorResolution']['width']}x{f['sensorResolution']['height']}",
                         "frameRates": f["frameRates"], "offSpeed": [f["minOffSpeedFrameRate"], f["maxOffSpeedFrameRate"]],
                         "codecs": f.get("codecs") or []}
                        for f in formats.get("supportedFormats", [])],
            "isos": (self._get("video/supportedISOs") or {}).get("supportedISOs"),
            "shutterSpeeds": (self._get("video/supportedShutters") or {}).get("shutterSpeeds"),
            "zoomMm": [zoom.get("focalLength", {}).get("min"), zoom.get("focalLength", {}).get("max")],
            "aperture": (iris.get("apertureStop") or {}).get("min"),
            "oisControl": bool(ois.get("controlAvailable")),
            "learned": _now(),
        }
        return reads[0][0], app

    def _learn_active(self):
        lens_id, app = self._read_app()
        if not lens_id:
            return False
        entry = self.data["lenses"].setdefault(lens_id, {})
        old = {k: v for k, v in (entry.get("app") or {}).items() if k != "learned"}
        if old != {k: v for k, v in app.items() if k != "learned"}:
            entry["app"] = app
            self._save()
        return True

    def _setup(self):
        """Load or create the profile for the connected phone; learn the hardware part if missing."""
        ident = self._identity()
        if not self.data or self.data["phone"] != ident:
            self._load(ident)
        app_lenses = (self._get("lens/cameras") or {}).get("cameras", [])
        if not self.data.get("hardwareLearned") or set(self.data["lenses"]) != {c["id"] for c in app_lenses}:
            self._learn_hardware(app_lenses)
            self._save()
        else:
            self.publish()

    def _run(self):
        ready = False
        while True:
            time.sleep(5)
            if not (events.hub and events.hub.connected):
                ready = False
                continue
            if self.scan_state["state"] == "scanning":
                continue
            try:
                with self.lock:
                    if not ready:
                        self._setup()
                        ready = True
                    self._learn_active()
            except Exception as e:
                print("profile:", e)

    # scan all lenses
    def scan(self):
        if self.scan_state["state"] == "scanning":
            raise RuntimeError("a scan is already running")
        status, raw = _api("GET", "transports/0/record")
        if status == 200 and json.loads(raw).get("recording"):
            raise RuntimeError("stop recording first")
        self.scan_state = {"state": "scanning", "message": "Saving the current settings…"}
        self.publish()
        threading.Thread(target=self._scan, daemon=True).start()
        return self.scan_state

    def _set_scan(self, state, message):
        self.scan_state = {"state": state, "message": message}
        self.publish()

    def _scan(self):
        """Switch through every lens and learn what the app offers; restore everything from a preset afterwards.

        Switching lens can change the format (frame rate, off-speed), so a preset snapshot is the only safe way back.
        """
        for name in (SCAN_PRESET, f"{SCAN_PRESET}.bmcpreset"):
            _api("DELETE", f"presets/{name}")
        status, _ = _api("PUT", f"presets/{SCAN_PRESET}.bmcpreset")
        if status >= 300:
            return self._set_scan("failed", f"Couldn't save the current settings ({status}); nothing was changed.")
        before = (self._get("lens/cameras/active") or {}).get("id")
        failed = []
        try:
            with self.lock:
                if not self.data:
                    self._setup()
                lenses = [c for c in (self._get("lens/cameras") or {}).get("cameras", []) if c.get("isAvailable")]
                for i, lens in enumerate(lenses, 1):
                    self._set_scan("scanning", f"Lens {i} of {len(lenses)}: {self.data['lenses'].get(lens['id'], {}).get('name', lens['id'])}")
                    _api("PUT", "lens/cameras/active", json.dumps({"id": lens["id"]}).encode(), {"Content-Type": "application/json"})
                    for _ in range(10):  # wait for the switch, then for the app's lists to settle
                        time.sleep(1)
                        if (self._get("lens/cameras/active") or {}).get("id") == lens["id"]:
                            break
                    time.sleep(1.5)
                    if not self._learn_active():
                        failed.append(lens["id"])
        except Exception as e:
            failed.append(str(e))
        finally:
            self._set_scan("scanning", "Restoring your settings…")
            # A preset saved with PUT is stored under its file name (with .bmcpreset), an uploaded one without.
            stored = next((n for n in (self._get("presets") or {}).get("presets", []) if n.startswith(SCAN_PRESET)),
                          f"{SCAN_PRESET}.bmcpreset")
            status, _ = _api("PUT", "presets/active", json.dumps({"preset": stored}).encode(), {"Content-Type": "application/json"})
            time.sleep(3)
            for name in (SCAN_PRESET, f"{SCAN_PRESET}.bmcpreset"):
                _api("DELETE", f"presets/{name}")
            now = (self._get("lens/cameras/active") or {}).get("id")
        if status >= 300 or (before and now != before):
            return self._set_scan("failed", "Scan done, but restoring the settings didn't work; check the camera.")
        self.data["scanned"] = _now()
        self._save()
        self._set_scan("done", "All lenses scanned." if not failed else f"Scanned; {len(failed)} lens(es) couldn't be read.")


profile = None
