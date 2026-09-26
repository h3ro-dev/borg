"""Report observations separately from installation and provider authorization."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request


def get_json(url: str, *, timeout: int = 8, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)


def conductor_headers(root: Path) -> dict[str, str]:
    """Read the conductor token without following a substituted private path."""
    token_path = root / "conductors/primary/profile/.conductor/http-token"
    try:
        directory = token_path.parent.lstat()
    except FileNotFoundError:
        return {}
    if (not stat.S_ISDIR(directory.st_mode) or token_path.parent.is_symlink()
            or directory.st_uid != os.getuid() or directory.st_mode & 0o077):
        raise ValueError("Unsafe conductor token directory")
    try:
        fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "r") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 128):
            raise ValueError("Unsafe conductor token file")
        token = handle.read(129).strip()
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("Malformed conductor token")
    return {"Authorization": "Bearer " + token}


def mcp_call(url: str, name: str, authorization: str, arguments: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": arguments or {}}}
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Authorization": authorization, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=35) as response:
        body = response.read(1024 * 1024)
    try:
        document = json.loads(body)
    except ValueError:
        document = next(json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith(b"data:"))
    result = document.get("result", {})
    if document.get("error") or result.get("isError"):
        raise RuntimeError("Native MCP operation was unsuccessful")
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for part in result.get("content", []):
        if part.get("type") == "text":
            return json.loads(part["text"])
    raise RuntimeError("Native MCP response has no structured result")


def brain_health(doc: dict, is_running: bool, *, now: float | None = None) -> dict:
    from installer.config import read_private
    from installer.brain_service import CYCLE_MAX_SECONDS, SUCCESS_MAX_AGE_SECONDS
    root = Path(doc["home"])
    if not is_running:
        return {"state": "stopped"}
    try:
        receipt = read_private(root / "graphiti/data/brain-state.json")
        if (receipt.get("schema") != "borg-brain-cycle/v1" or receipt.get("instance_id") != doc["instance_id"]
                or receipt.get("home") != str(root)):
            raise ValueError("Brain receipt identity differs")
        clock = time.time() if now is None else now
        state = receipt["state"]
        success = receipt.get("last_success") or {}
        age = clock - float(success["completed_at"])
        current_age = clock - float(receipt["started_at"])
        good = (success.get("exit_codes") == [0, 0] and len(success.get("steps", [])) == 2 and -5 <= age <= SUCCESS_MAX_AGE_SECONDS
                and -5 <= current_age <= SUCCESS_MAX_AGE_SECONDS and (
                    state == "succeeded" and receipt.get("exit_codes") == [0, 0]
                    and receipt.get("completed_at") == success.get("completed_at")
                    or state == "running" and current_age <= CYCLE_MAX_SECONDS))
        return {"state": "cycle_verified" if good else "stale_or_failed", "cycle_state": state,
                "success_age_seconds": round(age, 1), "exit_codes": receipt.get("exit_codes"),
                "last_success": success, "started_at": receipt["started_at"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unverified"}


def graph_llm_health(doc: dict) -> dict:
    try:
        observed = get_json(f"http://127.0.0.1:{doc['ports']['graph_llm']}/health", timeout=3)
        expected = f"http://127.0.0.1:{doc['ports']['ollama']}"
        good = observed.get("ok") is True and observed.get("upstream") == expected
        return {"state": "identity_verified" if good else "identity_mismatch"}
    except (OSError, ValueError, TypeError):
        return {"state": "unavailable"}


def wait_for_local_ready(doc: dict, seconds: int = 900) -> dict:
    deadline = time.monotonic() + max(0, seconds)
    while True:
        result = status(doc)
        if result["local_services_ready"] or time.monotonic() >= deadline:
            return result
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def status(doc: dict) -> dict:
    from installer import blueprint
    from installer.config import read_private
    root, ports = Path(doc["home"]), doc["ports"]
    enabled = set(blueprint.service_names(doc))
    result = {"schema": "borg-health/v2", "instance_id": doc["instance_id"], "owner": doc["owner"],
              "home": str(root), "ready": False, "local_services_ready": False, "components": {},
              "notice": "Installed, responding, authenticated, and operation verified are distinct states."}
    components = result["components"]
    for name in [n for n in ["qdrant", "graph", "ollama", "graph_llm", "memory", "connector", "inbox", "conductor"] if n in enabled]:
        try:
            with socket.create_connection(("127.0.0.1", ports[name]), timeout=2):
                components[name] = {"state": "responding", "port": ports[name]}
        except OSError:
            components[name] = {"state": "not_responding", "port": ports[name]}
    if "graph_llm" in enabled:
        components["graph_llm"] = graph_llm_health(doc)
    try:
        from installer.config import read_private
        config = read_private(root / "borg-context/config.json")
        auth_path = Path(config["inbound_authorization_file"])
        # Same private-file reader as the deployed connector; never return the value.
        import sys
        runtime = root / "borg-context"
        sys.path.insert(0, str(runtime))
        from borg_context_server import private_text
        authorization = private_text(auth_path)
        observed = mcp_call(f"http://127.0.0.1:{ports['connector']}/mcp", "borg_status", authorization)
        components["connector"] = {"state": "authenticated" if observed.get("connector", {}).get("status") == "PASS" else "failed",
                                   "source_sha256": observed.get("adapter_source_sha256")}
        if "memory" in enabled:
            mem = observed.get("mem0", {})
            expected = doc["memory"]["principal"]
            components["memory"] = {"state": "authenticated" if mem.get("status") == "PASS" and mem.get("principal") == expected else "failed",
                                    "expected_principal": expected, "observed_principal": mem.get("principal")}
            components["graph"]["connector_observation"] = observed.get("graph")
    except (OSError, ValueError, RuntimeError, ImportError, StopIteration):
        components["connector"]["authenticated_probe"] = "unavailable"
    if "ollama" in enabled:
        try:
            tags = get_json(f"http://127.0.0.1:{ports['ollama']}/api/tags")["models"]
            available = {row["name"]: row["digest"].removeprefix("sha256:") for row in tags}
            required = {doc["models"][key] for key in ["extraction", "graph", "embedding"]}
            verified = all(available.get(doc["models"][key]) == doc["models"].get(key + "_id", "").removeprefix("ollama:sha256:")
                           for key in ["extraction", "embedding"])
            components["models"] = {"state": "digest_verified" if required <= available.keys() and verified else "missing_or_changed",
                                    "missing": sorted(required - available.keys())}
        except (OSError, ValueError, KeyError):
            components["models"] = {"state": "unavailable"}
    if "conductor" in enabled:
        try:
            native = get_json(f"http://127.0.0.1:{ports['conductor']}/status",
                              headers=conductor_headers(root))
            expected_profile = str(root / "conductors/primary/profile")
            matched = native.get("ok") is True and native.get("port") == ports["conductor"] and native.get("codexHome") == expected_profile
            components["conductor"]["app_server"] = "initialized" if matched else "identity_mismatch"
        except (OSError, ValueError):
            components["conductor"]["app_server"] = "unavailable"
    if "graph" in enabled:
        try:
            from redis import Redis
            graph = Redis(host="127.0.0.1", port=ports["graph"], socket_timeout=3,
                          socket_connect_timeout=3, decode_responses=True)
            try:
                matched = graph.ping() and Path(graph.config_get("dir")["dir"]).resolve() == root / "graphiti/data"
            finally:
                graph.close()
            components["graph"]["state"] = "storage_verified" if matched else "identity_mismatch"
        except Exception:
            components["graph"]["state"] = "unavailable"
    if "qdrant" in enabled:
        try:
            from urllib.parse import quote
            collection = get_json(f"http://127.0.0.1:{ports['qdrant']}/collections/" + quote(doc["memory"]["collection"], safe=""))["result"]
            actual_size = collection["config"]["params"]["vectors"]["size"]
            components["qdrant"]["state"] = "collection_verified" if actual_size == doc["models"]["embedding_dimensions"] else "dimension_mismatch"
        except (OSError, ValueError, KeyError, TypeError):
            components["qdrant"]["state"] = "collection_unavailable"
    from installer.services import service_environment
    from installer.downloads import executable
    try:
        env = service_environment(doc)
    except (OSError, ValueError):
        env = None
    python = str(root / "mem0/venv/bin/python")
    if "inbox" in enabled:
        try:
            probe = subprocess.run([python, str(root / "app/coordination/bin/borg-coordination"),
                                    "status", "--home", str(root)], env=env, capture_output=True, text=True, timeout=15)
            hub = json.loads(probe.stdout)
            components["inbox"]["state"] = "authenticated" if probe.returncode == 0 and hub.get("status") == "ready" else "unavailable"
        except (OSError, ValueError, subprocess.SubprocessError):
            components["inbox"]["state"] = "authentication_unverified"
    if "conductor" in enabled:
        try:
            probe = subprocess.run([str(executable(root, "node", "node")), str(root / "app/conductor/borg-conductor.mjs"),
                                    "auth", "status", "--config", str(root / "conductors/config.json"), "--lane", "primary"],
                                   env=env, capture_output=True, text=True, timeout=15)
            provider = json.loads(probe.stdout)
            components["provider_login"] = {"state": "authenticated_and_pinned" if probe.returncode == 0 and provider.get("accountMatchesPin") else "sign_in_required",
                                             "next_command": "borg auth codex"}
        except (OSError, ValueError, subprocess.SubprocessError):
            components["provider_login"] = {"state": "unavailable", "next_command": "borg auth codex"}
    if "conductor" in enabled and blueprint.full(doc):
        try:
            payload = {"method": "hooks/list", "params": {"cwd": str(root / "projects")}, "timeoutMs": 8000}
            headers = {"Content-Type": "application/json", **conductor_headers(root)}
            request = urllib.request.Request(f"http://127.0.0.1:{ports['conductor']}/rpc",
                        data=json.dumps(payload).encode(), headers=headers)
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
                observed = json.load(response)
            native = observed.get("result", observed)
            command = shlex.join([str(root / "mem0/venv/bin/python"), str(root / "app/borg.py"), "hook", "--home", str(root)])
            rows = [row for item in native.get("data", []) for row in item.get("hooks", [])
                    if row.get("command", "").startswith(command + " ")
                    and row.get("sourcePath") == str(root / "conductors/primary/profile/config.toml")]
            good = len(rows) == 4 and all(row.get("trustStatus") == "trusted" for row in rows)
            components["capture_hooks"] = {"state": "registered_and_trusted" if good else "incomplete", "count": len(rows)}
        except (OSError, ValueError, KeyError, TypeError):
            components["capture_hooks"] = {"state": "unavailable"}
    from installer.services import running
    for name in [n for n in ["brain", "watchdog"] if n in enabled]:
        try:
            components[name] = {"state": "running" if running(doc, name) else "stopped"}
        except (OSError, subprocess.SubprocessError):
            components[name] = {"state": "unavailable"}
    if "brain" in enabled:
        components["brain"] = brain_health(doc, components["brain"]["state"] == "running")
    try:
        state = read_private(root / "borg-context/watchdog/state.json")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["observed_at"])).total_seconds()
        names = ["adapter", "gateway", "tunnel"] if doc["external_access"]["enabled"] else ["adapter"]
        healthy = -5 <= age <= 90 and all(
            state["components"].get(name, {}).get("failures") == 0 and
            state["components"].get(name, {}).get("last_reason") == "ready" for name in names)
        components["watchdog"]["probes"] = "ready" if healthy else "stale_or_failed"
        components["watchdog"]["observed_at"] = state["observed_at"]
    except (OSError, ValueError, KeyError, TypeError):
        components["watchdog"]["probes"] = "unavailable"
    components["external_access"] = {
        "state": "local_gateway_and_tunnel_ready" if doc["external_access"]["enabled"]
            and components["watchdog"].get("probes") == "ready" else
            "setup_incomplete" if doc["external_access"]["enabled"] else "not_configured",
        "client_oauth_acceptance": "requires_verification_in_the_web_client"}
    expected = {"qdrant": "collection_verified", "graph": "storage_verified", "memory": "authenticated",
                "connector": "authenticated", "inbox": "authenticated", "models": "digest_verified",
                "capture_hooks": "registered_and_trusted", "brain": "cycle_verified", "watchdog": "running",
                "graph_llm": "identity_verified"}
    if "conductor" in enabled and not blueprint.full(doc):
        components["codex_client"] = tools_client_health(doc)
        expected["codex_client"] = "configured_without_capture"
    if doc.get("blueprint") and blueprint.selected(doc, "beads"):
        try:
            probe = subprocess.run([str(root / "bin/bd"), "list", "--limit", "1", "--json"], env=env,
                                   capture_output=True, text=True, timeout=15)
            info = json.loads(probe.stdout)
            # The native wrapper validates the private store and strips inherited Dolt routing.
            components["beads"] = {"state": "store_verified" if probe.returncode == 0 and isinstance(info, list) else "unverified"}
        except (OSError, ValueError, subprocess.SubprocessError):
            components["beads"] = {"state": "unavailable"}
        expected["beads"] = "store_verified"
    checks = set(enabled) | ({"models"} if blueprint.full(doc) else set())
    checks |= ({"capture_hooks"} if blueprint.full(doc) and "conductor" in enabled else set())
    checks |= {"beads", "codex_client"} & components.keys()
    result["local_services_ready"] = (
        all(components.get(k, {}).get("state") == v for k, v in expected.items() if k in checks)
        and ("conductor" not in enabled or components["conductor"].get("app_server") == "initialized")
        and components["watchdog"].get("probes") == "ready"
        and ("graph" not in enabled or (components["graph"].get("connector_observation") or {}).get("status") in {"READY", "PASS"}))
    owner_setup = []
    if doc.get("blueprint"):
        row = blueprint.selected_machine(doc)
        owner_setup = [item for item in blueprint.describe(row, blueprint.catalog())["setup"]
                       if item["id"] not in {"codex", "inbox", "beads"}]
        result["selected_setup"] = owner_setup
    result["ready"] = (result["local_services_ready"] and not owner_setup
        and ("conductor" not in enabled or components["provider_login"]["state"] == "authenticated_and_pinned"))
    result["state"] = ("ready" if result["ready"] else "selected_setup_required" if result["local_services_ready"] and owner_setup
                       else "provider_sign_in_required" if result["local_services_ready"] else "setup_incomplete")
    return result


def tools_client_health(doc: dict) -> dict:
    """Check live isolated Codex configuration; tools nodes must have no memory hooks."""
    root = Path(doc["home"])
    try:
        payload = {"method": "config/read", "params": {"includeLayers": True}, "timeoutMs": 8000}
        headers = {"Content-Type": "application/json", **conductor_headers(root)}
        request = urllib.request.Request(f"http://127.0.0.1:{doc['ports']['conductor']}/rpc",
            data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            observed = json.load(response)
        native = observed.get("result", observed)
        client = native["config"]
        mcp = client.get("mcp_servers", {}).get("borg", {})
        matched = (mcp.get("command") == str(root / "mem0/venv/bin/python")
                   and mcp.get("args") == [str(root / "app/borg.py"), "mcp-stdio", "--home", str(root)])
        stale = any(" hook --home " in hook.get("command", "")
                    for groups in client.get("hooks", {}).values() if isinstance(groups, list)
                    for group in groups for hook in group.get("hooks", []))
        return {"state": "configured_without_capture" if matched and not stale else "incomplete"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unavailable"}
