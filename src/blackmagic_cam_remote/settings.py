"""Settings kept between runs (data/settings.json): the "while recording" toggles and the last phone address."""
import json
import threading

from . import config

TOGGLES = ("screenOffWhileRecording", "dndWhileRecording")


class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {"screenOffWhileRecording": False, "dndWhileRecording": False, "lastAddress": ""}
        try:
            self.values.update(json.loads(config.SETTINGS_FILE.read_text()))
        except (OSError, ValueError):
            pass

    def get(self, key):
        with self.lock:
            return self.values.get(key)

    def snapshot(self):
        with self.lock:
            return dict(self.values)

    def toggles(self):
        with self.lock:
            return {k: self.values[k] for k in TOGGLES}

    def update(self, changes, toggles_only=False):
        """Apply known keys from changes, save, and return the toggles."""
        with self.lock:
            for k in self.values:
                if k in changes and (k in TOGGLES or not toggles_only):
                    self.values[k] = bool(changes[k]) if k in TOGGLES else str(changes[k])
            config.DATA.mkdir(parents=True, exist_ok=True)
            config.SETTINGS_FILE.write_text(json.dumps(self.values, indent=2))
            return {k: self.values[k] for k in TOGGLES}


settings = Settings()
