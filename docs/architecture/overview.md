# Architecture

## Pieces

```
 Browser (any device)                Helper (Python, Docker)                     Android phone
 ───────────────────                ───────────────────────                     ─────────────
 src/web page  ── /api/* ─────────▶  REST proxy  ── HTTPS :4444 ─────────────▶  Blackmagic Camera app
               ◀─ /events (SSE) ───  EventHub    ◀─ event websocket :4444 ────   (HTTP API, self-signed)
               ◀─ /mirror (binary) ─ Mirror      ◀─ adb forward ◀─ scrcpy-server (screen video + mic)
                                     ScreenPower ── adb forward ── scrcpy-server (display power only)
                                     Readiness   ── adb track-devices, logcat, direct API probe
                                     setup / phone / recordings / automation ── adb shell commands
```

- The **helper** (`src/blackmagic_cam_remote/`) is the only thing that talks to the phone. The browser never
  reaches the phone directly (the phone's certificate is self-signed and browsers would block it).
- **adb** runs inside the container over Wireless debugging. Docker Desktop (macOS/Windows) has no USB and no
  mDNS inside containers; Linux with host networking has both.
- **scrcpy-server** (from scrcpy, pushed to the phone on each start) provides the live view and microphone, and,
  in a separate control-only session, turns the display panel off/on.

## Modules (`src/blackmagic_cam_remote/`)

| Module | Responsibility |
|---|---|
| `config.py` | Paths, constants, runtime options set by `server.py` (`PHONE`, `PORT`, `TLS`, `ALLOWED_HOSTS`) |
| `settings.py` | Persisted settings (`data/settings.json`): automation toggles, last phone address/IP |
| `adb.py` | Finding/reconnecting the phone (`adb_serial`), `adb()` wrapper, phone IP, scrcpy-server location, pairing identity |
| `util.py` | `recv_exact`, `free_port` |
| `readiness.py` | Live setup state: device tracking, foreground app (logcat events), API probe, periodic full checks; pushed via `/events` |
| `setup.py` | The checks (`setup_status`), QR/code pairing, connect, launch app |
| `webserver.py` | HTTP server and routes, host/origin checks, login, static files, SSE, live-view relay, clip ranges |
| `events.py` | One websocket to the app's event API; caches all properties and fans out changes |
| `mirror.py` | Live view session: scrcpy-server video (H.264) + raw PCM audio, framed for the page; one stream at a time |
| `screen.py` | Display power via scrcpy control channel; back-gesture watcher (`getevent`) |
| `automation.py` | On record start/stop: screen off, Do Not Disturb |
| `phone.py` | Phone health (battery, temperature, Wi-Fi, storage), rotation, viewfinder position (UI dump) |
| `recordings.py` | Clip list (folder + durations from the app's `/clips`), byte-range reads, ffmpeg thumbnails (cached in `data/cache`), delete via the media store |
| `recformat.py` | Resolution changes via a temporary preset (the API alone reverts them) |
| `auth.py`, `tls.py` | Optional password login (signed cookie), optional HTTPS (self-signed or own cert) |

## Page (`src/web/js/`, loaded in this order)

| File | Responsibility |
|---|---|
| `core.js` | `$`, toasts, `api()`/`send()` (REST via `/api`), small UI helpers, `dashboardOn` |
| `camera.js` | Property cache `P`, `render()`, main controls (record, exposure, WB, focus, lens, zoom) |
| `panels.js` | Recording format, monitoring overlays, presets, phone health/automation, quick settings, audio |
| `liveview.js` | `/mirror` reader, WebCodecs decoding, crop/rotation drawing, viewport prediction, audio playback |
| `mse.js` | Fallback for plain `http://` pages: H.264 → fragmented MP4 → Media Source Extensions |
| `recordings.js` | Clip list, player, downloads |
| `setup.js` | The `/events` stream, setup screen, start/stop of the dashboard |

## Data flows

### Setup gating
`readiness.py` combines live values (adb device list from `host:track-devices-l`, foreground app from
`logcat -b events` `wm_on_top_resumed_*`, direct API probe every 0.3–2 s) with the slower `setup_status()` checks
(every 10 s and after any change). The combined state is pushed as `event: setup` on `/events`. The page shows the
dashboard only while `ready` is true; otherwise it shows the setup screen and stops the live view.

### Camera state
`events.py` subscribes to every property from `/control/api/v1/event/list` over the app's websocket. `/events`
sends a `snapshot` and then one message per change. The page keeps them in `P` and re-renders. Writes go through
`/api/*` (REST). After a write the page re-reads that property only if no pushed update arrived in 700 ms (a late
read could overwrite a newer pushed value).

### Live view
`/mirror?quality=…` starts scrcpy-server (`video_codec=h264`, `audio_codec=raw`, `max_fps=24`) and relays frames
as `type u8, pts u64, size u32, payload` (0 session size, 1 SPS/PPS, 2 key frame, 3 delta, 4 PCM, 5 replaced).
The page crops to the viewfinder (`/viewport`, a UI dump, ~2.5 s) and rotates. After a resolution change it predicts
the new viewfinder area and confirms it once the app's layout matches.

### Resolution change
`POST /format/resolution`: save a temporary preset `bcr-resolution`, change `appsettings.resolutionSize` and
`isOpenGate`, upload it, load it (stored name has no extension), delete it.

### Screen off while recording
`automation.py` reacts to `/transports/0/record` changes: `screen.set(False)` (scrcpy `SET_DISPLAY_POWER`) and
`cmd notification set_dnd priority`; both are undone when recording stops. A back gesture (edge swipe seen by
`getevent`) turns the screen on. If the control session dies, scrcpy's cleanup restores the display.

### QR pairing
The QR encodes `WIFI:T:ADB;S:<name>;P:<password>;;`. The phone then opens a pairing port. The helper finds it via
mDNS (`_adb-tls-pairing._tcp` named `<name>`) or, without mDNS, by scanning the phone's IP (32768–49999 first,
400 probes at a time) and trying `adb pair` on each newly found port. The scan stops when the attempt ends.

## Security model
- Default compose publishes on `127.0.0.1` only, no login. Without a password the server answers only to
  `localhost`/`BCR_ALLOWED_HOSTS` (DNS rebinding), rejects cross-site writes and embeds (`Origin`,
  `Sec-Fetch-Site`), and requires JSON for writes.
- `compose.server.yaml` requires `BCR_PASSWORD` and serves HTTPS.
- adb pairing keys live in the `bcr-data` volume; anyone with them and network access to the phone can control it
  while Wireless debugging is on.
