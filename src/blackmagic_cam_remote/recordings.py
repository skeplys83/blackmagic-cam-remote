"""Recordings on the phone: listing, byte-range reads for playback/download, cached thumbnails."""
import hashlib
import json
import re
import shlex
import subprocess
import threading
import urllib.parse
import urllib.request

from . import auth, config
from .adb import adb, adb_serial, phone_host


def safe_name(name):
    """Clip names end up in adb shell commands: allow only plain file names."""
    if not name or name.startswith(".") or not re.fullmatch(r"[\w .()\-]+", name):
        raise ValueError("bad clip name")
    return name

def clip_path(name):
    return f"{config.CLIPS_DIR}/{safe_name(name)}"

def _app_clips():
    """The app's own clip list (/clips): duration and media-store URI per clip, keyed by file name without extension."""
    try:
        url = f"https://{phone_host()}:{config.API_PORT}/control/api/v1/clips"
        with urllib.request.urlopen(url, context=config.PHONE_SSL, timeout=5) as r:
            clips = json.load(r).get("clips", [])
    except Exception:
        return {}
    info = {}
    for c in clips:
        fmt = c.get("videoFormat") or {}
        try:
            seconds = c["frameCount"] / float(fmt["frameRate"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            seconds = None
        info[fmt.get("name")] = {"duration": seconds, "uri": c.get("filePath")}
    return info


def list_clips():
    app = _app_clips()  # the folder is the source of truth; the app adds durations
    cmd = f"cd {shlex.quote(config.CLIPS_DIR)} 2>/dev/null && for f in *; do [ -f \"$f\" ] && stat -c '%s|%Y|%n' \"$f\"; done"
    out = adb("shell", cmd, timeout=15).stdout.decode(errors="replace")
    clips = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            name = parts[2]
            clips.append({"name": name, "size": int(parts[0]), "mtime": int(parts[1]),
                          "duration": app.get(name.rsplit(".", 1)[0], {}).get("duration")})
    clips.sort(key=lambda c: c["mtime"], reverse=True)
    return clips

def delete_clip(name):
    """Delete a recording through Android's media store (keeps the gallery in sync), falling back to the file."""
    path = clip_path(name)
    uri = _app_clips().get(name.rsplit(".", 1)[0], {}).get("uri") or ""
    if uri.startswith("content://media/"):
        adb("shell", f"content delete --uri {shlex.quote(uri)}", timeout=15)
    if adb("shell", f"test -e '{path}' && echo yes", timeout=10).stdout.strip():
        adb("shell", f"content delete --uri content://media/external/file --where \"_data LIKE '%/{safe_name(name)}'\"", timeout=15)
    if adb("shell", f"test -e '{path}' && echo yes", timeout=10).stdout.strip():
        adb("shell", f"rm '{path}'", timeout=15)
    if adb("shell", f"test -e '{path}' && echo yes", timeout=10).stdout.strip():
        raise RuntimeError("the phone didn't delete the file")
    return {"deleted": name}


def clip_size(name):
    out = adb("shell", f"stat -c %s '{clip_path(name)}'", timeout=10).stdout.decode().strip()
    if not out.isdigit():
        raise FileNotFoundError(name)
    return int(out)

def read_range(name, start, length, write):
    """Stream bytes [start, start+length) of a clip from the phone."""
    cmd = f"tail -c +{start + 1} '{clip_path(name)}' | head -c {length}"
    p = subprocess.Popen(["adb", "-s", adb_serial(), "exec-out", cmd], stdout=subprocess.PIPE)
    try:
        while True:
            chunk = p.stdout.read(256 * 1024)
            if not chunk:
                break
            write(chunk)
    finally:
        p.kill()
        p.wait()

_thumb_lock = threading.Lock()

def thumbnail(name, size, mtime):
    config.CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{name}|{size}|{mtime}".encode()).hexdigest()
    out = config.CACHE / f"{key}.jpg"
    if not out.exists():
        with _thumb_lock:  # one ffmpeg at a time; each one reads over adb
            if not out.exists():
                scheme = "https" if config.TLS else "http"
                url = f"{scheme}://127.0.0.1:{config.PORT}/clips/file?name={urllib.parse.quote(name)}"
                tmp = out.with_suffix(".tmp.jpg")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-headers", f"X-BCR-Internal: {auth.INTERNAL_TOKEN}\r\n",
                                "-ss", "0.5", "-i", url, "-frames:v", "1",
                                "-vf", "scale=480:-2", "-q:v", "4", str(tmp)], timeout=120)
                if not tmp.exists():
                    raise RuntimeError("thumbnail failed")
                tmp.rename(out)
    return out.read_bytes()
