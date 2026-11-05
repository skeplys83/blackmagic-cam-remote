#!/usr/bin/env python3
"""Blackmagic Cam Remote: start the dashboard.

  python3 src/server.py [--phone IP] [--host 127.0.0.1] [--port 8080]

Environment:
  BCR_PHONE      the phone's Wi-Fi IP (default: asked over adb)
  BCR_HOST       address to listen on (default 127.0.0.1)
  BCR_PORT       dashboard port (default 8080)
  BCR_DATA       settings, cache, login secret and certificate (default ./data)
  BCR_PASSWORD   require a login
  BCR_TLS=1      serve HTTPS (self-signed unless BCR_TLS_CERT and BCR_TLS_KEY are set)
  BCR_TLS_HOSTS  extra names/IPs for the self-signed certificate, comma-separated
  SCRCPY_SERVER, SCRCPY_VERSION   location and version of scrcpy-server (found automatically when installed)

The code lives in src/blackmagic_cam_remote/ (one module per concern) and src/web/ (the page).
"""
import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from blackmagic_cam_remote import auth, config, events  # noqa: E402
from blackmagic_cam_remote.adb import adb  # noqa: E402
from blackmagic_cam_remote.tls import tls_context  # noqa: E402
from blackmagic_cam_remote.webserver import Handler, Server  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Blackmagic Cam Remote dashboard")
    ap.add_argument("--phone", default=os.environ.get("BCR_PHONE", ""), help="phone IP (default: ask the phone over adb)")
    ap.add_argument("--host", default=os.environ.get("BCR_HOST", "127.0.0.1"), help="address to listen on")
    ap.add_argument("--port", type=int, default=int(os.environ.get("BCR_PORT", "8080")))
    args = ap.parse_args()

    config.PHONE, config.PORT = args.phone, args.port
    auth.PASSWORD = os.environ.get("BCR_PASSWORD", "")
    if os.environ.get("BCR_TLS", "").lower() in ("1", "true", "yes") or os.environ.get("BCR_TLS_CERT"):
        config.TLS = tls_context()

    try:  # leftovers from a previous run (the phone's display comes back on when they exit)
        adb("shell", "pkill -f com.genymobile.scrcpy.Server", timeout=10)
    except Exception:
        pass
    events.hub = events.EventHub()

    srv = Server((args.host, args.port), Handler)
    print(f"Blackmagic Cam Remote listening on {'https' if config.TLS else 'http'}://{args.host}:{args.port}"
          f"{' (login required)' if auth.PASSWORD else ''}", flush=True)
    if not auth.PASSWORD and args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("BCR_DOCKER_LOCAL"):
        print("WARNING: listening on the network without a password; set BCR_PASSWORD.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
