"""Run the native scoped graph feed and recall projector in sequence."""
from __future__ import annotations

import argparse
import importlib.machinery
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import tempfile
import time


def initialize(root: Path) -> None:
    sys.path[:0] = [str(root / "mem0/bin"), str(root / "graphiti")]
    native = importlib.machinery.SourceFileLoader("borg_memory_init", str(root / "mem0/bin/mem0ctl")).load_module()
    memory = native.get_memory("qwen")
    memory.db.connection.close()
    from graph_scope import ScopeRegistry
    ScopeRegistry(root / "graphiti/data/scope-graphs.json").ensure_scope(os.environ["BORG_MEMORY_SCOPE"])
    print(json.dumps({"state": "initialized", "memories_created": 0}), flush=True)


CYCLE_MAX_SECONDS = 810
SUCCESS_MAX_AGE_SECONDS = 900


def record(root: Path, state: dict) -> None:
    from installer.config import write_private
    path = root / "graphiti/data/brain-state.json"
    write_private(path, json.dumps(state, indent=2) + "\n", replace=path.exists())


def terminate_child(process) -> None:
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def step_receipt(output, step: str, doc: dict) -> dict:
    output.seek(0)
    body = output.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("Brain child output exceeded its receipt limit")
    rows = []
    for line in body.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    if not rows:
        raise ValueError("Brain child did not produce a receipt")
    row = rows[-1]
    if step == "feed":
        # PARTIAL is healthy only for a bounded, checkpointed pass with work
        # deferred by its budget; unresolved graph failures are not healthy.
        if (row.get("schema") != "graph-feed/2" or row.get("outcome") not in {"PASS", "PARTIAL"}
                or row.get("collection") != doc["memory"]["collection"]
                or not row.get("receipt_id") or type(row.get("scan_epoch")) is not int
                or row.get("retry_count") != 0 or row.get("pending_graph_retries") != 0):
            raise ValueError("Native graph feed did not complete a healthy checkpoint")
        keys = ["schema", "receipt_id", "outcome", "collection", "scan_epoch", "retry_count",
                "pending_graph_retries", "new_episodes", "unattempted_episodes", "cursor_after_present"]
    else:
        if row.get("schema") != "graph-recall-projector/1" or row.get("status") != "PASS":
            raise ValueError("Native recall projection did not complete")
        keys = ["schema", "operation", "status", "attempted", "projected", "resolved", "waiting", "failed"]
    return {key: row[key] for key in keys if key in row}


def run_cycle(root: Path, doc: dict, stopped: threading.Event, previous: dict | None = None) -> dict:
    state = {"schema": "borg-brain-cycle/v1", "instance_id": doc["instance_id"],
             "home": str(root), "pid": os.getpid(), "state": "running",
             "started_at": time.time(), "completed_at": None, "exit_codes": [],
             "last_success": (previous or {}).get("last_success"), "steps": []}
    record(root, state)
    env = {**os.environ, "GRAPH_FEED_LIVE": "1", "MEM0_GRAPH_PROJECTOR_LIVE": "1"}
    commands = [
        ([sys.executable, "-B", str(root / "graphiti/backfill.py"), "--limit", "96", "--fetch", "500",
          "--scan-points", "12000", "--scan-pages", "24", "--episodes", "12", "--workers", "1",
          "--episode-timeout", "420", "--pass-budget", "600", "--cleanup-margin", "30", "--json"], 660),
        ([sys.executable, "-B", str(root / "graphiti/bin/graph-recall-projector")], 120),
    ]
    for command, timeout in commands:
        if stopped.is_set():
            break
        with tempfile.TemporaryFile(mode="w+b") as output:
            try:
                process = subprocess.Popen(command, env=env, cwd=root, stdout=output)
            except OSError:
                state["exit_codes"].append(127)
                break
            deadline = time.monotonic() + timeout
            timed_out = False
            while process.poll() is None:
                if stopped.wait(0.25) or time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > 1024 * 1024:
                    timed_out = not stopped.is_set()
                    terminate_child(process)
                    break
            code = 124 if timed_out else process.returncode
            if code == 0 and not stopped.is_set():
                try:
                    state["steps"].append(step_receipt(output, "feed" if not state["steps"] else "projector", doc))
                except (ValueError, KeyError, TypeError):
                    code = 125
            state["exit_codes"].append(code)
            if code != 0:
                break
    state["completed_at"] = time.time()
    state["state"] = ("interrupted" if stopped.is_set() else
                      "succeeded" if state["exit_codes"] == [0, 0] else "failed")
    if state["state"] == "succeeded":
        state["last_success"] = {"completed_at": state["completed_at"], "exit_codes": [0, 0], "steps": state["steps"]}
    record(root, state)
    print(json.dumps(state), flush=True)
    return state


def run(root: Path, *, once: bool = False) -> int:
    from installer.config import load
    doc = load(root)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    previous = None
    while not stopped.is_set():
        previous = run_cycle(root, doc, stopped, previous)
        if once or stopped.is_set():
            return 0 if previous["state"] == "succeeded" else 1
        stopped.wait(60)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["initialize", "run", "once"])
    args = parser.parse_args()
    from installer.config import load
    doc = load(os.environ["BORG_HOME"])
    root = Path(doc["home"])
    if args.operation == "initialize":
        initialize(root)
        return 0
    return run(root, once=args.operation == "once")


if __name__ == "__main__":
    # The script is also invoked directly by native service managers.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
