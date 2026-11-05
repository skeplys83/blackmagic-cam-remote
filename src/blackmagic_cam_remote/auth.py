"""Optional password login (BCR_PASSWORD): signed session cookies."""
import hashlib
import hmac
import html
import os
import secrets
import time

from . import config


PASSWORD = ""  # set by server.py from BCR_PASSWORD

INTERNAL_TOKEN = secrets.token_urlsafe(24)  # lets the helper's own ffmpeg read clips

SESSION_DAYS = 30

_secret = None

def session_secret():
    """Random key (kept in the data dir) that signs login cookies; changing the password logs everyone out."""
    global _secret
    if _secret is None:
        f = config.DATA / "secret"
        try:
            _secret = f.read_bytes()
        except OSError:
            config.DATA.mkdir(parents=True, exist_ok=True)
            _secret = secrets.token_bytes(32)
            f.write_bytes(_secret)
            os.chmod(f, 0o600)
    return _secret + hashlib.sha256(PASSWORD.encode()).digest()

def make_session():
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return exp + "." + hmac.new(session_secret(), exp.encode(), "sha256").hexdigest()

def valid_session(value):
    exp, _, sig = (value or "").partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, hmac.new(session_secret(), exp.encode(), "sha256").hexdigest())

COOKIE = "bcr_session"


def login_page(error=""):
    msg = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return (config.WEB / "login.html").read_text().replace("{msg}", msg)
