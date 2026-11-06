"""Is anyone using the dashboard? Background work towards the phone runs only while someone is.

A page keeps /events open, so every open /events stream is a viewer; /api requests (scripts) count for a while too.
After the last viewer leaves, the helper stays active for IDLE_AFTER seconds (a reload or a short network drop
doesn't restart everything), then goes idle: no adb commands, no API probes, no reconnect attempts or port scans,
no camera websocket. It stays active while the phone records, so automation can undo screen off and Do Not Disturb
when recording stops. The first viewer wakes everything up again.
"""
import threading
import time

IDLE_AFTER = 60

_cond = threading.Condition()
_viewers = 0
_last = float("-inf")  # when the last viewer left or the last /api request came; idle from the start


def enter():
    global _viewers
    with _cond:
        _viewers += 1
        _cond.notify_all()


def leave():
    global _viewers, _last
    with _cond:
        _viewers -= 1
        _last = time.monotonic()


def touch():
    global _last
    with _cond:
        _last = time.monotonic()
        _cond.notify_all()


def _active():
    from . import events  # events imports this module
    hub = events.hub
    return bool(_viewers or time.monotonic() - _last < IDLE_AFTER
                or (hub and hub.connected and hub.recording()))


def active():
    with _cond:
        return _active()


def wait():
    """Block until someone uses the dashboard (returns at once if someone does)."""
    with _cond:
        while not _active():
            _cond.wait(60)
