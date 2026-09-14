#!/usr/bin/env python3
"""Bounded self-healing watchdog for the BORG ChatGPT connection.

This process is deliberately independent of the connector it observes. It uses
payload-free probes, restarts only a failed local component, and stops automatic
intervention after a bounded restart budget. Authentication/configuration
failures are reported but never bypassed or repaired with alternate credentials.
"""
from __future__ import annotations

import json
import argparse
import fcntl
import os
import re
import stat
import subprocess
import time
import tempfile
from contextlib import contextmanager
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from runtime_paths import borg_home, loopback_mcp_url

HOME = Path.home()
RUNTIME = borg_home() / "borg-context"
CONFIG = RUNTIME / "config.json"
STATE_DIR = RUNTIME / "watchdog"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "events.jsonl"
INTERVAL_SECONDS = 30
FAILURES_BEFORE_RESTART = 2
RESTART_COOLDOWN_SECONDS = 300
RESTART_WINDOW_SECONDS = 3600
MAX_RESTARTS_PER_WINDOW = 3
SERVICES = {}
URLS = {"adapter": "http://127.0.0.1:8770/mcp", "gateway": "http://127.0.0.1:8772/mcp",
        "tunnel": "http://127.0.0.1:8773/ready"}
SERVICE_MANAGER = "launchd"


def configure_instance(path: Path) -> None:
    """An independent watchdog may only manage its explicitly named services."""
    global SERVICES, URLS, SERVICE_MANAGER
    doc = json.loads(secure_text(path))
    services, urls = doc.get("services"), doc.get("urls")
    if (doc.get("version") != 1 or not isinstance(services, dict)
            or not services or not isinstance(urls, dict) or set(services) != set(urls)
            or not set(services) <= {"adapter", "gateway", "tunnel"}
            or doc.get("service_manager") not in {"launchd", "systemd"}):
        raise ValueError("watchdog needs explicit instance services and endpoints")
    for component, label in services.items():
        suffix = "connector" if component == "adapter" else component
        if (not isinstance(label, str) or not re.fullmatch(
                r"local\.borg\.[a-zA-Z0-9_-]{1,64}\." + suffix, label)):
            raise ValueError("watchdog may only manage explicitly configured BORG instance services")
        endpoint = urls[component]
        if component == "tunnel":
            if not isinstance(endpoint, str) or not endpoint.endswith("/ready"):
                raise ValueError("tunnel needs an explicit loopback readiness endpoint")
            endpoint = endpoint[:-6] + "/mcp"
        loopback_mcp_url(endpoint)
    SERVICES, URLS, SERVICE_MANAGER = services, urls, doc["service_manager"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def secure_text(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("unsafe private file")
        value = handle.read(65537)
    if not value.strip() or len(value) > 65536:
        raise ValueError("invalid private file")
    return value.strip()


def ensure_state_dir() -> None:
    if STATE_DIR.is_symlink():
        raise ValueError("watchdog state directory cannot be a symlink")
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


def load_state() -> dict:
    ensure_state_dir()
    try:
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass
    return {"version": 1, "components": {}}


def save_state(state: dict) -> None:
    ensure_state_dir()
    fd, name = tempfile.mkstemp(prefix=".state-", dir=STATE_DIR)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_FILE)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def instance_lock():
    """Keep one probe/restart budget owner, including across service reloads."""
    ensure_state_dir()
    fd = os.open(STATE_DIR / "watchdog.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("unsafe watchdog lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        # Keep the lock inode: unlinking it would allow two independent owners.
        os.close(fd)


def event(component: str, outcome: str, detail: str | None = None) -> None:
    ensure_state_dir()
    row = {"at": now_iso(), "component": component, "outcome": outcome}
    if detail:
        row["detail"] = detail
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(LOG_FILE, 0o600)


def inbound_authorization() -> str:
    config = json.loads(secure_text(CONFIG))
    path = Path(config["inbound_authorization_file"])
    value = secure_text(path)
    if not value.startswith("Bearer ") or len(value) < 39:
        raise ValueError("invalid inbound authorization")
    return value


def _json_from_mcp(body: bytes) -> dict | None:
    if len(body) > 512_000:
        return None
    try:
        return json.loads(body)
    except ValueError:
        pass
    for line in body.splitlines():
        if line.startswith(b"data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                continue
    return None


def probe_adapter() -> tuple[bool, str]:
    try:
        authorization = inbound_authorization()
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return False, "configuration_error"
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "borg_status", "arguments": {}}}).encode()
    request = urllib.request.Request(URLS["adapter"], data=payload, method="POST",
        headers={"Authorization": authorization, "Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read(512_001)
            if response.status != 200:
                return False, "http_error"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "authentication_mismatch"
        return False, "http_error"
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "unreachable"
    doc = _json_from_mcp(body)
    result = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(result, dict) or result.get("isError"):
        return False, "invalid_status"
    status = result.get("structuredContent")
    if not isinstance(status, dict):
        for part in result.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text":
                try:
                    status = json.loads(part.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(status, dict):
                    break
    if isinstance(status, dict) and status.get("connector") == {"status": "PASS"}:
        return True, "ready"
    return False, "invalid_status"


def probe_gateway() -> tuple[bool, str]:
    try:
        config = json.loads(secure_text(RUNTIME / "cloudflare/config.json"))
        public = urlsplit(config["public_url"])
        if (public.scheme != "https" or not public.hostname or public.netloc != public.hostname
                or public.path or public.query or public.fragment):
            raise ValueError("invalid public origin")
        expected = 'Bearer resource_metadata="' + config["public_url"] + '/.well-known/oauth-protected-resource"'
    except (OSError, ValueError, KeyError, TypeError):
        return False, "configuration_error"
    request = urllib.request.Request(URLS["gateway"], data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status == 401, "unexpected_status"
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return False, "http_error"
        try:
            body = json.loads(exc.read(4097))
            if (exc.headers.get("WWW-Authenticate") == expected
                    and body == {"error": "BORG owner authentication required"}):
                return True, "ready"
        except (OSError, ValueError, AttributeError):
            pass
        return False, "identity_mismatch"
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "unreachable"


def probe_tunnel() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(URLS["tunnel"], timeout=5) as response:
            return (True, "ready") if response.status == 200 else (False, "http_error")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False, "unreachable"


def restart_service(component: str) -> bool:
    label = SERVICES.get(component)
    if not label:
        return False
    target = f"gui/{os.getuid()}/{label}"
    command = (["/bin/launchctl", "kickstart", "-k", target] if SERVICE_MANAGER == "launchd"
               else ["systemctl", "--user", "restart", label])
    try:
        completed = subprocess.run(command,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=20, check=False)
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def component_state(state: dict, component: str) -> dict:
    components = state.setdefault("components", {})
    return components.setdefault(component, {"failures": 0, "last_restart": 0.0, "restarts": []})


def record_probe(state: dict, component: str, ok: bool, reason: str,
                 restart: Callable[[str], bool] = restart_service, now: float | None = None) -> None:
    stamp = time.time() if now is None else now
    row = component_state(state, component)
    if ok:
        if row.get("failures", 0):
            event(component, "recovered_without_restart", reason)
        row["failures"] = 0
        row["last_ok"] = stamp
        row["last_reason"] = reason
        return

    row["failures"] = int(row.get("failures", 0)) + 1
    row["last_failure"] = stamp
    row["last_reason"] = reason
    event(component, "probe_failed", reason)

    # Configuration and authentication mismatches require reconciliation, not
    # blind process restarts or weaker credentials.
    if reason in {"configuration_error", "authentication_mismatch", "identity_mismatch"}:
        event(component, "manual_attention_required", reason)
        return
    if row["failures"] < FAILURES_BEFORE_RESTART:
        return

    restarts = [float(value) for value in row.get("restarts", [])
                if stamp - float(value) <= RESTART_WINDOW_SECONDS]
    row["restarts"] = restarts
    if len(restarts) >= MAX_RESTARTS_PER_WINDOW:
        event(component, "restart_budget_exhausted", reason)
        return
    last_restart = float(row.get("last_restart", 0.0))
    if stamp - last_restart < RESTART_COOLDOWN_SECONDS:
        return

    if restart(component):
        row["last_restart"] = stamp
        row["restarts"] = [*restarts, stamp]
        row["failures"] = 0
        event(component, "restart_started", reason)
    else:
        event(component, "restart_failed", reason)


def cycle(state: dict, probes: dict[str, Callable[[], tuple[bool, str]]] | None = None,
          restart: Callable[[str], bool] = restart_service, now: float | None = None) -> dict:
    if probes is None:
        available = {"adapter": probe_adapter, "gateway": probe_gateway, "tunnel": probe_tunnel}
        probes = {name: available[name] for name in SERVICES}
    for component, probe in probes.items():
        try:
            ok, reason = probe()
        except Exception:
            ok, reason = False, "probe_exception"
        record_probe(state, component, ok, reason, restart=restart, now=now)
    state["observed_at"] = now_iso()
    save_state(state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="perform one scheduled probe cycle")
    parser.add_argument("--adapter-url", help="explicit loopback adapter endpoint during an owner migration")
    args = parser.parse_args()
    configure_instance(RUNTIME / "watchdog/config.json")
    if args.adapter_url:
        URLS["adapter"] = loopback_mcp_url(args.adapter_url)
    with instance_lock() as acquired:
        if not acquired:
            return 0
        state = load_state()
        event("watchdog", "started")
        while True:
            cycle(state)
            if args.once:
                return 0
            time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
