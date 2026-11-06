# The helper (Python)

- Standard library only; Python 3.9+. One module per concern (table in `docs/architecture/overview.md`).
- Every phone request goes through here; the browser never talks to the phone directly.
- Before using an API endpoint, check `docs/api/blackmagic-camera-android.md`, then verify on a phone
  (read, write back unchanged, then change and restore).
- adb calls are slow (100 ms–2.5 s): never run them per request in a tight loop; cache or push via `readiness.py`.
- Python changes need an image rebuild to test in Docker (see `docs/development/workflow.md`). Run pyflakes.
