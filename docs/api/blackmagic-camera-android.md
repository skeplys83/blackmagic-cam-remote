# Blackmagic Camera (Android) API: what actually works

Tested with Blackmagic Camera 3.5.1 on a Pixel 10 (Android 17) only. Base URL `https://<phone>:4444/control/api/v1`,
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
- **Not every change is pushed.** Switching lens on the phone pushes `/lens/cameras/active`, but `/lens/cameras` keeps
  the old `isActive` flags. The helper therefore also re-reads every property over REST in a loop (see the
  architecture doc); use the most specific property (`/lens/cameras/active`, not the list's flags).
- About 25 subscribable properties can't be read over REST on Android (501/404/400: gain, ND, color correction,
  playback, XLR audio options); they come only from events, if at all.
- The phone's FPS readout with off-speed on is "off-speed/project" (e.g. `15/120` = project 120, off-speed 15;
  in presets `selectedPFR` / `selectedOFR`).
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
| `PUT /system/format` | GET shape. **Change one thing per PUT** and wait for it (~0.3–1 s); PUTs changing several fields get misapplied. **While off-speed is on, `frameRate` is written into the off-speed rate** and the project rate can't change: turn off-speed off, set the frame rate, turn it back on with `offSpeedFrameRate`. Off-speed limits depend on lens and resolution: `min/maxOffSpeedFrameRate` in `/system/format` (live) and per entry in `/system/supportedFormats` (Pixel 10: 1x/0.6x reach 240 at 1080p/720p, 4.3x and front 60, 4K 30–60). Any whole rate up to 60 works; above 60 only the format's high-speed `frameRates` (120, 240), others → 400. **Resolution reverts after ~1 s** (the app rebuilds from its own resolution + Open Gate settings); use a preset instead (below) |
| `PUT /system/videoFormat` | Same revert for resolution |
| Color space | **Not in the REST API.** Presets carry it as `appsettings.colorSpace`; loading a preset applies it (see below) |
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

## Color space
- Preset values (`appsettings.colorSpace`): 0 Rec.709, 1 BT.2020 HLG10, 2 P3 D65, 3 Blackmagic Log HDR, 14 sRGB,
  15 P3 D60, 18 HDR10+, 19 Dolby Vision PQ, 20 Vendor Log HDR, 21 Dolby Vision HLG (HDR10 has none that loads back).
  `colorSpaceTag` is the container tag (0 Rec.709, 1 Rec.2020); leave it.
- **Only set profiles the phone supports.** Unsupported ones are usually reset to Rec.709, but Vendor Log HDR (20) on
  a Pixel 10 at 1080p/120 crashed the app's video encoder in a loop; restarting the app then reset **all** its
  settings (including *Enable HTTP Server*). Recovery: turn the HTTP server back on in the app, load a saved preset.
- What the phone supports: `dumpsys media.camera`, `android.request.availableDynamicRangeProfilesMap` per camera
  (triples of profile, compatible profiles, latency; profile 2 = HLG10, 4 = HDR10, 8 = HDR10+), plus standard (1).
  The app's own menu matches it (Pixel 10: Rec.709 and BT2020 HLG10, with H.265).
- Current value: the active stream's `Dynamic Range Profile: 0x1` (standard) / `0x2` (HLG10) in the same dump.

## Other phones
Only a Pixel 10 has been tested. The page takes device-specific values from the app wherever it reports them:
lenses (`/lens/cameras`), formats and frame rates (`/system/supportedFormats`), off-speed limits per lens and
resolution (`/system/format`), ISO and shutter lists (`/video/supported*`), audio inputs. Assumptions that may not
hold elsewhere: which off-speed rates between the limits are accepted, the viewfinder being the largest
`SurfaceView`, and the clip folder path. When something is refused, the page shows the app's error.

## Not in the REST API
The app's own remote protocol (port 2222, advertised as `_bmd-cam-control._tcp`; protobuf over a WebRTC data
channel, signaling over a websocket) also has the stabilization mode and exposure compensation. Not used here: it
would need a WebRTC stack (no standard-library option).

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
- The scrcpy-server microphone capture (`audio_source=mic`) works while Blackmagic Camera records; Android lets both
  read the mic.
- Pairing name on the phone is `"<LOGNAME>@<HOSTNAME>"` of the adb server process (not the key comment); Android
  cuts it at the first space.
