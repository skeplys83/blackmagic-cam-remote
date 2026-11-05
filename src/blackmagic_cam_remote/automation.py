"""What happens when recording starts or stops: optional screen off and Do Not Disturb."""
import threading

from .adb import adb
from .screen import screen
from .settings import settings


class Automation:
    def __init__(self):
        self.lock = threading.Lock()
        self.recording = None
        self.dnd_set_by_us = False

    def on_record(self, recording):
        if recording == self.recording:
            return
        first = self.recording is None
        self.recording = recording
        if first and not recording:
            return
        threading.Thread(target=self._apply, args=(recording,), daemon=True).start()

    def _apply(self, recording):
        s = settings.snapshot()
        try:
            if recording:
                if s["dndWhileRecording"]:
                    zen = adb("shell", "settings get global zen_mode", timeout=10).stdout.decode().strip()
                    if zen in ("", "0"):
                        adb("shell", "cmd notification set_dnd priority", timeout=10)
                        self.dnd_set_by_us = True
                if s["screenOffWhileRecording"]:
                    screen.set(False)
            else:
                if self.dnd_set_by_us:
                    adb("shell", "cmd notification set_dnd off", timeout=10)
                    self.dnd_set_by_us = False
                if screen.off:
                    screen.set(True)
        except Exception as e:
            print("automation:", e)

automation = Automation()
