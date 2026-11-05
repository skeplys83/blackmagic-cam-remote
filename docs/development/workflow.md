# Development workflow

## Run
- **Docker from source:** `./start.sh --build` → http://localhost:8080. Python changes need
  `docker compose -f compose.yaml -f compose.build.yaml up -d --build` (plain `./start.sh` uses the published image).
  Page-only changes: `docker cp src/web/. blackmagic-cam-remote:/app/web/` and reload the tab (rebuild before committing).
- **Without Docker:** `python3 src/server.py --port 8091` with adb, scrcpy and (optionally) ffmpeg/qrencode installed.
  Data goes to `./data` (or `BCR_DATA`). This uses the host's own adb key and server.
- **Home server mode locally:** `BCR_PASSWORD=x BCR_TLS=1 BCR_PORT=8443` (self-signed cert in `data/tls`).

## Phone access while developing
- The container's adb: `docker exec blackmagic-cam-remote adb …`. It is paired as `blackmagic-cam-remote@<computer>`.
- A host adb can be paired too (separate key). Both can be connected at the same time.
- The app's API is reachable directly: `curl -sk https://<phone-ip>:4444/control/api/v1/system`.
- Useful: `adb shell dumpsys activity activities | grep topResumedActivity=`, `adb exec-out screencap -p > s.png`,
  `adb exec-out uiautomator dump /dev/tty`.

## Checks before committing
```sh
# Python: unused imports / undefined names
python3 -m venv /tmp/v && /tmp/v/bin/pip -q install pyflakes && /tmp/v/bin/pyflakes src/
# JavaScript: parse the files in load order (macOS JavaScriptCore; any JS engine works)
cd src/web/js && cat core.js camera.js panels.js liveview.js mse.js recordings.js setup.js > /tmp/all.js
/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc -e "new Function(read('/tmp/all.js'))"
# No personal data in what will be published
git grep -n -i -E '<your phone ip>|<adb serial>|<wifi name>|@gmail|/Users/' -- . ':!LICENSE'
```

## Testing changes on a phone
- Read a value, write it back unchanged, then test the real change and **restore it**.
- For page changes, test in a browser tab and check `read_console_messages`/DevTools for errors.
- Live view tests: only one stream at a time; opening a test tab takes it over from other tabs.
- Things that need a human: scanning a QR code, back gesture, real recordings (clean up test clips).

## Gotchas found the hard way
- Too many long-lived requests exhaust the browser's ~6 connections per server → scripts load after seconds.
- `uiautomator dump` is slow (~2.5 s); don't call it on a short interval.
- scrcpy-server deletes its jar after start (cleanup) → push it on every start.
- Docker Desktop's network layer drops floods of connections; keep port scans at ≤400 concurrent probes.
- adb's pairing name comes from `LOGNAME@HOSTNAME`; set before the adb server starts.

## Commits and releases
- Small, focused commits with a summary line and a short "why". Push to `main` on GitHub.
- Every push to `main` builds and publishes `ghcr.io/skeplys83/blackmagic-cam-remote:latest` (amd64 + arm64)
  via `.github/workflows/docker-image.yml`. A tag `vX.Y.Z` publishes `:X.Y.Z`, `:X.Y`, `:X`.
- `scrcpy-server` is downloaded and checksum-verified at build. Bump `SCRCPY_VERSION`/`SCRCPY_SHA256` together.
