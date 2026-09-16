"""Integrate real component sources and perform one complete installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time

from installer import blueprint, config, dependencies, services

SOURCE = Path(__file__).resolve().parent.parent
DIRECTORIES = ["connector", "conductor", "memory", "graph", "training", "adapters", "coordination", "installer", "platform"]


def source_files() -> list[Path]:
    from installer.adapter_contract import verify_release
    verify_release(SOURCE)
    if (SOURCE / "connector/prepare_computer_backend.py").exists():
        raise ValueError("An obsolete external computer backend helper is present")
    files = [SOURCE / name for name in ["borg.py", "LICENSE", "THIRD_PARTY_NOTICES.md"]]
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Required BORG source or notice is missing: " + path.name)
    for name in DIRECTORIES:
        directory = SOURCE / name
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Required BORG component source is missing: " + name)
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("Source package must not contain symlinks")
            if not (path.is_file() or path.is_dir()):
                raise ValueError("Source package contains a special file")
            if any(part in {"__pycache__", ".git", ".pytest_cache", "node_modules"} for part in path.parts):
                continue
            if path.is_file() and not (path.name.endswith((".pyc", ".private.json"))
                    or "private" in path.name.casefold()
                    or path.name in {"REPORT.md", "LANE-REPORT.md", "NEEDS-INPUT.md"}):
                files.append(path)
    for required in ["memory/bin/mem0_scope_lib.py", "memory/bin/mem0-codex-hook", "conductor/conductor.mjs"]:
        if SOURCE / required not in files:
            raise RuntimeError("Required native BORG source is missing: " + required)
    return sorted(files)


def component_copies(source: Path, root: Path):
    return [(source / "memory/bin", root / "mem0/bin"),
            (source / "graph", root / "graphiti"),
            (source / "connector", root / "borg-context")]


def check_component(path: Path, expected: Path) -> None:
    config.managed_directory(path.parent)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or path.read_bytes() != expected.read_bytes()):
            raise RuntimeError("Installed component was changed: " + str(path))


def preflight(doc: dict) -> tuple[list[Path], dict[str, str], str]:
    """Check source and existing installation without modifying either."""
    root = Path(doc["home"])
    config.validate_managed_paths(doc)
    python = root / "mem0/venv/bin/python"
    if python.exists() or python.is_symlink():
        config.managed_python(root, python)
    for name in ["package.json", "package-lock.json"]:
        check_component(root / "runtime/npm" / name, SOURCE / "installer/npm" / name)
    files = source_files()
    manifest = {str(path.relative_to(SOURCE)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    app = root / "app"
    config.absolute_root(app)
    marker = app / ".borg-source-sha256"
    if app.exists():
        recorded = app / "source-manifest.json"
        if (marker.is_symlink() or recorded.is_symlink() or not marker.is_file()
                or not recorded.is_file() or marker.read_text().strip() != digest
                or json.loads(recorded.read_text()) != manifest):
            raise RuntimeError("Application source differs from this installation. Preserve it and use a separate home for this release.")
        for relative, expected in manifest.items():
            path = app / relative
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError("Installed application was changed: " + relative)
        expected_paths = set(manifest) | {".borg-source-sha256", "source-manifest.json"}
        expected_dirs = {str(parent) for name in manifest for parent in Path(name).parents if str(parent) != "."}
        for path in app.rglob("*"):
            relative = path.relative_to(app).as_posix()
            if (path.is_symlink() or (path.is_file() and relative not in expected_paths)
                    or (path.is_dir() and relative not in expected_dirs)
                    or not (path.is_file() or path.is_dir())):
                raise RuntimeError("Unexpected installed application entry: " + str(path.relative_to(app)))
    for source, target in component_copies(SOURCE, root):
        for path in source.rglob("*"):
            if path in files:
                check_component(target / path.relative_to(source), path)
    return files, manifest, digest


def install_sources(doc: dict) -> str:
    root = Path(doc["home"])
    files, manifest, digest = preflight(doc)
    app = root / "app"
    if not app.exists():
        stage = root / ".app-installing"
        if stage.exists() or stage.is_symlink():
            raise RuntimeError("An unfinished source installation needs inspection: " + str(stage))
        stage.mkdir(mode=0o700)
        for path in files:
            target = stage / path.relative_to(SOURCE)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (stage / ".borg-source-sha256").write_text(digest + "\n")
        (stage / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(stage, app)
    for source, target in component_copies(app, root):
        # Reconcile interrupted copies without overwriting an owner's edits.
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            destination = target / path.relative_to(source)
            check_component(destination, path)
            if destination.exists() or destination.is_symlink():
                continue
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    bindir = root / "bin"
    bindir.mkdir(mode=0o700, exist_ok=True)
    launcher = "#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\nexec " + shlex.quote(str(root / "mem0/venv/bin/python")) + " -B " + shlex.quote(str(app / "borg.py")) + " \"$@\"\n"
    launcher = launcher.replace("exec ", "export BORG_HOME=" + shlex.quote(str(root)) + "\nexec ", 1)
    config.write_private(bindir / "borg", launcher, replace=(bindir / "borg").exists())
    (bindir / "borg").chmod(0o700)
    catalog = {"version": "borg-install/v1", "tools": [
        {"id": "borg", "name": "BORG lifecycle", "category": "system", "invoke": {"cli": str(bindir / "borg")}},
        {"id": "memory", "name": "BORG memory", "category": "memory", "invoke": {"cli": str(root / "mem0/bin/mem0ctl")}},
        {"id": "conductor", "name": "BORG conductors", "category": "agents", "invoke": {"source": str(app / "conductor")}},
        {"id": "inbox", "name": "Agent Inbox", "category": "coordination", "invoke": {"source": str(app / "coordination")}},
        {"id": "beads", "name": "Beads work store", "category": "coordination", "invoke": {"cli": str(bindir / "bd")}}],
        "machines": {"this_installation": ["local"]}}
    if doc.get("blueprint"):
        enabled_tools = {"borg"} | ({"memory"} if blueprint.full(doc) else set())
        enabled_tools |= {service for component, service in [("codex", "conductor"), ("inbox", "inbox"), ("beads", "beads")]
                          if blueprint.selected(doc, component)}
        catalog["tools"] = [row for row in catalog["tools"] if row["id"] in enabled_tools]
    if not (root / "ops/tools.json").exists():
        config.write_private(root / "ops/tools.json", json.dumps(catalog, indent=2) + "\n")
    return digest


def wait_for_port(port: int, *, seconds: int = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"BORG service did not start on loopback port {port}; inspect the instance logs")


def login(doc: dict, provider: str) -> int:
    root = Path(doc["home"])
    if not blueprint.selected(doc, "codex"):
        raise ValueError("Codex is not selected in this blueprint")
    env = services.service_environment(doc)
    command = [str(dependencies.executable(root, "node", "node")),
               str(root / "app/conductor/borg-conductor.mjs"), "auth"]
    options = ["--config", str(root / "conductors/config.json"), "--lane", "primary"]
    result = subprocess.run([*command, "login", *options], env=env)
    if result.returncode:
        return result.returncode
    return subprocess.run([*command, "pin", *options], env=env).returncode


def install(doc: dict, *, system_dependencies: bool = False, start: bool = True) -> int:
    root = Path(doc["home"])
    preflight(doc)
    config.write_connector_config(doc)
    dependencies.prepare(doc, system_dependencies=system_dependencies)
    # Bootstrap Python intentionally has no application packages. All native
    # integration and health checks run in the freshly locked environment.
    env = {**os.environ, **services.service_environment(doc), "PYTHONPATH": str(SOURCE)}
    env.pop("PYTHONHOME", None)
    command = [str(root / "mem0/venv/bin/python"), "-B", "-m", "installer.installation", "--home", str(root)]
    if not start:
        command.append("--no-start")
    return subprocess.run(command, env=env, cwd=SOURCE).returncode


def complete_install(doc: dict, *, start: bool = True) -> int:
    root = Path(doc["home"])
    if Path(sys.prefix).resolve() != root / "mem0/venv":
        raise RuntimeError("Native installation requires this home's locked Python environment")
    digest = install_sources(doc)
    # The accepted native coordination package must provide this bootstrap adapter.
    bootstrap = root / "app/coordination/bin/borg-coordination"
    if not bootstrap.is_file():
        raise RuntimeError("The native coordination bootstrap is missing from this package")
    env = services.service_environment(doc)
    if blueprint.selected(doc, "inbox") or blueprint.selected(doc, "beads"):
        subprocess.run([str(root / "mem0/venv/bin/python"), str(bootstrap), "bootstrap", "--home", str(root),
                        "--owner", doc["owner"], "--port", str(doc["ports"]["inbox"])], env=env, check=True)
    node = dependencies.executable(root, "node", "node")
    conductor = root / "app/conductor/borg-conductor.mjs"
    if any(blueprint.selected(doc, name) for name in ["codex", "grok", "claude", "cursor", "launch-bus"]):
        subprocess.run([str(node), str(conductor), "bootstrap", "--borg-home", str(root),
            "--config", str(root / "conductors/config.json"), "--owner", doc["owner"],
            "--instance-id", doc["instance_id"], "--port", str(doc["ports"]["conductor"]),
            "--node-bin", str(node), "--codex-bin", str(root / "runtime/npm/node_modules/.bin/codex")], env=env, check=True)
    if blueprint.selected(doc, "beads"):
        subprocess.run([str(root / "mem0/venv/bin/python"), str(bootstrap), "init-beads",
                        "--home", str(root), "--bd", str(dependencies.executable(root, "beads", "bd"))], env=env, check=True)
        beads_launcher = "#!/bin/sh\nexec " + shlex.join([
            str(root / "mem0/venv/bin/python"), "-B", str(bootstrap), "exec-beads", "--home", str(root),
            "--bd", str(dependencies.executable(root, "beads", "bd")), "--"]) + ' "$@"\n'
        target = root / "bin/bd"
        config.write_private(target, beads_launcher, replace=target.exists())
        target.chmod(0o700)
    from installer.clients import configure_clients
    configure_clients(doc)
    from installer.web import configure_watchdog
    configure_watchdog(doc)
    if not start:
        print(json.dumps({"state": "installed_not_started", "home": str(root), "source_sha256": digest}))
        return 0
    if blueprint.full(doc):
        services.start(doc, ["qdrant", "graph", "ollama"])
        for name in ["qdrant", "graph", "ollama"]:
            wait_for_port(doc["ports"][name])
        doc = dependencies.pull_models(doc)
        # Initialize the native memory store and scope registry before readiness.
        # These operations create no memories and make an empty installation usable.
        env = services.service_environment(doc)
        subprocess.run([str(root / "mem0/venv/bin/python"), str(root / "app/installer/brain_service.py"),
                        "initialize"], env=env, check=True)
    services.start(doc)
    for name in [name for name in ["memory", "connector", "inbox", "conductor"] if name in blueprint.service_names(doc)]:
        wait_for_port(doc["ports"][name])
    from installer.health import wait_for_local_ready
    result = wait_for_local_ready(doc)
    print(json.dumps(result, indent=2))
    if blueprint.selected(doc, "codex"):
        print("Connect your own provider account with: " + str(root / "bin/borg") + " auth codex")
    if doc.get("blueprint"):
        from installer.onboarding import plan
        print(json.dumps(plan(doc), indent=2))
    return 0 if result["local_services_ready"] else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Complete native setup in the managed Python environment")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--no-start", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    raise SystemExit(complete_install(config.load(args.home), start=not args.no_start))
