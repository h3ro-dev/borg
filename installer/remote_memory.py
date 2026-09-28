"""Explicit, instance-owned remote Mem0 lifecycle client for tools nodes.

The default tools blueprint remains hook-free. This module stages a new badge
under the BORG home, verifies its Hub identity, then delegates every Codex
config/trust mutation to the bundled native configurator.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import pwd
import re
import secrets
import stat
import subprocess
from urllib.parse import urlsplit

from installer import blueprint, config, services


SCHEMA = "borg-memory-client/v1"
SOURCE_ROOT = Path(__file__).absolute().parent.parent
# hooks/list `eventName` and the configurator's trust receipt use these
# lower-camel native values, even though config.toml keys use PascalCase.
EVENTS = {"sessionStart", "userPromptSubmit", "stop", "sessionEnd"}
MAX_OUTPUT = 1024 * 1024
MACHINE = re.compile(r"[a-z][a-z0-9-]{0,79}\Z")
PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class NativeRefusal(RuntimeError):
    """A native Codex operation refused; its raw output must not escape."""


class NativeUncertain(NativeRefusal):
    """A timed-out native operation may have committed; never replay it here."""


def _paths(doc: dict) -> dict[str, Path]:
    root = Path(doc["home"])
    staged = (SOURCE_ROOT.parent == root / "tools/releases"
              and re.fullmatch(r"[0-9a-f]{40}", SOURCE_ROOT.name) is not None)
    installed_curl = root / "mem0/bin/mem0-mcp-curl"
    installed_driver = root / "mem0/bin/mem0-fleet-hook"
    return {"root": root, "data": root / "mem0/data",
            "manifest": root / "mem0/data/remote-client.json",
            "token": root / "mem0/data/fleet-token",
            "endpoint": root / "mem0/data/fleet-endpoint",
            "profile": root / "conductors/primary/profile",
            "config": root / "conductors/primary/profile/config.toml",
            "helper": SOURCE_ROOT / "memory/bin/mem0-fleet-configure",
            "curl": SOURCE_ROOT / "memory/bin/mem0-mcp-curl" if staged else installed_curl,
            "driver": SOURCE_ROOT / "memory/bin/mem0-fleet-hook" if staged else installed_driver,
            "installed_curl": installed_curl, "installed_driver": installed_driver,
            "source_curl": SOURCE_ROOT / "memory/bin/mem0-mcp-curl",
            "source_driver": SOURCE_ROOT / "memory/bin/mem0-fleet-hook",
            "codex": root / "runtime/npm/node_modules/.bin/codex"}


def _owned_regular(path: Path, *, private: bool = False, limit: int = 1024 * 1024) -> bytes:
    config.absolute_root(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & (0o077 if private else 0o022)
                or info.st_size > limit):
            raise ValueError("BORG memory client path is not an owned regular file")
        return handle.read(limit + 1)


def _url(raw: str) -> str:
    if not isinstance(raw, str) or raw != raw.strip() or re.search(r"\s", raw):
        raise ValueError("Memory endpoint must be an explicit credential-free HTTPS MCP URL")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("Invalid memory endpoint") from exc
    if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
            or parsed.path != "/mcp" or parsed.query or parsed.fragment or not parsed.netloc):
        raise ValueError("Memory endpoint must be an explicit credential-free HTTPS MCP URL")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Invalid memory endpoint port") from exc
    return raw


def _verify_source(doc: dict) -> None:
    """Accept only this home's intact app or a complete owner-staged release."""
    root = Path(doc["home"])
    source = config.absolute_root(SOURCE_ROOT)
    if source == root / "app":
        from installer import installation
        if Path(installation.__file__).absolute().parent.parent != source:
            raise ValueError("BORG installed source identity differs")
        installation.preflight(doc)
        return
    release_parent = root / "tools/releases"
    if source.parent != release_parent or not re.fullmatch(r"[0-9a-f]{40}", source.name):
        raise ValueError("Memory client source must be this BORG home's reviewed release")
    for directory in (root / "tools", release_parent, source):
        config.managed_directory(directory)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("Memory client release directory is not owner-controlled")
    from installer import release_guard
    inventory = source / "RELEASE-INVENTORY.json"
    allowlist = source / "RELEASE-ALLOWLIST.json"
    _owned_regular(inventory, limit=128 * 1024)
    _owned_regular(allowlist, limit=128 * 1024)
    try:
        approved = release_guard._load_inventory(inventory)
        if approved.get("excluded_self") != "RELEASE-INVENTORY.json":
            raise ValueError("Memory client release inventory has no exact self-exclusion")
        files, findings = release_guard.scan(source, skip={"RELEASE-INVENTORY.json"},
                                             allowlist_path=allowlist)
        findings += release_guard._compare_inventory(files, approved)
    except release_guard.GuardConfigurationError:
        raise ValueError("Memory client release inventory is invalid") from None
    if findings:
        raise ValueError("Memory client release failed its exact source inventory")
    expected = {"borg.py", "installer/cli.py", "installer/remote_memory.py",
                "memory/bin/borg_client_config.py", "memory/bin/mem0-fleet-configure", "memory/bin/mem0-fleet-hook",
                "memory/bin/mem0-mcp-curl"}
    if not expected <= {str(row["path"]) for row in files}:
        raise ValueError("Memory client release is missing a required source file")
    for entry in source.rglob("*"):
        info = entry.lstat()
        if (info.st_uid != os.getuid() or info.st_mode & 0o022
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise ValueError("Memory client release ownership differs")


def _verify_installed_sidecars(paths: dict[str, Path]) -> None:
    """A source-only client never overwrites the immutable installed app."""
    app = paths["root"] / "app"
    config.managed_directory(app)
    info = app.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Installed BORG app directory differs")
    try:
        manifest = json.loads(_owned_regular(app / "source-manifest.json", limit=128 * 1024))
    except (ValueError, TypeError):
        raise ValueError("Installed BORG app manifest is invalid") from None
    if not isinstance(manifest, dict):
        raise ValueError("Installed BORG app manifest is invalid")
    for name in ("curl", "driver"):
        relative = "memory/bin/mem0-mcp-curl" if name == "curl" else "memory/bin/mem0-fleet-hook"
        expected = manifest.get(relative)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Installed memory sidecar manifest is incomplete")
        for path in (app / relative, paths["installed_" + name]):
            if hashlib.sha256(_owned_regular(path, limit=2 * 1024 * 1024)).hexdigest() != expected:
                raise ValueError("Installed memory sidecar differs from its app manifest")


def _identity(doc: dict) -> dict[str, Path]:
    paths = _paths(doc)
    root = paths["root"]
    config.absolute_root(root)
    if not doc.get("blueprint") or blueprint.full(doc) or not blueprint.selected(doc, "codex"):
        raise ValueError("Remote memory client requires a Codex tools blueprint")
    _verify_source(doc)
    for name in ("data", "profile"):
        path = paths[name]
        config.managed_directory(path)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("BORG memory client directory is not owner-only")
    _owned_regular(paths["config"], private=True)
    conductor = config.read_private(root / "conductors/config.json")
    primary = [row for row in conductor.get("conductors", []) if isinstance(row, dict) and row.get("id") == "primary"]
    if (conductor.get("owner") != doc["owner"] or conductor.get("instance_id") != doc["instance_id"]
            or conductor.get("borgHome") != str(root) or len(primary) != 1
            or primary[0].get("codexHome") != str(paths["profile"])):
        raise ValueError("BORG primary conductor identity differs")
    for name in ("helper", "curl", "driver", "source_curl", "source_driver",
                 "installed_curl", "installed_driver"):
        _owned_regular(paths[name], limit=2 * 1024 * 1024)
    if any(not os.access(paths[name], os.X_OK) for name in ("helper", "curl", "driver")):
        raise ValueError("BORG memory client executables are not available")
    if paths["driver"] != paths["installed_driver"]:
        _verify_installed_sidecars(paths)
    else:
        for name in ("curl", "driver"):
            installed = hashlib.sha256(_owned_regular(paths[name], limit=2 * 1024 * 1024)).digest()
            bundled = hashlib.sha256(_owned_regular(paths["source_" + name], limit=2 * 1024 * 1024)).digest()
            if installed != bundled:
                raise ValueError("Installed memory client runtime differs from the reviewed release")
    binary = paths["codex"].resolve(strict=True)
    if (not binary.is_relative_to(root / "runtime/npm") or not binary.is_file()
            or binary.stat().st_uid != os.getuid() or binary.stat().st_mode & 0o022):
        raise ValueError("Pinned BORG Codex binary differs")
    runtime = conductor.get("runtime")
    node_bin = runtime.get("nodeBin") if isinstance(runtime, dict) else None
    if not isinstance(node_bin, str) or not Path(node_bin).is_absolute():
        raise ValueError("Pinned BORG Node runtime differs")
    node = Path(node_bin).resolve(strict=True)
    if (not node.is_relative_to(root / "runtime/node") or not node.is_file()
            or node.stat().st_uid != os.getuid() or node.stat().st_mode & 0o022):
        raise ValueError("Pinned BORG Node runtime differs")
    paths["node"] = node
    return paths


def _expected(doc: dict, *, machine: str, hub_machine: str, endpoint: str,
              principal: str, read_scopes: list[str], write_scope: str) -> dict:
    if not isinstance(machine, str) or not MACHINE.fullmatch(machine):
        raise ValueError("Invalid native machine ID")
    if not isinstance(hub_machine, str) or not MACHINE.fullmatch(hub_machine) or hub_machine == machine:
        raise ValueError("Invalid distinct Hub machine ID")
    owner_scope = doc["memory"]["default_scope"]
    allowed = {owner_scope, "team:project", "ops"}
    if (not isinstance(read_scopes, list) or not read_scopes
            or any(not isinstance(scope, str) or scope not in allowed for scope in read_scopes)
            or len(set(read_scopes)) != len(read_scopes)
            or owner_scope not in read_scopes or write_scope != owner_scope):
        raise ValueError("Remote memory scopes must stay within the owner/project/ops policy")
    expected_principal = f"borg-life-{machine}-{doc['instance_id']}"
    if not isinstance(principal, str) or not PRINCIPAL.fullmatch(principal) or principal != expected_principal:
        raise ValueError("Remote memory principal must bind this machine and BORG instance")
    return {"schema": SCHEMA, "instance_id": doc["instance_id"], "owner": doc["owner"],
            "home": doc["home"], "profile": str(Path(doc["home"]) / "conductors/primary/profile"),
            "machine": machine, "hub_machine": hub_machine, "endpoint": _url(endpoint),
            "principal": principal, "read_scopes": sorted(read_scopes), "write_scope": write_scope}


def _read_manifest(doc: dict, paths: dict[str, Path]) -> dict | None:
    path = paths["manifest"]
    if not path.exists() and not path.is_symlink():
        return None
    manifest = config.read_private(path)
    if not isinstance(manifest, dict) or set(manifest) != {
            "schema", "instance_id", "owner", "home", "profile", "machine", "hub_machine",
            "endpoint", "principal", "read_scopes", "write_scope", "token_sha256"}:
        raise ValueError("Remote memory client manifest is invalid")
    expected = _expected(doc, machine=manifest["machine"], hub_machine=manifest["hub_machine"],
                         endpoint=manifest["endpoint"], principal=manifest["principal"],
                         read_scopes=manifest["read_scopes"], write_scope=manifest["write_scope"])
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("Remote memory client manifest belongs to another BORG instance")
    if not isinstance(manifest["token_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["token_sha256"]):
        raise ValueError("Remote memory client token digest is invalid")
    token = _owned_regular(paths["token"], private=True, limit=256).strip()
    if (not re.fullmatch(rb"[A-Za-z0-9_+/=-]{40,128}", token)
            or hashlib.sha256(token).hexdigest() != manifest["token_sha256"]):
        raise ValueError("Remote memory client token differs")
    stored_endpoint = _owned_regular(paths["endpoint"], private=True, limit=2048).decode("utf-8").strip()
    if stored_endpoint != manifest["endpoint"]:
        raise ValueError("Remote memory client endpoint differs")
    return manifest


def _safe_result(manifest: dict, state: str, *, route: bool = False, native: bool = False,
                 replay: str = "NOT_VERIFIED") -> dict:
    return {**manifest, "state": state, "route_authenticated": route,
            "native_trust_verified": native, "replay_scheduler_state": replay,
            "lifecycle_e2e": "NOT_VERIFIED"}


def stage(doc: dict, *, machine: str, hub_machine: str, endpoint: str,
          principal: str, read_scopes: list[str], write_scope: str) -> dict:
    paths = _identity(doc)
    expected = _expected(doc, machine=machine, hub_machine=hub_machine, endpoint=endpoint,
                         principal=principal, read_scopes=read_scopes, write_scope=write_scope)
    existing = _read_manifest(doc, paths)
    if existing is not None:
        if any(existing.get(key) != value for key, value in expected.items()):
            raise ValueError("Existing remote memory binding differs; preserve and reconcile it")
        return _safe_result(existing, "STAGED_NEEDS_GRANT")
    if any(paths[name].exists() or paths[name].is_symlink() for name in ("token", "endpoint")):
        raise ValueError("Unrecognized remote memory route exists; preserve and reconcile it")
    token = secrets.token_urlsafe(36)
    digest = hashlib.sha256(token.encode()).hexdigest()
    config.write_private(paths["token"], token + "\n")
    config.write_private(paths["endpoint"], endpoint + "\n")
    manifest = {**expected, "token_sha256": digest}
    config.write_private(paths["manifest"], json.dumps(manifest, sort_keys=True) + "\n")
    return _safe_result(manifest, "STAGED_NEEDS_GRANT")


def _environment(doc: dict, paths: dict[str, Path], manifest: dict) -> dict[str, str]:
    env = {**os.environ, **services.service_environment(doc)}
    for key in tuple(env):
        if key.startswith("MEM0_") or key in {"BORG_POLICY_FILE", "BORG_MEMORY_ENDPOINT", "MEMORY_CODEX_BIN"}:
            env.pop(key, None)
    env.update({"BORG_HOME": doc["home"], "BORG_LOCAL_MACHINE_ID": manifest["hub_machine"],
                "MEM0_FLEET_BASE": str(paths["root"] / "mem0"),
                "MEM0_FLEET_TOKEN_FILE": str(paths["token"]),
                "MEM0_FLEET_ENDPOINT_FILE": str(paths["endpoint"]),
                "MEM0_MACHINE": manifest["machine"], "MEM0_HARNESS": "codex",
                "CODEX_HOME": str(paths["profile"]),
                "PATH": str(paths["node"].parent) + os.pathsep + env.get("PATH", os.defpath)})
    return env


REPLAY_INTERVAL_SECONDS = 300
REPLAY_ITEM_CAP = 4
REPLAY_WALL_SECONDS = 30


def _launch_agents_dir(home: Path | None = None) -> Path:
    # HOME is an environment input to the CLI; the native UID database binds
    # the user domain and the only LaunchAgents directory we may create in it.
    try:
        user_home = home or Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        raise NativeRefusal("Native user home is unavailable for replay") from None
    directory = user_home / "Library/LaunchAgents"
    for parent in (user_home, directory.parent, directory):
        if not parent.exists() and not parent.is_symlink():
            if parent == directory:
                continue
            raise NativeRefusal("Native LaunchAgent parent is unavailable")
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise NativeRefusal("Native LaunchAgent directory is not owner-controlled")
    return directory


def _replay_definition(doc: dict, paths: dict[str, Path], manifest: dict) -> tuple[Path, bytes, dict]:
    """One opt-in user job; the installed app and all other services stay untouched."""
    if platform.system() != "Darwin":
        raise NativeRefusal("Periodic remote memory replay is unsupported on this platform")
    root = paths["root"]
    try:
        python = config.managed_python(root, root / "mem0/venv/bin/python")
    except (OSError, ValueError):
        raise NativeRefusal("Pinned BORG Python runtime is unavailable for replay") from None
    label = services.label(doc, "memory-replay")
    directory = _launch_agents_dir()
    environment = {
        "BORG_HOME": doc["home"], "BORG_OWNER_ID": doc["owner"],
        "BORG_LOCAL_MACHINE_ID": manifest["hub_machine"],
        "MEM0_FLEET_BASE": str(root / "mem0"),
        "MEM0_FLEET_TOKEN_FILE": str(paths["token"]),
        "MEM0_FLEET_ENDPOINT_FILE": str(paths["endpoint"]),
        "MEM0_FLEET_ENDPOINT": "",
        "MEM0_MACHINE": manifest["machine"], "MEM0_HARNESS": "codex",
        "MEM0_REPLAY_ITEM_CAP": str(REPLAY_ITEM_CAP),
        "MEM0_REPLAY_WALL_SECONDS": str(REPLAY_WALL_SECONDS),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": str(Path(python).parent) + ":/usr/bin:/bin",
    }
    specification = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/env", "-u", "MEM0_FLEET_TEST_CAPTURE_JSON",
                             "-u", "MEM0_FLEET_TEST_SEARCH_JSON", str(python), "-B",
                             str(paths["driver"]), "replay"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": environment,
        "RunAtLoad": True, "StartInterval": REPLAY_INTERVAL_SECONDS,
        "ProcessType": "Background", "LowPriorityIO": True, "Nice": 5,
        "Umask": 0o077,
        "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
    }
    return directory / (label + ".plist"), plistlib.dumps(specification), specification


def _launchctl(args: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["/bin/launchctl", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise NativeUncertain("Native memory replay registration timed out; inspect the exact user job") from None
    except OSError:
        raise NativeRefusal("Native memory replay registration is unavailable") from None
    if len(result.stdout) > 128 * 1024:
        raise NativeRefusal("Native memory replay job response is too large")
    return result


def _launchctl_fields(raw: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Parse only the small, stable fields needed to reject a stale loaded job."""
    fields: dict[str, str] = {}
    blocks: dict[str, list[str]] = {}
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("\t") and not line.startswith("\t\t") and line.endswith(" = {"):
            name = line.strip()[:-4]
            values = []
            index += 1
            while index < len(lines) and lines[index] != "\t}":
                if not lines[index].startswith("\t\t"):
                    raise NativeRefusal("Native memory replay job response is malformed")
                values.append(lines[index].strip())
                index += 1
            if index == len(lines) or name in blocks:
                raise NativeRefusal("Native memory replay job response is malformed")
            blocks[name] = values
        elif line.startswith("\t") and not line.startswith("\t\t") and " = " in line:
            name, value = line.strip().split(" = ", 1)
            if name in fields:
                raise NativeRefusal("Native memory replay job response is malformed")
            fields[name] = value
        index += 1
    return fields, blocks


def _replay_state(doc: dict, paths: dict[str, Path], manifest: dict) -> str:
    target, expected, specification = _replay_definition(doc, paths, manifest)
    domain = f"gui/{os.getuid()}"
    loaded = _launchctl(["print", domain + "/" + specification["Label"]])
    if loaded.returncode not in {0, 113}:
        raise NativeRefusal("Native memory replay job could not be inspected")
    if target.exists() or target.is_symlink():
        try:
            observed = _owned_regular(target, private=True, limit=64 * 1024)
        except (OSError, ValueError):
            raise NativeRefusal("Existing native memory replay job is not an owned private file") from None
        if observed != expected:
            raise NativeRefusal("Existing native memory replay job differs; preserve it")
    elif loaded.returncode == 0:
        raise NativeRefusal("Loaded native memory replay job has no owned definition")
    else:
        return "MISSING"
    if loaded.returncode == 113:
        return "MISSING"
    if not loaded.stdout.startswith(domain + "/" + specification["Label"] + " = {\n"):
        raise NativeRefusal("Loaded native memory replay job identity differs")
    fields, blocks = _launchctl_fields(loaded.stdout)
    if any(fields.get(key) != value for key, value in {
            "path": str(target), "type": "LaunchAgent",
            "program": specification["ProgramArguments"][0],
            "working directory": specification["WorkingDirectory"],
            "run interval": str(REPLAY_INTERVAL_SECONDS) + " seconds"}.items()):
        raise NativeRefusal("Loaded native memory replay job differs from its owned definition")
    if "runatload" not in (fields.get("properties") or "").split(" | "):
        raise NativeRefusal("Loaded native memory replay job did not retain RunAtLoad")
    if blocks.get("arguments") != specification["ProgramArguments"]:
        raise NativeRefusal("Loaded native memory replay command differs")
    observed_env = {}
    for row in blocks.get("environment", []):
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*) => ?(.*)", row)
        if match is None:
            raise NativeRefusal("Loaded native memory replay environment is malformed")
        key, value = match.groups()
        if key in observed_env:
            raise NativeRefusal("Loaded native memory replay environment is malformed")
        observed_env[key] = value
    expected_env = specification["EnvironmentVariables"]
    if (any(observed_env.get(key) != value for key, value in expected_env.items())
            or set(observed_env) - set(expected_env) - {"XPC_SERVICE_NAME"}):
        raise NativeRefusal("Loaded native memory replay environment differs")
    return "VERIFIED"


def _enable_replay(doc: dict, paths: dict[str, Path], manifest: dict) -> str:
    state = _replay_state(doc, paths, manifest)
    if state == "VERIFIED":
        return state
    target, body, specification = _replay_definition(doc, paths, manifest)
    if not target.parent.exists():
        try:
            target.parent.mkdir(mode=0o700)
        except OSError:
            raise NativeRefusal("Native LaunchAgent directory could not be created") from None
        _launch_agents_dir()
    if not target.exists() and not target.is_symlink():
        try:
            config.write_private(target, body.decode("utf-8"))
        except (OSError, ValueError):
            raise NativeRefusal("Native memory replay definition could not be created") from None
    try:
        if _owned_regular(target, private=True, limit=64 * 1024) != body:
            raise NativeRefusal("Existing native memory replay job differs; preserve it")
    except (OSError, ValueError):
        raise NativeRefusal("Existing native memory replay job is not an owned private file") from None
    # A definite bootstrap refusal leaves the exact owned definition for an
    # explicit retry. A timeout is uncertain and is never retried in this call.
    result = _launchctl(["bootstrap", f"gui/{os.getuid()}", str(target)], timeout=30)
    if result.returncode != 0:
        raise NativeRefusal("Native memory replay bootstrap refused")
    try:
        verified = _replay_state(doc, paths, manifest) == "VERIFIED"
    except (NativeRefusal, NativeUncertain):
        verified = False
    if not verified:
        raise NativeUncertain("Native memory replay bootstrap completed without exact readback; inspect the user job")
    return "VERIFIED"


def _run(args: list[str], env: dict[str, str], *, timeout: int) -> dict:
    try:
        result = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise NativeUncertain("Native memory client operation timed out; inspect native state before retrying") from None
    except OSError:
        raise NativeRefusal("Native memory client operation unavailable") from None
    if result.returncode != 0 or len(result.stdout) > MAX_OUTPUT:
        raise NativeRefusal("Native memory client operation refused; inspect the private native receipt")
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise NativeRefusal("Native memory client returned an unreadable receipt") from None
    if not isinstance(value, dict):
        raise NativeRefusal("Native memory client returned an invalid receipt")
    return value


def _whoami(doc: dict, paths: dict[str, Path], manifest: dict) -> bool:
    try:
        value = _run([str(paths["curl"]), "memory_whoami", "{}"],
                     _environment(doc, paths, manifest), timeout=30)
    except NativeRefusal:
        return False
    return (value.get("principal") == manifest["principal"]
            and isinstance(value.get("allowed_scopes"), list)
            and set(value["allowed_scopes"]) == set(manifest["read_scopes"])
            and len(value["allowed_scopes"]) == len(manifest["read_scopes"])
            and value.get("write_scope") == manifest["write_scope"])


def _plan(doc: dict, paths: dict[str, Path], manifest: dict, *, apply: bool) -> tuple[dict, bool]:
    args = [str(paths["helper"]), "--machine", manifest["machine"], "--codex",
            "--codex-home", str(paths["profile"]), "--codex-bin", str(paths["codex"])]
    if not apply:
        args.append("--check")
    receipt = _run(args, _environment(doc, paths, manifest), timeout=120 if apply else 60)
    codex = receipt.get("codex")
    profiles = codex.get("profiles") if isinstance(codex, dict) else None
    if (receipt.get("status") != ("PASS" if apply else "CHECK") or receipt.get("machine") != manifest["machine"]
            or receipt.get("driver_path") != str(paths["driver"])
            or not isinstance(profiles, list) or len(profiles) != 1 or codex.get("selected") != 1
            or not isinstance(profiles[0], dict) or profiles[0].get("path") != str(paths["config"])):
        raise NativeRefusal("Native memory configurator receipt differs from the owned primary profile")
    profile = profiles[0]
    trust = profile.get("trust")
    if not isinstance(trust, dict):
        raise NativeRefusal("Native memory configurator omitted hook trust evidence")
    trusted = {value.get("event") for key, value in trust.items()
               if key != "state_upsert_required" and isinstance(value, dict)
               and value.get("status_after" if apply else "status_before") == "trusted"}
    exact_hooks = (len(trust) == len(EVENTS) + (0 if apply else 1)
                   and len({key for key in trust if key != "state_upsert_required"}) == len(EVENTS))
    ready = (exact_hooks and codex.get("changed") == 0 and profile.get("changed_fields") == []
             and profile.get("missing_fields") == []
             and trust.get("state_upsert_required") is False and trusted == EVENTS) if not apply else (
             exact_hooks and profile.get("unrelated_trust_preserved") is True and trusted == EVENTS)
    return receipt, ready


def check(doc: dict) -> dict:
    paths = _identity(doc)
    manifest = _read_manifest(doc, paths)
    if manifest is None:
        return {"schema": SCHEMA, "state": "ABSENT", "instance_id": doc["instance_id"],
            "route_authenticated": False, "native_trust_verified": False,
            "replay_scheduler_state": "NOT_VERIFIED", "lifecycle_e2e": "NOT_VERIFIED"}
    route = _whoami(doc, paths, manifest)
    try:
        _, trusted = _plan(doc, paths, manifest, apply=False)
    except NativeUncertain:
        return _safe_result(manifest, "NATIVE_UNCERTAIN", route=route)
    except NativeRefusal:
        return _safe_result(manifest, "NATIVE_REFUSAL", route=route)
    if not route or not trusted:
        return _safe_result(manifest, "NEEDS_GRANT" if not route else "NEEDS_HOOKS",
                            route=route, native=trusted)
    if platform.system() != "Darwin":
        return _safe_result(manifest, "REPLAY_UNSUPPORTED", route=True, native=True,
                            replay="UNSUPPORTED")
    try:
        replay = _replay_state(doc, paths, manifest)
    except NativeUncertain:
        return _safe_result(manifest, "NATIVE_UNCERTAIN", route=True, native=True)
    except NativeRefusal:
        return _safe_result(manifest, "NATIVE_REFUSAL", route=True, native=True)
    return _safe_result(manifest, "VERIFIED" if replay == "VERIFIED" else "NEEDS_REPLAY",
                        route=True, native=True, replay=replay)


def enable(doc: dict) -> dict:
    paths = _identity(doc)
    manifest = _read_manifest(doc, paths)
    if manifest is None:
        raise ValueError("Stage the owned remote memory client before enabling it")
    if not _whoami(doc, paths, manifest):
        return _safe_result(manifest, "NEEDS_GRANT")
    if platform.system() != "Darwin":
        return _safe_result(manifest, "REPLAY_UNSUPPORTED", route=True,
                            replay="UNSUPPORTED")
    try:
        _, trusted = _plan(doc, paths, manifest, apply=False)
        if not trusted:
            _, applied = _plan(doc, paths, manifest, apply=True)
            if not applied:
                raise NativeRefusal("Native memory configurator did not trust all owned hooks")
        _, trusted = _plan(doc, paths, manifest, apply=False)
        replay = _enable_replay(doc, paths, manifest) if trusted else "NOT_VERIFIED"
    except NativeUncertain:
        return _safe_result(manifest, "NATIVE_UNCERTAIN", route=True)
    except NativeRefusal:
        return _safe_result(manifest, "NATIVE_REFUSAL", route=True)
    return _safe_result(manifest, "VERIFIED" if trusted and replay == "VERIFIED" else "NEEDS_HOOKS",
                        route=True, native=trusted, replay=replay)
