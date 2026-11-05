"""Paths, constants and the runtime options set at startup (see server.py)."""
import os
import pathlib
import ssl

SRC = pathlib.Path(__file__).resolve().parent.parent  # src/
WEB = SRC / "web"                                     # the dashboard's HTML, CSS and JavaScript
DATA = pathlib.Path(os.environ.get("BCR_DATA") or SRC.parent / "data")  # settings, cache, login secret, certificate
CACHE = DATA / "cache"
SETTINGS_FILE = DATA / "settings.json"

APP_PACKAGE = "com.blackmagicdesign.android.blackmagiccam"
API_PORT = 4444                          # Blackmagic Camera's HTTPS API (Settings → Network Access)
CLIPS_DIR = "/sdcard/DCIM/Blackmagic Camera"

# The phone's API uses a self-signed certificate, so don't verify it.
PHONE_SSL = ssl.create_default_context()
PHONE_SSL.check_hostname = False
PHONE_SSL.verify_mode = ssl.CERT_NONE

# Set by server.py at startup.
PHONE = ""   # phone IP override (BCR_PHONE); empty = ask the phone over adb
PORT = 8080  # dashboard port
TLS = None   # ssl.SSLContext when the dashboard serves HTTPS

# Extra host names the dashboard answers to without a password (besides localhost); see webserver._host_ok.
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("BCR_ALLOWED_HOSTS", "").split(",") if h.strip()}
