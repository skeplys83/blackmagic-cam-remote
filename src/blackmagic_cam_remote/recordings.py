"""Recordings on the phone: listing, byte-range reads for playback/download, cached thumbnails."""
import hashlib
import shlex
import subprocess
import threading
import urllib.parse

from . import auth, config
from .adb import adb, adb_serial


def safe_name(name):
    if not name or "/" in name or name.startswith(".") or "'" in name or "\n" in name:
        raise ValueError("bad clip name")
    return name

def clip_path(name):
    return f"{config.CLIPS_DIR}/{safe_name(name)}"

def list_clips():
    cmd = f"cd {shlex.quote(config.CLIPS_DIR)} 2>/dev/null && for f in *; do [ -f \"$f\" ] && stat -c '%s|%Y|%n' \"$f\"; done"
    out = adb("shell", cmd, timeout=15).stdout.decode(errors="replace")
    clips = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            clips.append({"name": parts[2], "size": int(parts[0]), "mtime": int(parts[1])})
    clips.sort(key=lambda c: c["mtime"], reverse=True)
    return clips

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
