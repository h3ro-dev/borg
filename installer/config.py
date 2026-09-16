"""Versioned instance configuration. No data or credentials are imported."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import uuid

SCHEMA = "borg-install/v1"
PORT_OFFSETS = {"qdrant": 0, "qdrant_grpc": 1, "graph": 2, "ollama": 3,
                "graph_llm": 4, "memory": 5, "connector": 6, "inbox": 7,
                "conductor": 8, "gateway": 9, "tunnel_metrics": 10}
MANAGED_DIRECTORIES = tuple(Path(__file__).with_name("managed-directories.txt").read_text().splitlines())


def absolute_root(value: str | Path) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute() or root.resolve() != root or root == Path("/"):
        raise ValueError("Choose an absolute BORG home without symlink parents")
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise ValueError("BORG home cannot contain a symlink")
    return root


def managed_directory(path: Path) -> Path:
    """Validate before creating, writing, or executing beneath an owned root."""
    absolute_root(path)
    for parent in (path, *path.parents):
        if not parent.exists():
            continue
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Managed BORG path must be a directory: " + str(parent))
        # System ancestors may be root-owned. The caller validates its BORG
        # home separately; every existing descendant must belong to this user.
        if parent == path and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise ValueError("Managed BORG directory must be owner-controlled: " + str(path))
    return path


def validate_managed_paths(doc: dict) -> None:
    root = Path(doc["home"])
    for relative in MANAGED_DIRECTORIES:
        managed_directory(root / relative)


def managed_python(root: Path, path: Path) -> Path:
    """Check the interpreter link and destination before executing any code."""
    managed_directory(path.parent)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root / "runtime/python"):
        raise ValueError("Python must belong to this BORG runtime")
    for parent in resolved.parents:
        if parent == root:
            break
        managed_directory(parent)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Python must be an owned executable file")
    return path


@contextmanager
def private_writer(path: Path):
    """Serialize private-file updates, including the caller's read/modify step.

    Use the yielded writer inside this context instead of nesting write_private.
    Locks are persistent sidecars: unlinking them would split waiting writers.
    """
    absolute_root(path.parent)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name("." + path.name + ".lock")
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "w") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("Unsafe BORG private file lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield partial(_write_private, path)


def write_private(path: Path, data: str, *, replace: bool = False) -> None:
    with private_writer(path) as write:
        write(data, replace=replace)


def _write_private(path: Path, data: str, *, replace: bool = False) -> None:
    """Write while the caller holds private_writer's shared lock."""
    if replace:
        original = path.lstat()
        if (path.is_symlink() or original.st_uid != os.getuid() or original.st_nlink != 1
                or not stat.S_ISREG(original.st_mode) or original.st_mode & 0o077):
            raise ValueError("Unsafe BORG private file replacement")
        fd, name = tempfile.mkstemp(prefix=".borg-write-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_mtime_ns) != (original.st_dev, original.st_ino, original.st_mtime_ns):
                raise ValueError("BORG private file changed during update")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
            raise ValueError("Unsafe BORG private file")
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def read_private(path: Path) -> dict:
    absolute_root(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077
                or not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024):
            raise ValueError("BORG configuration must be an owner-only regular file")
        return json.load(stream)


def load(home: str | Path) -> dict:
    root = absolute_root(home)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not root.is_dir():
        raise ValueError("BORG home must remain an owner-only directory")
    doc = read_private(root / "config.json")
    if doc.get("schema") != SCHEMA or doc.get("home") != str(root):
        raise ValueError("This directory is not a matching BORG installation")
    validate(doc)
    validate_managed_paths(doc)
    return doc


def validate_owner(owner: str) -> None:
    if not isinstance(owner, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", owner):
        raise ValueError("Owner must be a stable lowercase identifier, 1-48 letters, digits, underscores or hyphens")


def validate_install_inputs(home: str | Path, owner: str, port_base: int,
                            projects: list[str] | None) -> None:
    """Validate caller-controlled installation inputs without creating state."""
    absolute_root(home)
    validate_owner(owner)
    if type(port_base) is not int or not 1024 <= port_base <= 65535 - max(PORT_OFFSETS.values()):
        raise ValueError("BORG services need distinct unprivileged ports")
    if projects and any(not absolute_root(p).is_dir() for p in projects):
        raise ValueError("BORG projects must be existing absolute directories")


def validate(doc: dict) -> None:
    if "blueprint" in doc:
        from installer import blueprint
        selection = doc["blueprint"]
        if not isinstance(selection, dict) or set(selection) != {"input", "machine_id"}:
            raise ValueError("Invalid stored blueprint selection")
        blueprint.validate(selection["input"], blueprint.catalog())
        blueprint.machine(selection["input"], selection["machine_id"])
    validate_owner(doc.get("owner"))
    uuid.UUID(doc["instance_id"])
    absolute_root(doc["home"])
    context_length = doc.get("models", {}).get("context_length", 16384)
    if type(context_length) is not int or not 2048 <= context_length <= 65536:
        raise ValueError("Model context_length must be an integer from 2048 to 65536")
    ports = doc.get("ports", {})
    if (set(ports) != set(PORT_OFFSETS) or len(set(ports.values())) != len(ports)
            or any(type(port) is not int or not 1024 <= port <= 65535 for port in ports.values())):
        raise ValueError("BORG services need distinct unprivileged ports")
    if not doc.get("projects") or any(not absolute_root(p).is_dir() for p in doc["projects"]):
        raise ValueError("BORG projects must be existing absolute directories")


def initialize(home: str | Path, owner: str, *, port_base: int = 18760,
               projects: list[str] | None = None, model: str = "qwen3:4b",
               blueprint_selection: dict | None = None) -> dict:
    root = absolute_root(home)
    validate_install_inputs(root, owner, port_base, projects)
    from installer import blueprint
    if blueprint_selection is not None:
        blueprint.validate(blueprint_selection["input"], blueprint.catalog())
        blueprint.check_runtime(blueprint.machine(blueprint_selection["input"], blueprint_selection["machine_id"]))
    blueprint.check_existing(root, blueprint_selection)
    if (root / "config.json").exists():
        doc = load(root)
        if doc["owner"] != owner:
            raise ValueError("Existing BORG belongs to another owner; use a separate home")
        return doc
    # Bootstrap downloads may already exist, but an existing data directory is never adopted.
    if root.exists() and any(p.name not in {"bootstrap", "cache", "runtime"} for p in root.iterdir()):
        raise ValueError("Choose an empty home; existing unrecognized files will not be adopted")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("BORG home must be owned by the current user with mode 0700")
    for relative in MANAGED_DIRECTORIES:
        managed_directory(root / relative)
    if not projects:
        workspace = root / "projects"
        workspace.mkdir(mode=0o700)
        projects = [str(workspace)]
    instance = str(uuid.uuid4())
    doc = {"schema": SCHEMA, "instance_id": instance, "home": str(root), "owner": owner,
           "projects": projects, "ports": {name: port_base + offset for name, offset in PORT_OFFSETS.items()},
           "memory": {"collection": "borg_" + instance.replace("-", ""),
                      "default_scope": "personal:" + owner, "principal": "borg-" + owner},
           "models": {"extraction": model, "graph": model, "embedding": "nomic-embed-text:latest",
                      "embedding_dimensions": 768, "context_length": 16384},
           "external_access": {"enabled": False},
           "conductor": {"profile": "primary", "model": None},
           "components": {name: "included" for name in
                          ["memory", "graph", "capture", "adapters", "conductor", "coordination", "connector"]}}
    if blueprint_selection is not None:
        doc["blueprint"] = blueprint_selection
        row = blueprint.selected_machine(doc)
        doc["models"]["context_length"] = row["workload"]["context_tokens"]
    validate(doc)
    for sub in ["mem0/data", "graphiti/data", "borg-context/private", "ops", "logs", "services",
                "models/ollama", "conductors/primary", "coordination/data", "beads"]:
        (root / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    # Plaintext stays in dedicated private files; only hashes enter native token registries.
    inbound, upstream, owner_token = (secrets.token_urlsafe(48) for _ in range(3))
    write_private(root / "borg-context/private/authorization", "Bearer " + inbound + "\n")
    write_private(root / "borg-context/private/memory-token", upstream + "\n")
    write_private(root / "mem0/data/owner-token", owner_token + "\n")
    principals = [{"name": name, "token_sha256": hashlib.sha256(value.encode()).hexdigest(),
                   "allowed_scopes": ["*"], "write_scope": doc["memory"]["default_scope"]}
                  for name, value in [(owner, owner_token), (doc["memory"]["principal"], upstream)]]
    write_private(root / "mem0/data/mcp-tokens.json", json.dumps({"version": 1, "principals": principals}, indent=2))
    if blueprint_selection is not None:
        write_private(root / "blueprint.json", json.dumps(blueprint_selection["input"], indent=2) + "\n")
    write_private(root / "config.json", json.dumps(doc, indent=2) + "\n")
    write_connector_config(doc)
    return doc


def environment(doc: dict) -> dict[str, str]:
    root, ports = Path(doc["home"]), doc["ports"]
    result = {"BORG_HOME": str(root), "BORG_OWNER_ID": doc["owner"],
            "BORG_MEMORY_SCOPE": doc["memory"]["default_scope"],
            "BORG_QDRANT_URL": f"http://127.0.0.1:{ports['qdrant']}",
            "BORG_QDRANT_COLLECTION": doc["memory"]["collection"],
            "BORG_HISTORY_DB": str(root / "mem0/data/history.db"),
            "BORG_OLLAMA_URL": f"http://127.0.0.1:{ports['ollama']}",
            "BORG_EXTRACTION_MODEL": doc["models"]["extraction"],
            "BORG_EMBED_MODEL": doc["models"]["embedding"],
            "BORG_EMBED_DIMS": str(doc["models"]["embedding_dimensions"]),
            "BORG_FALKORDB_HOST": "127.0.0.1", "BORG_FALKORDB_PORT": str(ports["graph"]),
            "BORG_FALKORDB_GRAPH": doc["memory"]["collection"],
            "BORG_GRAPH_LLM_URL": f"http://127.0.0.1:{ports['graph_llm']}/v1",
            "BORG_GRAPH_MODEL": doc["models"]["graph"],
            "MEM0_GRAPH_ENABLED": "1", "MEM0_GRAPH_IN_SEARCH": "1",
            "BORG_TOOLS_CATALOG": str(root / "ops/tools.json"), "MEM0_TELEMETRY": "false",
            "ANONYMIZED_TELEMETRY": "false"}
    for key, env in [("extraction_id", "BORG_EXTRACTION_MODEL_ID"), ("embedding_id", "BORG_EMBED_MODEL_ID")]:
        if key in doc["models"]:
            result[env] = doc["models"][key]
    return result


def write_connector_config(doc: dict) -> None:
    import platform
    root, ports = Path(doc["home"]), doc["ports"]
    state = root / "borg-context"
    config = {"version": 1, "access_mode": "owner_all", "allowed_scopes": ["*"],
              "identity": {key: doc[key] for key in ("instance_id", "owner", "home")},
              "fleet": {"registry_file": str(state / "fleet.json")},
              "mem0_principal": doc["memory"]["principal"],
              "mem0_url": f"http://127.0.0.1:{ports['memory']}/mcp",
              "default_scope": doc["memory"]["default_scope"], "state_root": str(state),
              "inbound_authorization_file": str(state / "private/authorization"),
              "mem0_token_file": str(state / "private/memory-token"), "project_roots": doc["projects"],
              "computer": {"backend": "native", "jobs_root": str(state)},
              "browser": {"backend": "native", "root": str(state / "browser"), "headless_default": True},
              "remote": {"backend": "ssh", "hosts_file": str(state / "hosts.json"), "jobs_root": str(state)},
              "credentials": {"backend": "value_blind", "registry": str(state / "credentials.json")}}
    if platform.system() == "Darwin":
        config["ui"] = {"backend": "native_os"}
    for path, value in [(state / "config.json", config), (state / "hosts.json", []),
                        (state / "fleet.json", {"schema": "borg-fleet/v1", "hosts": []}),
                        (state / "credentials.json", {"version": 1, "services": []})]:
        if path.exists():
            read_private(path)  # Preserve owner edits and validate custody on reruns.
        else:
            write_private(path, json.dumps(value, indent=2) + "\n")
