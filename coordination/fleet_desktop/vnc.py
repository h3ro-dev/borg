"""Narrow vncdotool 1.4.2 adaptations for resizing Tart guests and revocable input."""
import io
import threading
import time
from fleet_browser.supervisor import SupervisorError

from vncdotool.client import VNCDoToolClient, VNCDoToolFactory


class CaptureToken(io.BytesIO):
    """A single bounded memory sink; asynchronous callbacks never own a file path."""
    def __init__(self, maximum, deadline, revoked):
        super().__init__()
        self.maximum, self.deadline, self.revoked = maximum, deadline, revoked
        self.lock = threading.Lock()
        self.complete = False

    def _check(self):
        if self.closed or self.revoked() or time.monotonic() >= self.deadline:
            raise SupervisorError('capture_cancelled')

    def write(self, data):
        with self.lock:
            self._check()
            if self.complete:
                raise SupervisorError('capture_complete')
            if self.tell() + len(data) > self.maximum:
                raise SupervisorError('output_limit')
            return super().write(data)

    def finish(self):
        with self.lock:
            self._check()
            self.complete = True

    def result(self):
        with self.lock:
            self._check()
            if not self.complete:
                raise SupervisorError('capture_incomplete')
            return self.getvalue()

    def close(self):
        with self.lock:
            super().close()


def factory(check):
    class Client(VNCDoToolClient):
        def updateDesktopSize(self, width, height):
            super().updateDesktopSize(width, height)
            # Upstream resizes its PIL framebuffer but leaves RFB refresh bounds stale.
            self.width, self.height = width, height

        def keyEvent(self, *args, **kwargs):
            check()
            return super().keyEvent(*args, **kwargs)

        def pointerEvent(self, *args, **kwargs):
            check()
            return super().pointerEvent(*args, **kwargs)

        def framebufferUpdateRequest(self, *args, **kwargs):
            check()
            return super().framebufferUpdateRequest(*args, **kwargs)

        def _captureSave(self, data, fp, *args, format=None):
            check()
            # Only the supervisor may finalize output; reject raw paths here.
            if not isinstance(fp, CaptureToken) or args or format != 'PNG':
                raise SupervisorError('invalid_capture_target')
            result = super()._captureSave(data, fp, format=format)
            fp.finish()
            return result

        def keyPress(self, key):
            if key == 'backspace': key = 'bsp'
            return super().keyPress(key)

        def desktopText(self, text):
            for char in text:
                shifted = char.isupper() or char in self.SPECIAL_KEYS_US
                if shifted: self.keyEvent(0xFFE1, down=True)
                self.keyEvent(ord(char), down=True)
                self.keyEvent(ord(char), down=False)
                if shifted: self.keyEvent(0xFFE1, down=False)
            # Threaded proxy's Deferred chain must retain its protocol instance.
            return self

    class Factory(VNCDoToolFactory):
        protocol = Client
    return Factory
