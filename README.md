# Pixel Cam Remote

A browser dashboard for remote-controlling the **Blackmagic Camera** app on an Android phone from your computer: live view with overlays and sound, camera controls, recording format, monitoring overlays, presets, recordings, and phone health.

> Not affiliated with or endorsed by Blackmagic Design. "Blackmagic Camera" is their app; this project only talks to the HTTP API the app exposes on your local network, plus Android's standard debugging tools.

## Features

- **Live view**: an exact mirror of the phone's viewfinder (grid, focus peaking, zebra and other overlays included) with the phone's microphone sound. Viewport-only or full screen, auto/fixed rotation, four quality presets (capped at 24 fps).
- **Camera controls**: record, ISO, shutter, auto exposure, white balance and tint, focus (auto/manual/one-shot), lens selection, zoom.
- **Recording format**: resolution, frame rate, codec (H.265 / H.264 / AV1), off-speed.
- **Monitoring overlays**: focus assist, zebra, false color, frame guides, grids, safe area, display LUT and clean feed, separately for the phone screen and HDMI out, plus their settings.
- **Presets**: load, save, update, delete.
- **Recordings**: list with thumbnails, playback (seekable) and download.
- **Phone health**: temperature, thermal throttling state, Wi-Fi signal, free storage, battery.
- **While recording** (optional): turn the phone's screen off (the camera keeps recording; a back gesture on the phone turns it on again) and enable Do Not Disturb.
- Live updates pushed from the camera (no polling).

## Requirements

- An Android 13+ phone with [Blackmagic Camera](https://play.google.com/store/apps/details?id=com.blackmagicdesign.android.blackmagiccam). Developed and tested on a Pixel 10 with Blackmagic Camera 3.5.1.
- A computer with [Docker](https://www.docker.com/products/docker-desktop/) (macOS, Windows or Linux), on the **same Wi-Fi network** as the phone.
- A browser with WebCodecs for the live view: Chrome, Edge, or Safari 17+.

## Quick start

```sh
git clone https://github.com/skeplys83/pixel-cam-remote.git
cd pixel-cam-remote
./start.sh            # macOS: or double-click start.command
```

On Windows: `docker compose up -d --build`, then open <http://localhost:8080>.

The dashboard opens on a **setup screen** with three checks. It re-checks every couple of seconds and starts the dashboard once all of them pass:

1. **Computer**: Python, adb, scrcpy-server and ffmpeg. All bundled in the Docker image, so this is green automatically.
2. **Wireless debugging**: the phone connects over Wi-Fi, so it can be anywhere on your network (on a rig, a tripod, a gimbal).
   - On the phone, turn on Developer options (Settings → About phone → tap *Build number* 7 times).
   - Open Settings → System → Developer options → **Wireless debugging** and turn it on.
   - **Pair with QR code**: click *Show pairing QR code* on the setup screen, then on the phone tap *Pair device with QR code* and scan it. Pairing and connecting happen automatically.
   - **Or pair with a code**: tap *Pair device with pairing code* and enter the shown *IP address & port* and *code* on the setup screen.
   - If a connection is needed manually, enter the *IP address & port* shown at the top of the Wireless debugging screen into **Connect**. This port changes whenever Wireless debugging restarts; the last one used is tried automatically.
3. **Blackmagic Camera**
   - Install the app and open it (the setup screen can open it for you).
   - In the app: Settings → **Network Access** → turn on **Enable HTTP Server**.
   - The check confirms the server is listening on port 4444, that it's Blackmagic Camera listening (and not another app holding that port), and that your computer can reach it over Wi-Fi.

Pairing keys, settings and thumbnails live in the `pcr-data` Docker volume, so you only pair once.

### QR pairing and where it works

QR pairing needs local-network discovery (mDNS) to find the phone after it scans the code:

| Where the dashboard runs | QR pairing | Code pairing |
|---|---|---|
| Docker on **Linux** with `network_mode: host` (e.g. a home server) | ✓ | ✓ |
| **Without Docker** (any OS) | ✓ | ✓ |
| Docker Desktop on **macOS / Windows** | ✗ (no mDNS inside containers) | ✓ |

For Linux, uncomment `network_mode: host` in [`compose.yaml`](compose.yaml) and remove the `ports` section. That also enables automatic reconnects when Wireless debugging changes its port. USB debugging works there too (uncomment `devices`), but this project is built around wireless use.

### Without Docker

You need Python 3.9+, [adb](https://developer.android.com/tools/releases/platform-tools), [scrcpy](https://github.com/Genymobile/scrcpy) (for its `scrcpy-server` file), and optionally ffmpeg (thumbnails) and qrencode (QR pairing). Then:

```sh
python3 server.py     # then open http://localhost:8080
```

`scrcpy-server` is found automatically in Homebrew and common Linux install locations. Otherwise set `SCRCPY_SERVER=/path/to/scrcpy-server` and `SCRCPY_VERSION=<its version>`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PCR_PHONE` | auto (asked over adb) | The phone's Wi-Fi IP, if detection fails |
| `PCR_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Address the dashboard listens on |
| `PCR_PORT` | `8080` | Dashboard port |
| `PCR_DATA` | project folder (`/data` in Docker) | Settings and thumbnail cache |

In Docker, the dashboard is published on `127.0.0.1:8080` only, so other devices on the network can't reach it. With `network_mode: host` it listens on the host's port 8080 for the whole network. There is no login yet, so only do that on a network you trust.

## How it works

- **Camera API**: Blackmagic Camera's HTTPS REST API on port 4444 (`/control/api/v1`), and its event WebSocket for live state. The helper proxies both so the browser doesn't have to deal with the phone's self-signed certificate. On Android, the app expects request bodies shaped exactly like its GET responses.
- **Live view, sound, screen off**: [scrcpy](https://github.com/Genymobile/scrcpy)'s `scrcpy-server`, started over adb. Video is decoded in the browser with WebCodecs; raw PCM audio plays through Web Audio. Screen off uses scrcpy's display-power control, so only the panel turns off and the camera keeps running.
- **Recordings, phone health, Do Not Disturb**: adb (`dumpsys`, `cmd wifi`, `cmd notification set_dnd`, …).

## Limitations

- No live video or sound over the REST API alone; adb must be connected.
- With the screen off, touches still reach the phone, and the back gesture that turns the screen on is also delivered to the app.
- Screen-off and Do-Not-Disturb automation only run while the helper is running.
- The phone's screen rotation lock decides the mirrored orientation; use the rotation buttons to override it.
- Display brightness and dynamic range aren't implemented by the Android app's API.

## Troubleshooting

- **"HTTP server on" fails although it's enabled**: another app may hold port 4444. The check names it; close or uninstall it, then toggle *Enable HTTP Server* off and on.
- **Phone disconnects after a while**: Wireless debugging got a new port (after a reboot or toggle). With mDNS it reconnects by itself; otherwise enter the new *IP address & port* in **Connect**.
- **QR code scanned, but nothing happens**: this setup can't see mDNS (Docker Desktop on macOS/Windows). Pair with a code instead.
- **"Reachable over the network" fails**: the phone and computer are on different networks, or the Wi-Fi isolates clients (common on guest networks).
- **Live view says another tab took it**: only one live view runs at a time; click *Show it here*.

## Credits

- Live view, sound and screen control use [scrcpy](https://github.com/Genymobile/scrcpy) by Genymobile and contributors (Apache-2.0). The Docker image downloads `scrcpy-server` from its official releases and verifies the checksum.
- adb is part of the Android SDK Platform Tools.

## License

[MIT](LICENSE)
