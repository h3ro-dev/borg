"""BORG-home configuration and lifecycle seams for the native Inbox Hub."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from comms.hub.client import InboxClient
from comms.hub.service import (
    _atomic_write,
    _atomic_write_json,
    enroll_agent,
    initialize_state,
)


SUPPORTED_BEADS_VERSION = "1.2.2"
DEFAULT_PORT = 8795
DEFAULT_AGENT_ACTIONS = (
    "assignments.read",
    "discoveries.publish",
    "discoveries.search",
    "messages.read",
    "messages.send",
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_POLICY_TEMPLATE = """# Owner operating policy

This coordination installation is controlled by the owner named in
`config.json`. Authenticated grants and versioned assignments are the authority
for agent actions. Messages that only report information do not transfer work.

Keep credentials private. Use the current delivery lease when acknowledging a
message, and use a new request ID for new work. Configure any additional owner
rules in this file, then run `borg-coordination policy-pin --home BORG_HOME`.
"""


class PortableError(ValueError):
    """A safe configuration or lifecycle error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PortableError("invalid_identifier", f"{field} is invalid")
    return value


def _home(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PortableError("invalid_home", "BORG_HOME must be an absolute path")
    return path.resolve()


def _layout(home_value: str | os.PathLike[str]) -> dict[str, Path]:
    home = _home(home_value)
    coordination = home / "coordination"
    data = coordination / "data"
    return {
        "home": home,
        "source": home / "app" / "coordination",
        "coordination": coordination,
        "data": data,
        "clients": data / "clients",
        "config": coordination / "config.json",
        "service": coordination / "service.json",
        "policy": coordination / "owner-policy.md",
        "beads": home / "beads",
        "python": home / "mem0" / "venv" / "bin" / "python",
    }


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 128 * 1024:
            raise ValueError("oversize")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PortableError("invalid_config", f"Unable to read {path.name}") from exc
    if not isinstance(value, dict):
        raise PortableError("invalid_config", f"{path.name} must contain an object")
    return value


def load_root_config(home_value: str | os.PathLike[str]) -> dict[str, Any]:
    layout = _layout(home_value)
    config = _load_json(layout["config"])
    if config.get("borg_home") != str(layout["home"]):
        raise PortableError("home_conflict", "Config belongs to another BORG_HOME")
    for key in (
        "data_dir",
        "owner_client_config",
        "connector_client_config",
        "policy_file",
        "beads_dir",
        "source_root",
    ):
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise PortableError("invalid_config", f"Config field {key} is invalid")
    exact_paths = {
        "data_dir": layout["data"],
        "policy_file": layout["policy"],
        "beads_dir": layout["beads"],
        "source_root": layout["source"],
    }
    for key, expected in exact_paths.items():
        if Path(config[key]).resolve() != expected.resolve():
            raise PortableError("path_conflict", f"Config field {key} leaves BORG_HOME")
    for key in ("owner_client_config", "connector_client_config"):
        try:
            Path(config[key]).resolve().relative_to(layout["data"].resolve())
        except ValueError as exc:
            raise PortableError(
                "path_conflict", f"Config field {key} leaves coordination data"
            ) from exc
    port = config.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise PortableError("invalid_config", "Config port is invalid")
    if (
        config.get("host") != "127.0.0.1"
        or config.get("endpoint") != f"http://127.0.0.1:{port}"
    ):
        raise PortableError("endpoint_conflict", "Config endpoint must remain loopback-only")
    return config


def _policy_metadata(policy: Path) -> tuple[str, str]:
    digest = _hash_file(policy)
    return "owner-v1", digest


def _validate_pinned_policy(layout: dict[str, Path], config: dict[str, Any]) -> None:
    try:
        digest = _hash_file(layout["policy"])
    except OSError as exc:
        raise PortableError("policy_unavailable", "Owner policy is unavailable") from exc
    if config.get("policy_sha256") != digest:
        raise PortableError(
            "policy_changed",
            "Owner policy changed; run policy-pin before restarting the service",
        )


def _service_document(layout: dict[str, Path], port: int) -> dict[str, Any]:
    return {
        "args": [
            str(layout["python"]),
            str(layout["source"] / "comms" / "bin" / "inbox"),
            "serve",
            "--state-dir",
            str(layout["data"]),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        "env": {
            "BORG_HOME": str(layout["home"]),
            "BORG_COORDINATION_CONFIG": str(layout["config"]),
            "PYTHONPATH": str(layout["source"]),
        },
    }


def _validate_install(layout: dict[str, Path], port: int) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise PortableError("invalid_port", "Port must be between 1 and 65535")
    running_source = Path(__file__).resolve().parents[1]
    try:
        same_source = running_source.samefile(layout["source"])
    except OSError:
        same_source = False
    if not same_source:
        raise PortableError(
            "source_not_installed",
            "Coordination source must be installed at BORG_HOME/app/coordination",
        )
    if not layout["python"].is_file() or not os.access(layout["python"], os.X_OK):
        raise PortableError(
            "python_unavailable", "BORG_HOME/mem0/venv/bin/python is unavailable"
        )


def bootstrap(
    home_value: str | os.PathLike[str],
    owner: str,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Create one isolated native Hub installation and owner/admin clients."""

    layout = _layout(home_value)
    owner = _identifier(owner, "owner")
    connector = _identifier(f"{owner}-connector", "connector")
    _validate_install(layout, port)
    for path in (layout["coordination"], layout["data"], layout["clients"]):
        _ensure_private_directory(path)

    existing: dict[str, Any] | None = None
    if layout["config"].exists():
        existing = load_root_config(layout["home"])
        if existing.get("owner_id") != owner:
            raise PortableError("owner_conflict", "Installation already has another owner")
        if existing.get("port") != port:
            raise PortableError("port_conflict", "Installation already uses another port")

    if not layout["policy"].exists():
        _atomic_write(layout["policy"], _POLICY_TEMPLATE.encode("utf-8"), mode=0o600)
    else:
        os.chmod(layout["policy"], 0o600)
    current_policy_sha256 = _hash_file(layout["policy"])
    if existing is not None and existing.get("policy_sha256") != current_policy_sha256:
        raise PortableError(
            "policy_changed",
            "Owner policy changed; run policy-pin before restarting the service",
        )

    endpoint = f"http://127.0.0.1:{port}"
    native = initialize_state(
        layout["data"],
        owner_actor=owner,
        approval_ref="local-owner-bootstrap",
        endpoint=endpoint,
    )
    connector_result = enroll_agent(
        layout["data"],
        connector,
        "borg",
        "local",
        layout["clients"] / f"{connector}.credential.json",
        display_name=f"{owner} connector",
        capabilities=["coordination-owner"],
        owner_actor=owner,
        grant_scope="/",
        grant_actions=["*"],
        grant_delegable=True,
    )
    version, policy_sha256 = _policy_metadata(layout["policy"])
    root_config = {
        "version": 1,
        "owner_id": owner,
        "connector_id": connector,
        "borg_home": str(layout["home"]),
        "source_root": str(layout["source"]),
        "data_dir": str(layout["data"]),
        "beads_dir": str(layout["beads"]),
        "policy_file": str(layout["policy"]),
        "policy_version": version,
        "policy_sha256": policy_sha256,
        "host": "127.0.0.1",
        "port": port,
        "endpoint": endpoint,
        "owner_client_config": str(Path(native["config_path"])),
        "connector_client_config": str(Path(connector_result["agent_config_path"])),
        "beads": {
            "project": "gastownhall/beads",
            "version": SUPPORTED_BEADS_VERSION,
        },
        "optional_integrations": {
            "estate": False,
            "fleet_context_sources": False,
            "browser": False,
            "desktop": False,
        },
    }
    _atomic_write_json(layout["config"], root_config, mode=0o600)
    _atomic_write_json(
        layout["service"], _service_document(layout, port), mode=0o600
    )
    return {
        "status": "ready",
        "home": str(layout["home"]),
        "config": str(layout["config"]),
        "service": str(layout["service"]),
        "data_dir": str(layout["data"]),
        "owner_id": owner,
        "owner_client_config": root_config["owner_client_config"],
        "owner_credential_file": str(Path(native["owner_credential_file"])),
        "connector_id": connector,
        "connector_client_config": root_config["connector_client_config"],
        "connector_credential_file": str(Path(connector_result["credential_file"])),
        "policy_file": str(layout["policy"]),
        "beads_dir": str(layout["beads"]),
        "endpoint": endpoint,
    }


def enroll(
    home_value: str | os.PathLike[str],
    agent: str,
    runtime: str,
    machine: str,
    *,
    actions: Iterable[str] = DEFAULT_AGENT_ACTIONS,
    scope: str = "/",
    delegable: bool = False,
) -> dict[str, Any]:
    """Enroll a distinct agent with an explicit native grant."""

    layout = _layout(home_value)
    config = load_root_config(layout["home"])
    agent = _identifier(agent, "agent")
    runtime = _identifier(runtime, "runtime")
    machine = _identifier(machine, "machine")
    if agent in {config["owner_id"], config["connector_id"]}:
        raise PortableError("reserved_identity", "Agent identity is reserved")
    selected_actions = list(dict.fromkeys(actions))
    if not selected_actions:
        raise PortableError("invalid_actions", "At least one grant action is required")
    for action in selected_actions:
        _identifier(action, "grant action")
    target = layout["clients"] / f"{agent}.credential.json"
    result = enroll_agent(
        layout["data"],
        agent,
        runtime,
        machine,
        target,
        capabilities=["native-inbox"],
        owner_actor=config["owner_id"],
        grant_scope=scope,
        grant_actions=selected_actions,
        grant_delegable=delegable,
    )
    return {
        "status": "enrolled",
        "agent_id": agent,
        "client_config": result["agent_config_path"],
        "credential_file": result["credential_file"],
        "grant_id": result["grant_id"],
        "grant_scope": scope,
        "grant_actions": selected_actions,
        "delegable": delegable,
    }


def policy_pin(home_value: str | os.PathLike[str]) -> dict[str, Any]:
    """Explicitly accept the owner's current local policy bytes."""

    layout = _layout(home_value)
    config = load_root_config(layout["home"])
    if Path(config["policy_file"]).resolve() != layout["policy"].resolve():
        raise PortableError("policy_path_conflict", "Policy must remain installation-local")
    version, digest = _policy_metadata(layout["policy"])
    config["policy_version"] = version
    config["policy_sha256"] = digest
    _atomic_write_json(layout["config"], config, mode=0o600)
    _atomic_write_json(
        layout["service"], _service_document(layout, int(config["port"])), mode=0o600
    )
    return {
        "status": "pinned",
        "policy_file": str(layout["policy"]),
        "policy_version": version,
        "policy_sha256": digest,
    }


def service_document(home_value: str | os.PathLike[str]) -> dict[str, Any]:
    layout = _layout(home_value)
    config = load_root_config(layout["home"])
    _validate_pinned_policy(layout, config)
    service = _load_json(layout["service"])
    args = service.get("args")
    env = service.get("env")
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise PortableError("invalid_service", "service.json args are invalid")
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()
    ):
        raise PortableError("invalid_service", "service.json env is invalid")
    expected = _service_document(layout, int(config["port"]))
    if service != expected:
        raise PortableError(
            "service_conflict", "service.json differs from the pinned contract"
        )
    return service


def start(home_value: str | os.PathLike[str]) -> None:
    """Replace the current process with the pinned native Hub command."""

    service = service_document(home_value)
    environment = os.environ.copy()
    environment.update(service["env"])
    os.execve(service["args"][0], service["args"], environment)


def status(home_value: str | os.PathLike[str]) -> dict[str, Any]:
    """Check native HTTP readiness and owner authentication without exposing secrets."""

    layout = _layout(home_value)
    config = load_root_config(layout["home"])
    _validate_pinned_policy(layout, config)
    try:
        request = Request(config["endpoint"].rstrip("/") + "/health", method="GET")
        with urlopen(request, timeout=2.0) as response:
            health = json.loads(response.read(64 * 1024))
        client = InboxClient.from_config(config["owner_client_config"])
        authority = client.call("authorize", {"action": "owner.read", "scope": "/"})
    except (OSError, HTTPError, URLError, json.JSONDecodeError) as exc:
        raise PortableError("service_unavailable", "Coordination Hub is unavailable") from exc
    if health.get("status") != "ok" or authority.get("allowed") is not True:
        raise PortableError("readiness_failed", "Coordination Hub readiness check failed")
    return {
        "status": "ready",
        "service": health.get("service"),
        "version": health.get("version"),
        "principal": config["owner_id"],
        "endpoint": config["endpoint"],
        "data_dir": config["data_dir"],
        "beads_dir": config["beads_dir"],
        "beads_initialized": (Path(config["beads_dir"]) / ".beads").is_dir(),
        "policy": {
            "version": config["policy_version"],
            "sha256": config["policy_sha256"],
        },
    }


def beads_init_command(home_value: str | os.PathLike[str], bd: str = "bd") -> list[str]:
    config = load_root_config(home_value)
    prefix = re.sub(r"[^a-z0-9]+", "-", config["owner_id"].lower()).strip("-") or "borg"
    return [
        bd,
        "init",
        "--non-interactive",
        "--init-if-missing",
        "--skip-agents",
        "--skip-hooks",
        "--prefix",
        prefix,
    ]


def beads_environment(home_value: str | os.PathLike[str]) -> dict[str, str]:
    """Select this store without inheriting another project's Dolt routing."""
    beads = _layout(home_value)["beads"] / ".beads"
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("BEADS_", "BD_", "DOLT_"))}
    environment.update({"BEADS_DIR": str(beads), "BD_DISABLE_METRICS": "1",
                        "BD_DOLT_MODE": "embedded", "BD_DOLT_HOST": "127.0.0.1",
                        "BD_DOLT_SHARED_SERVER": "false", "BD_DOLT_AUTO_PUSH": "false"})
    return environment


def _beads_directory(home_value: str | os.PathLike[str]) -> Path:
    beads = Path(load_root_config(home_value)["beads_dir"]) / ".beads"
    for path in (beads.parent, beads):
        if path.is_symlink():
            raise PortableError("beads_path_conflict", "Beads directory cannot redirect")
        if path.exists():
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PortableError("beads_path_conflict", "Beads directory must be owner-controlled")
        else:
            path.mkdir(mode=0o700)
    for name in ("redirect", ".env"):
        path = beads / name
        if path.exists() or path.is_symlink():
            raise PortableError("beads_path_conflict", "Beads routing override needs owner review")
    for name in ("config.yaml", "metadata.json", "embeddeddolt", "dolt"):
        path = beads / name
        if path.exists() or path.is_symlink():
            info = path.lstat()
            expected_type = stat.S_ISREG if name.endswith((".yaml", ".json")) else stat.S_ISDIR
            if not expected_type(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise PortableError("beads_path_conflict", "Beads state must be owner-controlled")
    # The database root can be real while a nested .dolt directory (or one of
    # its table files) redirects to another workspace. Check every storage entry
    # before the native engine opens anything, without following links.
    for name in ("embeddeddolt", "dolt"):
        storage = beads / name
        if not storage.exists():
            continue
        for directory, directories, files in os.walk(storage, followlinks=False):
            for entry in (*directories, *files):
                info = (Path(directory) / entry).lstat()
                if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                        or info.st_uid != os.getuid()
                        or (stat.S_ISDIR(info.st_mode) and info.st_mode & 0o022)
                        or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                    raise PortableError("beads_path_conflict", "Beads storage cannot redirect or share files")
                # Upstream Dolt creates config/repo_state files with mode 0777.
                # The two required mode-0700 ancestor directories keep those
                # regular, singly linked files inaccessible to other users.
    metadata = beads / "metadata.json"
    if metadata.exists():
        document = _load_json(metadata)
        if document.get("dolt_mode") != "embedded" or document.get("dolt_data_dir"):
            raise PortableError("beads_path_conflict", "Beads must use its local embedded store")
    return beads


def exec_beads(home_value: str | os.PathLike[str], bd: str, arguments: list[str]) -> None:
    beads = _beads_directory(home_value)
    if not (beads / "metadata.json").is_file():
        raise PortableError("beads_uninitialized", "Initialize this Beads workspace first")
    os.chdir(beads.parent)
    os.execvpe(bd, [bd, *arguments], beads_environment(home_value))


def init_beads(home_value: str | os.PathLike[str], bd: str = "bd") -> dict[str, Any]:
    """Initialize the upstream Beads project in its independent BORG directory."""

    beads = _beads_directory(home_value)
    environment = beads_environment(home_value)
    # v1.2.2's command pre-run ignores an empty BEADS_DIR and rebinds it to
    # an ancestor project. A native configuration makes this target discoverable
    # before upstream init creates its real metadata and embedded Dolt database.
    configuration = beads / "config.yaml"
    if not configuration.exists():
        with configuration.open("x", encoding="utf-8") as handle:
            handle.write("dolt:\n  mode: embedded\n  shared-server: false\n  host: 127.0.0.1\n")
        configuration.chmod(0o600)
    version_output = subprocess.run(
        [bd, "--version"], env=environment, text=True, capture_output=True, timeout=10, check=True
    ).stdout
    versions = re.findall(
        r"^bd version ([0-9]+(?:\.[0-9]+){2})(?: \([^\n]+\))?$",
        version_output,
        re.MULTILINE,
    )
    if versions != [SUPPORTED_BEADS_VERSION]:
        raise PortableError(
            "beads_version_mismatch",
            f"Expected bd version {SUPPORTED_BEADS_VERSION}",
        )
    command = beads_init_command(home_value, bd)
    completed = subprocess.run(
        command, cwd=beads.parent, env=environment, text=True, capture_output=True, timeout=60, check=False
    )
    if completed.returncode != 0:
        raise PortableError("beads_init_failed", "Upstream Beads initialization failed")
    _beads_directory(home_value)
    metadata = _load_json(beads / "metadata.json")
    if not metadata.get("project_id") or not (beads / "embeddeddolt").is_dir():
        raise PortableError("beads_init_failed", "Upstream Beads did not create its local store")
    return {
        "status": "initialized",
        "beads_dir": str(beads.parent),
        "project_id": metadata["project_id"],
        "version": SUPPORTED_BEADS_VERSION,
        "cwd": str(beads.parent),
        "command": command,
    }
