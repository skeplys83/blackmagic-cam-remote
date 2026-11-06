# The page (plain JavaScript and CSS)

- No build step, no libraries. Scripts load in order core → camera → panels → liveview → mse → recordings → setup
  and share globals (`$`, `P`, `state`, `render`).
- `render()` runs on every pushed camera event (several per second). Don't rebuild controls there unconditionally:
  use `fillSelect` (rebuilds only when the list changes) and don't overwrite a control the user is using or one
  with a change in flight (`document.activeElement`, `dragging`, `fmtBusy`).
- Writes: `send()` for single values; recording-format changes go through `putFormat()` (one field per request,
  each confirmed; see the API doc).
- Use the shared styles: 44 px controls, tokens and layout in `docs/design/ui.md`.
- Try changes with `docker cp src/web/. blackmagic-cam-remote:/app/web/` and a reload (this copies this file too; harmless); run the JS parse check.
