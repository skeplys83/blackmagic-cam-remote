# Blackmagic Camera (Android) API: what actually works

Tested with Blackmagic Camera 3.5.1 on a Pixel 10 (Android 17). Base URL `https://<phone>:4444/control/api/v1`,
self-signed certificate, no authentication. The app ships its own OpenAPI files at `/control/documentation.html`,
but the Android implementation differs from them in many places. **Check here first, then verify on a phone.**

## General
- **Bodies are decoded strictly** into the app's models (kotlinx). Missing or unknown fields → `400`. Safe default:
  send exactly what GET returns, changed where needed.
- `/access/request`, `/access/status` are stubs (always "accepted"); no authentication is needed.
- The HTTP server only runs while the app is **in front**; it is back ~1.8 s after the app returns.
- Port 4444 is fixed. Another app holding it (e.g. "Android IP Camera") silently prevents the server from starting.
- The event websocket `/control/api/v1/event/websocket`: send
  `{"type":"request","data":{"action":"subscribe","properties":[...]},"id":1}` with the list from `/event/list`;
  the response carries current `values`, then `{"type":"event","data":{"action":"propertyValueChanged",...}}`.
  Event payloads differ slightly from REST (e.g. focus assist mode `"Colored Lines"` vs `"ColoredLines"`,
  `/transports/0/record` without `clipName`, `/system` shaped like `/system/format`).
- The phone screen is display `Device` in REST but `LCD` in events. Only `Device` and `HDMI` exist;
  `/monitoring/display` returns 500.

## Endpoint notes

| Endpoint | Notes |
|---|---|
| `PUT /transports/0/record` | `{"recording": true/false, "clipName": null}` (POST without body also exists) |
| `PUT /transports/0/doStillCapture` | **Not implemented** (501 in handler) |
| `PUT /lens/focus/autoFocus` | Full GET shape: `{"supported","enabled","mode","state","errors"}`; only `enabled` is used |
| `PUT /lens/focus` | Send **only** `{"normalized": x}`. If `focusDistance` is present the app uses it and ignores `normalized` (opposite of the spec) |
| `PUT /lens/zoom` | Only `{"normalized": x}` works (`focalLength` → 400) |
| `PUT /lens/cameras/active` | `{"id": "2"}` |
| `PUT /lens/opticalImageStabilization` | `{"enabled": bool}`; `controlAvailable` is false on lenses without OIS (e.g. the 0.6x ultra-wide). The app's own stabilization mode (Standard/Off) is neither in the API nor in presets |
| `PUT /video/iso` | Value must be in `/video/supportedISOs`; switches auto exposure off |
| `PUT /video/shutter` | `{"shutterSpeed": n}` or `{"shutterAngle": hundredths}` (18000 = 180°), value from `/video/supportedShutters`; switches auto exposure off |
| `PUT /video/shutter/measurement` | `{"measurement": "ShutterSpeed"|"ShutterAngle"}`; angle mode fills `shutterAngles` |
| `PUT /video/autoExposure` | `{"mode": "Continuous"|"Off", "type": null}` |
| `PUT /video/whiteBalance` | `{"whiteBalance": K, "tint": n}`; locks WB (no way back to continuous auto via API) |
| `PUT /video/whiteBalance/doAuto` | One-shot auto WB, no body |
| `/video/gain`, `/system/dynamicRange`, `/monitoring/*/brightness` | 501 |
| `PUT /system/format` | GET shape. **Change one thing per PUT** and wait for it (~0.3–1 s); PUTs changing several fields get misapplied. **While off-speed is on, `frameRate` is written into the off-speed rate** and the project rate can't change: turn off-speed off, set the frame rate, turn it back on with `offSpeedFrameRate`. Some off-speed rates are refused (400, e.g. 96 at 48 fps). **Resolution reverts after ~1 s** (the app rebuilds from its own resolution + Open Gate settings); use a preset instead (below) |
| `PUT /system/videoFormat` | Same revert for resolution |
| `/system/audioCodec` | Read only (AAC) |
| `GET/PUT/DELETE /presets/{name}.bmcpreset` | PUT saves the current state; GET returns JSON (`appsettings` incl. `resolutionSize` "WxH", `isOpenGate`) |
| `POST /presets` | Raw file, `Content-Type: application/octet-stream`, `Content-Disposition: attachment; filename="x.bmcpreset"`. Stored **without** the extension (`presetAdded`). Uploading does not apply it |
| `PUT /presets/active` | `{"preset": "<stored name>"}` applies all settings, including resolution |
| `PUT /monitoring/Device/{zebra,focusAssist,falseColor,frameGuide,frameGrids,safeArea,displayLUT}` | `{"enabled": bool}`; `cleanFeed` only on `HDMI` |
| `PUT /monitoring/focusAssist` | `{"mode": "Peak"|"ColoredLines", "color", "intensity": 1..100}` |
| `PUT /monitoring/zebra` | `{"highlight": {"level": 75..100 step 5, "enabled"}, "skinTone": null}` |
| `PUT /monitoring/frameGrids` | `{"frameGrids": ["Thirds","Crosshair","Dot","Horizon"]}` |
| `PUT /audio/channel/N/input` | Current input reads `"PhoneMicrophone"` but only `"Phone Microphone"` is accepted (the input list uses the spaced form) |
| `PUT /audio/channel/N/level` | Needs all three fields `{"gain","normalized","normalised"}` |
| `/audio/channel/N/{padding,lowCutFilter,phantomPower}` | 400 (no such hardware) |
| `PUT /transports/0/proxyRecording` | `{"enabled": bool}` |
| `/slates/...` | Readable (reel, scene, take, …); writes via `/slates/{target}/{category}/{property}`, untested |
| `GET /clips` | Per clip: `videoFormat.name` (file name without `.mp4`), `frameCount`, `videoFormat.frameRate`, `durationTimecode`, `filePath` (a `content://media/…` URI). The list lags behind deletions |
| `/camera/motionSensor/euler` | `roll ≈ atan2(gy, gx)` in radians |

## adb facts used by the helper
- Display rotation: `dumpsys display | grep -m1 -o 'orientation=[0-9]'`.
- Foreground app: `dumpsys activity activities | grep topResumedActivity=`; live events via
  `logcat -b events` tags `wm_on_top_resumed_gained_called` / `_lost_called`.
- Viewfinder: largest `android.view.SurfaceView` in `uiautomator dump` (~2.5 s; the camera preview never idles).
- Do Not Disturb: `cmd notification set_dnd priority|off`; state `settings get global zen_mode`.
- Clips: `/sdcard/DCIM/Blackmagic Camera`. Delete through the media store so the gallery stays in sync:
  `content delete --uri content://media/external/file/<id>` (no output; check the file is gone).
- Debian's adb (in the image) is built **without mDNS**: `adb mdns check/services` print nothing. `mdns.py` queries
  `_adb-tls-pairing._tcp.local` / `_adb-tls-connect._tcp.local` itself. Send on the LAN interface explicitly
  (`IP_MULTICAST_IF`), and ask both ways: a normal query from port 5353 (answers go to the group) and a direct
  ("QU") query from its own port; on a shared port 5353 a direct answer can go to avahi/mDNSResponder instead.
  Google's platform-tools adb has mDNS but may not be redistributed in the image (Android SDK license).
- Pairing name on the phone is `"<LOGNAME>@<HOSTNAME>"` of the adb server process (not the key comment); Android
  cuts it at the first space.
