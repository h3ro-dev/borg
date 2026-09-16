"""Native user services, addressed only by this installation's UUID."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import plistlib
import re
import socket
import subprocess
import time

from installer.config import environment
from installer.downloads import executable


def label(doc: dict, component: str) -> str:
    return "local.borg." + doc["instance_id"] + "." + component


def service_environment(doc: dict) -> dict[str, str]:
    root = Path(doc["home"])
    node = executable(root, "node", "node")
    paths = [str(root / "mem0/venv/bin"), str(root / "bin"), str(node.parent), str(root / "runtime/npm/node_modules/.bin"),
             str(executable(root, "beads", "bd").parent),
             "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return {"HOME": str(Path.home()), "PATH": os.pathsep.join(paths), "LANG": "en_US.UTF-8",
            **environment(doc), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "BEADS_DIR": str(root / "beads/.beads"), "BD_DISABLE_METRICS": "1",
            "OLLAMA_HOST": f"127.0.0.1:{doc['ports']['ollama']}",
            "OLLAMA_MODELS": str(root / "models/ollama"), "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_CONTEXT_LENGTH": str(doc["models"].get("context_length", 16384)),
            "OLLAMA_MAX_LOADED_MODELS": "2",
            "OLLAMA_KEEP_ALIVE": "5m", "OLLAMA_NO_CLOUD": "1"}


def specifications(doc: dict) -> dict[str, dict]:
    root, ports = Path(doc["home"]), doc["ports"]
    py = str(root / "mem0/venv/bin/python")
    node = str(executable(root, "node", "node"))
    code = root / "app"
    base = service_environment(doc)
    rows = {
        "qdrant": {"args": [str(executable(root, "qdrant", "qdrant"))], "port": ports["qdrant"],
                    "env": {"QDRANT__SERVICE__HOST": "127.0.0.1",
                            "QDRANT__SERVICE__HTTP_PORT": str(ports["qdrant"]),
                            "QDRANT__SERVICE__GRPC_PORT": str(ports["qdrant_grpc"]),
                            "QDRANT__STORAGE__STORAGE_PATH": str(root / "mem0/data/qdrant"),
                            "QDRANT__TELEMETRY_DISABLED": "true"}},
        "graph": {"args": [py, str(code / "installer/falkor_service.py")], "port": ports["graph"]},
        "ollama": {"args": [str(executable(root, "ollama", "ollama")), "serve"], "port": ports["ollama"]},
        "graph_llm": {"args": [py, str(root / "graphiti/bin/ollama-schema-shim")], "port": ports["graph_llm"],
                      "env": {"OLLAMA_UPSTREAM": base["BORG_OLLAMA_URL"], "SHIM_PORT": str(ports["graph_llm"]),
                              "SHIM_PAIRS_LOG": str(root / "graphiti/data/shim-pairs.jsonl"),
                              "SHIM_KEEP_ALIVE": "5m"}},
        "memory": {"args": [py, str(root / "mem0/bin/mem0-mcp-server-v2"), "--http", str(ports["memory"])],
                   "port": ports["memory"]},
        "brain": {"args": [py, str(code / "installer/brain_service.py"), "run"]},
        "connector": {"args": [py, str(root / "borg-context/borg_context_server.py"), "--http", str(ports["connector"]),
                               "--config", str(root / "borg-context/config.json")], "port": ports["connector"]},
        "conductor": {"args": [node, str(code / "conductor/borg-conductor.mjs"), "start",
                                "--config", str(root / "conductors/config.json"), "--lane", "primary"],
                      "port": ports["conductor"],
                      "env": {"CONDUCTOR_PORT": str(ports["conductor"]), "CONDUCTOR_HOST": "127.0.0.1",
                              "CONDUCTOR_LOGS": str(root / "conductors/primary/logs"),
                              "CODEX_HOME": str(root / "conductors/primary/profile"),
                              "CODEX_BIN": str(root / "runtime/npm/node_modules/.bin/codex")}},
    }
    # The actual native Hub bootstrap supplies its final service contract.
    contract = root / "coordination/service.json"
    if contract.exists():
        from installer.config import read_private
        hub = read_private(contract)
        rows["inbox"] = {"args": hub["args"], "env": hub.get("env", {}), "port": ports["inbox"]}
    if doc["external_access"]["enabled"]:
        rows["gateway"] = {"args": [py, str(root / "borg-context/cloudflare_gateway.py"),
                                    "--http", str(ports["gateway"]), "--config",
                                    str(root / "borg-context/cloudflare/config.json")], "port": ports["gateway"]}
        rows["tunnel"] = {"args": [str(executable(root, "cloudflared", "cloudflared")),
                                    "tunnel", "--config", str(root / "cloudflare/cloudflared.json"), "run"],
                          "port": ports["tunnel_metrics"]}
    if (root / "borg-context/watchdog/config.json").exists():
        rows["watchdog"] = {"args": [py, str(root / "borg-context/connection_watchdog.py")]}
    from installer.blueprint import service_names
    enabled = set(service_names(doc))
    rows = {name: row for name, row in rows.items() if name in enabled}
    for name, row in rows.items():
        row["env"] = {**base, **row.get("env", {})}
        row["cwd"] = str(root)
        row["label"] = label(doc, name)
    return rows


def running(doc: dict, component: str) -> bool:
    name = label(doc, component)
    if platform.system() == "Darwin":
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{name}"],
                                capture_output=True, text=True, timeout=10)
        return result.returncode == 0 and bool(re.search(r"^\s*pid = [1-9][0-9]*$", result.stdout, re.M))
    result = subprocess.run(["systemctl", "--user", "is-active", name + ".service"],
                            capture_output=True, timeout=10)
    return result.returncode == 0


def install_definition(doc: dict, name: str, spec: dict) -> Path:
    root = Path(doc["home"])
    logs = root / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    if platform.system() == "Darwin":
        directory = Path.home() / "Library/LaunchAgents"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (spec["label"] + ".plist")
        body = plistlib.dumps({"Label": spec["label"], "ProgramArguments": spec["args"],
            "WorkingDirectory": spec["cwd"], "EnvironmentVariables": spec["env"],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15, "ExitTimeOut": 10,
            "StandardOutPath": str(logs / (name + ".log")), "StandardErrorPath": str(logs / (name + ".log")),
            "Umask": 0o077})
    else:
        directory = Path.home() / ".config/systemd/user"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (spec["label"] + ".service")
        lines = ["[Unit]", "Description=BORG " + name, "[Service]", "Type=simple",
                 "WorkingDirectory=" + json.dumps(spec["cwd"]),
                 "ExecStart=" + " ".join(json.dumps(arg) for arg in spec["args"]),
                 *["Environment=" + json.dumps(k + "=" + v) for k, v in spec["env"].items()],
                 "Restart=on-failure", "RestartSec=15", "TimeoutStopSec=10", "UMask=0077",
                 "[Install]", "WantedBy=default.target"]
        body = ("\n".join(lines) + "\n").encode()
    if target.is_symlink():
        raise ValueError("BORG service definition cannot replace a symlink")
    # Service definitions contain paths/configuration, never bearer credentials.
    from installer.config import write_private
    if platform.system() == "Darwin":
        content = body.decode()
    else:
        content = body.decode()
    write_private(target, content, replace=target.exists())
    (root / "services" / target.name).write_bytes(body)
    return target


def start(doc: dict, components: list[str] | None = None) -> dict:
    rows = specifications(doc)
    chosen = components or list(rows)
    if any(name not in rows for name in chosen):
        raise ValueError("A requested service is unselected or has no installed service contract")
    for name in chosen:
        spec = rows[name]
        if running(doc, name):
            continue
        if "port" in spec:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", spec["port"])) == 0:
                    raise RuntimeError(f"Port {spec['port']} is occupied outside this BORG service: {name}")
        target = install_definition(doc, name, spec)
        if platform.system() == "Darwin":
            domain = f"gui/{os.getuid()}"
            # A registered, stopped service may have an older environment.
            # Reload this installation's exact label before bootstrapping it.
            check = subprocess.run(["launchctl", "print", domain + "/" + spec["label"]],
                                   capture_output=True, timeout=10)
            if check.returncode == 0:
                subprocess.run(["launchctl", "bootout", domain + "/" + spec["label"]], check=True, timeout=20)
            command = ["launchctl", "bootstrap", domain, str(target)]
        else:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=10)
            command = ["systemctl", "--user", "enable", "--now", target.name]
        subprocess.run(command, check=True, timeout=20, stdout=subprocess.DEVNULL)
    return {name: "running" if running(doc, name) else "starting" for name in chosen}


def stop(doc: dict, components: list[str] | None = None) -> None:
    from installer.blueprint import service_names
    chosen = components or list(specifications(doc))
    if any(name not in service_names(doc) for name in chosen):
        raise ValueError("A requested service is unselected or has no installed service contract")
    for name in reversed(chosen):
        target = label(doc, name)
        command = (["launchctl", "bootout", f"gui/{os.getuid()}/{target}"] if platform.system() == "Darwin"
                   else ["systemctl", "--user", "disable", "--now", target + ".service"])
        result = subprocess.run(command, capture_output=True, timeout=20)
        deadline = time.monotonic() + 15
        while running(doc, name) and time.monotonic() < deadline:
            time.sleep(0.2)
        if running(doc, name):
            raise RuntimeError("BORG service did not stop: " + name)
        # An already unloaded/inactive service is an idempotent stop. A failed
        # stop must never be reported as successful while its PID remains live.
