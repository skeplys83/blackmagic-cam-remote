# CLAUDE.md

Guidance for working on Blackmagic Cam Remote, a browser dashboard that remote-controls the Blackmagic Camera
Android app (live view, camera controls, recordings, phone health) through the app's HTTP API and adb.

## Start here

| Topic | Read |
|---|---|
| How the pieces fit together, data flows, modules | [docs/architecture/overview.md](docs/architecture/overview.md) |
| Blackmagic Camera API on Android: what works, what doesn't, quirks | [docs/api/blackmagic-camera-android.md](docs/api/blackmagic-camera-android.md) |
| UI layout, design tokens, control sizes, behaviour rules | [docs/design/ui.md](docs/design/ui.md) |
| Running, testing, debugging, releasing | [docs/development/workflow.md](docs/development/workflow.md) |

`CLAUDE.md` and `docs/` are excluded from the Docker image (`.dockerignore`). Folder-level `CLAUDE.md` files in
`src/blackmagic_cam_remote/` and `src/web/` hold the rules for that part. `CLAUDE.local.md` (gitignored, may not
exist) holds the maintainer's personal setup and preferences; never copy its contents into tracked files.

## Layout

```
src/server.py                 entry point only (options, wiring, start)
src/blackmagic_cam_remote/    the helper: one module per concern, Python standard library only
src/web/                      the page: index.html, login.html, css/app.css, js/*.js (no build step), img/
Dockerfile                    the image (built by .github/workflows/docker-image.yml → ghcr.io)
compose.yaml                  local use with the published image (127.0.0.1:8080, no password)
compose.server.yaml           home server (host networking, password required, HTTPS on 8443)
compose.build.yaml            override: build from this source tree instead of pulling
start.sh / start.command      launcher: checks Docker, pulls (or --build), starts, opens the browser
```

## Ground rules

- **No dependencies.** The helper uses only the Python standard library; the page is plain JavaScript and CSS
  without a build step. External tools are adb, scrcpy-server and (optional) ffmpeg and qrencode, all in the image.
- **Verify against the phone, not the spec.** The Android app deviates from Blackmagic's published API in many
  places (see the API doc). Before relying on a request format, read the value, write it back unchanged, and check.
  Restore any setting you change while testing.
- **Writes use the GET shape.** The app decodes request bodies strictly into its models; send what GET returns,
  changed only where needed, unless the API doc says otherwise for that endpoint.
- **Keep it touch friendly.** Every control is 44 px tall (see the design doc); new controls must use the shared styles.
- **Never publish personal data.** No IPs, serials, network names or emails in code, docs or screenshots. Run the
  scan in the workflow doc before every push.
- **One live stream per page.** Browsers allow ~6 connections per server across all tabs; a page uses only
  `/events` (SSE) and `/mirror`. Don't add more long-lived requests.

## Common commands

```sh
./start.sh --build                           # build from source + start, open http://localhost:8080
docker compose -f compose.yaml -f compose.build.yaml up -d --build   # rebuild after Python changes
docker cp src/web/. blackmagic-cam-remote:/app/web/   # try page changes without a rebuild (reload the tab)
python3 src/server.py --port 8091            # run without Docker (needs adb + scrcpy on the host)
docker exec blackmagic-cam-remote adb devices          # the container's adb
```

Checks before committing: `pyflakes src/` (any venv) and a JavaScript parse check of `src/web/js/*.js` concatenated
in load order (see the workflow doc).
