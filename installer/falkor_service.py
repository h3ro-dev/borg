"""Run the upstream embedded FalkorDB server as one owned local service."""
import os
from pathlib import Path
import signal
import threading


def main() -> None:
    from redislite import Redis
    root = Path(os.environ["BORG_HOME"]) / "graphiti/data"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    # Standard Redis/FalkorDB TCP protocol; Graphiti needs no compatibility shim.
    server = Redis(str(root / "graph.rdb"), serverconfig={
        "bind": "127.0.0.1", "port": os.environ["BORG_FALKORDB_PORT"],
        "protected-mode": "yes", "appendonly": "yes", "dir": str(root)})
    if not server.ping():
        raise RuntimeError("The instance graph server did not become ready")
    try:
        stopped.wait()
    finally:
        server.shutdown(save=True)


if __name__ == "__main__":
    main()
