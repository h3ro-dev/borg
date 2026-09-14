"""Forward cancellation to the installer subprocess group we create."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def run(command: list[str]) -> int:
    cancelled = 0

    def cancel(signum, _frame):
        nonlocal cancelled
        if not cancelled:
            cancelled = signum

    for signum in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
        signal.signal(signum, cancel)
    child = subprocess.Popen(command, start_new_session=True)
    while not cancelled:
        code = child.poll()
        if code is not None:
            return code
        time.sleep(0.1)
    # Reaping inside a signal handler can deadlock Popen's wait lock. Handle
    # the signal here, outside that handler, and address only our process group.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait(timeout=8)
    return 128 + cancelled


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1:]))
