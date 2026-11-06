"""The dashboard's HTTP server: static page, REST proxy, live view, events, setup, recordings."""
import hmac
import http.server
import json
import mimetypes
import queue
import re
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from . import auth, config, events, readiness
from .adb import phone_host
from .mirror import PRESETS, T_REPLACED, Mirror, take_over
from .phone import orientation, phone_status, viewport
from .recordings import clip_size, delete_clip, list_clips, read_range, thumbnail
from .recformat import set_color_space, set_resolution
from .screen import screen
from .settings import settings
from .setup import setup_action, setup_status


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/api/", "/clips/thumb", "/clips/file")):
            super().log_message(fmt, *args)

    def _send(self, code, body=b"", ctype="application/json", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- protection against other websites (DNS rebinding, cross-site requests) ----

    def _host_ok(self):
        """Without a login, only answer requests addressed to this computer by name (blocks DNS rebinding)."""
        if auth.PASSWORD:
            return True  # a rebound name never gets the session cookie
        host = (self.headers.get("Host") or "").strip().lower()
        host = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
        return host in ("localhost", "127.0.0.1", "::1") or host in config.ALLOWED_HOSTS

    def _cross_site(self):
        """True if the request was made by a page from another site."""
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return True
        origin = self.headers.get("Origin")
        if origin is None:
            return False  # not sent by a browser page (curl, ffmpeg, the health check)
        return origin == "null" or urllib.parse.urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower()

    def _reject(self, why):
        self._send(403, json.dumps({"error": why}).encode())

    def _authorized(self):
        if not auth.PASSWORD:
            return True
        if self.client_address[0] == "127.0.0.1" and hmac.compare_digest(self.headers.get("X-BCR-Internal", ""), auth.INTERNAL_TOKEN):
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == auth.COOKIE and auth.valid_session(v):
                return True
        return False

    def _login_page(self, error=""):
        self._send(401 if error else 200, auth.login_page(error).encode(), "text/html; charset=utf-8")

    def _login(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace")) if length else {}
        if not auth.PASSWORD or not hmac.compare_digest(form.get("password", [""])[0].encode(), auth.PASSWORD.encode()):
            time.sleep(1)  # slow down guessing
            return self._login_page("Wrong password")
        cookie = f"{auth.COOKIE}={auth.make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={auth.SESSION_DAYS * 86400}"
        self._send(303, b"", "text/plain", [("Set-Cookie", cookie + ("; Secure" if config.TLS else "")), ("Location", "/")])

    def _static(self, path):
        """Files under src/web/css, src/web/js and src/web/img."""
        f = (config.WEB / path.lstrip("/")).resolve()
        if config.WEB.resolve() not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _error(self, e, code=502):
        self._json({"error": str(e)}, code)

    def _proxy(self):
        try:
            host = phone_host()
        except Exception as e:
            return self._error(e)
        url = f"https://{host}:{config.API_PORT}/control/api/v1/{self.path[len('/api/'):]}"
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=data, method=self.command)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=config.PHONE_SSL, timeout=5) as r:
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._error(f"phone unreachable: {e}")

    def _mirror(self, q):
        max_size, bit_rate = PRESETS.get(q.get("quality", ["medium"])[0], PRESETS["medium"])
        source = q.get("audio", ["mic"])[0]
        if source not in ("mic", "mic-camcorder", "mic-unprocessed", "output", "playback"):
            source = "mic"
        m = Mirror(max_size, bit_rate, source)
        take_over(m)  # one stream at a time: a new viewer replaces the old one
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(b):
            self.wfile.write(b)
            self.wfile.flush()

        try:
            m.relay(write)
        except Exception as e:
            self.log_message("mirror ended: %s", e)
        finally:
            m.stop()
        if m.replaced:  # tell this page not to reconnect and fight the new viewer
            try:
                write(struct.pack(">BQI", T_REPLACED, 0, 0))
            except OSError:
                pass

    def _clip_file(self, q):
        name = q["name"][0]
        size = clip_size(name)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if m else 200)
        ctype = "video/quicktime" if name.lower().endswith(".mov") else "video/mp4"
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if "download" in q:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.end_headers()
        if self.command != "HEAD":
            try:
                read_range(name, start, length, self.wfile.write)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._host_ok():
            return self._reject("unknown host name; add it to BCR_ALLOWED_HOSTS or set BCR_PASSWORD")
        # Other sites may link to the dashboard, but not embed or fetch its content (live view, recordings, …).
        if self._cross_site() and self.headers.get("Sec-Fetch-Mode", "navigate") != "navigate":
            return self._reject("cross-site request")
        if u.path == "/login":
            return self._login_page() if auth.PASSWORD else self._send(303, b"", "text/plain", [("Location", "/")])
        if u.path == "/logout":
            return self._send(303, b"", "text/plain", [("Set-Cookie", f"{auth.COOKIE}=; Path=/; Max-Age=0"), ("Location", "/login")])
        if u.path.startswith(("/css/", "/js/", "/img/")):
            return self._static(u.path)
        if not self._authorized():
            if u.path in ("/", "/index.html"):
                return self._send(303, b"", "text/plain", [("Location", "/login")])
            return self._error("sign in first", 401)
        try:
            if u.path.startswith("/api/"):
                return self._proxy()
            if u.path in ("/", "/index.html"):
                return self._send(200, (config.WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/mirror":
                return self._mirror(q)
            if u.path == "/setup":
                return self._json(readiness.readiness.snapshot() or setup_status())
            if u.path == "/events":
                return self._events()
            if u.path == "/phone":
                return self._json(phone_status())
            if u.path == "/automation":
                return self._json(settings.toggles())
            if u.path == "/orientation":
                return self._json(orientation())
            if u.path == "/viewport":
                return self._json(viewport())
            if u.path == "/clips":
                return self._json({"clips": list_clips()})
            if u.path == "/clips/file":
                return self._clip_file(q)
            if u.path == "/clips/thumb":
                body = thumbnail(q["name"][0], q.get("size", [""])[0], q.get("mtime", [""])[0])
                return self._send(200, body, "image/jpeg", [("Cache-Control", "max-age=86400")])
        except (KeyError, ValueError) as e:
            return self._error(e, 400)
        except FileNotFoundError as e:
            return self._error(f"not found: {e}", 404)
        except Exception as e:
            return self._error(e)
        self._send(404, b"not found", "text/plain")

    def _events(self):
        """One stream per page (Server-Sent Events) for everything live: the setup state ("setup" events) and the
        camera's properties ("snapshot", then one message per change). Browsers allow only ~6 connections per
        server across all tabs, so a page keeps just this and the live view open."""
        q = queue.Queue(maxsize=2000)
        q, snapshot = events.hub.subscribe(q)
        _, state = readiness.readiness.subscribe(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if state:
                self.wfile.write(f"event: setup\ndata: {json.dumps(state)}\n\n".encode())
            snapshot["@connected"] = events.hub.connected
            self.wfile.write(f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    prop, value = q.get(timeout=15)
                    if prop == "@setup":
                        self.wfile.write(f"event: setup\ndata: {json.dumps(value)}\n\n".encode())
                    else:
                        self.wfile.write(f"data: {json.dumps({'p': prop, 'v': value})}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            events.hub.unsubscribe(q)
            readiness.readiness.unsubscribe(q)

    def _local_write(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if u.path == "/clips/file" and self.command == "DELETE":
                return self._json(delete_clip(urllib.parse.parse_qs(u.query)["name"][0]))
            if u.path == "/format/resolution" and self.command == "POST":
                return self._json(set_resolution(body["width"], body["height"], body.get("openGate", False)))
            if u.path == "/format/colorspace" and self.command == "POST":
                return self._json(set_color_space(str(body.get("colorSpace", ""))))
            if u.path == "/screen" and self.command == "POST":
                screen.set(bool(body.get("on")))
                return self._json({"screenOff": screen.off})
            if u.path == "/automation" and self.command in ("PUT", "POST"):
                return self._json(settings.update({k: v for k, v in body.items() if k != "lastAddress"}, toggles_only=True))
            if u.path.startswith("/setup/") and self.command == "POST":
                result = setup_action(u.path[len("/setup/"):], body)
                if readiness.readiness and not u.path.endswith("qr-status"):
                    readiness.readiness.recheck.set()  # re-run the checks right away
                return self._json(result)
        except ValueError as e:
            return self._error(e, 400)
        except Exception as e:
            return self._error(e, 502)
        self._send(404, b"not found", "text/plain")

    def _write(self):
        if not self._host_ok():
            return self._reject("unknown host name; add it to BCR_ALLOWED_HOSTS or set BCR_PASSWORD")
        if self._cross_site():
            return self._reject("cross-site request")
        length = int(self.headers.get("Content-Length") or 0)
        if self.path != "/login" and length and not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._reject("send JSON")
        if self.path == "/login" and self.command == "POST":
            return self._login()
        if not self._authorized():
            return self._error("sign in first", 401)
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._local_write()

    do_HEAD = do_GET
    do_PUT = do_POST = do_DELETE = _write

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def finish_request(self, request, client_address):
        if not config.TLS:
            return super().finish_request(request, client_address)
        try:
            request.settimeout(10)
            conn = config.TLS.wrap_socket(request, server_side=True)
            conn.settimeout(None)
        except (ssl.SSLError, OSError):
            return  # e.g. a browser that hasn't accepted the self-signed certificate yet
        try:
            super().finish_request(conn, client_address)
        finally:
            conn.close()
