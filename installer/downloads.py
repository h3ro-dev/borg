"""Fetch pinned upstream artifacts into an instance, verifying before execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

LOCK_PATH = Path(__file__).with_name("runtime-lock.json")


def platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    if system not in {"darwin", "linux"} or machine not in {"aarch64", "x86_64"}:
        raise ValueError("BORG currently supports macOS and Linux on ARM64 or x86-64")
    return system, machine


def artifact(component: str) -> tuple[str, dict]:
    row = json.loads(LOCK_PATH.read_text())["components"][component]
    system, machine = platform_key()
    if component == "ollama":
        name = "ollama-darwin.tgz" if system == "darwin" else f"ollama-linux-{'arm64' if machine == 'aarch64' else 'amd64'}.tar.zst"
    elif component == "beads":
        name = f"beads_{row['version'].removeprefix('v')}_{system}_{'arm64' if machine == 'aarch64' else 'amd64'}.tar.gz"
    elif component == "cloudflared":
        name = f"cloudflared-{system}-{'arm64' if machine == 'aarch64' else 'amd64'}" + (".tgz" if system == "darwin" else "")
    elif component == "node":
        arch = "arm64" if machine == "aarch64" else "x64"
        name = f"node-{row['version']}-{system}-{arch}." + ("tar.gz" if system == "darwin" else "tar.xz")
    else:
        triple = "apple-darwin" if system == "darwin" else "unknown-linux-gnu"
        if component == "qdrant" and system == "linux" and machine == "aarch64":
            triple = "unknown-linux-musl"
        name = f"{component}-{machine}-{triple}.tar.gz"
    if name not in row["artifacts"]:
        raise ValueError(f"No verified {component} artifact for {system}/{machine}")
    return name, row["artifacts"][name]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(home: Path, component: str) -> Path:
    name, spec = artifact(component)
    cache = home / "cache/downloads"
    from installer.config import absolute_root
    absolute_root(cache)
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = cache / name
    if target.exists() and not target.is_symlink() and sha256(target) == spec["sha256"]:
        return target
    request = urllib.request.Request(spec["url"], headers={"User-Agent": "BORG-installer/1"})
    fd, temp_name = tempfile.mkstemp(prefix=".download-", dir=cache)
    temp = Path(temp_name)
    try:
        print(f"Downloading {component} ({name})", flush=True)
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(request, timeout=60) as response:
            received = 0
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > spec.get("size", 2 * 1024**3):
                    raise ValueError(f"{component} artifact exceeds its pinned size")
                output.write(chunk)
        if sha256(temp) != spec["sha256"]:
            raise ValueError(f"{component} artifact checksum mismatch")
        os.replace(temp, target)
        return target
    finally:
        temp.unlink(missing_ok=True)


def unpack(home: Path, component: str) -> Path:
    archive = download(home, component)
    _, spec = artifact(component)
    dest = home / "runtime" / component
    from installer.config import absolute_root
    absolute_root(dest)
    marker = dest / ".borg-artifact-sha256"
    if dest.exists() and (marker.is_symlink() or not marker.is_file()
                          or marker.read_text().strip() != spec["sha256"]):
        raise ValueError(f"Existing {component} differs from the locked runtime; explicit upgrade required")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{component}-", dir=dest.parent))
    try:
        if component == "cloudflared" and not archive.name.endswith(".tgz"):
            shutil.copyfile(archive, temporary / "cloudflared")
            (temporary / "cloudflared").chmod(0o700)
        elif archive.name.endswith(".zst"):
            uncompressed = temporary / ".archive.tar"
            python = home / "mem0/venv/bin/python"
            if not python.is_file():
                raise RuntimeError("Install the locked Python environment before unpacking zstd runtimes")
            subprocess.run([str(python), "-c",
                "import sys,zstandard; "
                "src=open(sys.argv[1],'rb'); dst=open(sys.argv[2],'xb'); "
                "zstandard.ZstdDecompressor().copy_stream(src,dst); dst.close(); src.close()",
                str(archive), str(uncompressed)], check=True)
            with tarfile.open(uncompressed) as tar:
                tar.extractall(temporary, filter="data")
            uncompressed.unlink()
        else:
            with tarfile.open(archive) as tar:
                tar.extractall(temporary, filter="data")
        if dest.exists():
            # Derive the expected tree from the verified publisher archive,
            # not from editable installation metadata.
            if runtime_tree(dest) != runtime_tree(temporary):
                raise ValueError(f"Installed {component} was changed; preserve it before repair")
            return dest
        (temporary / ".borg-artifact-sha256").write_text(spec["sha256"] + "\n")
        os.replace(temporary, dest)
        return dest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def runtime_tree(root: Path) -> dict:
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative == ".borg-artifact-sha256":
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Runtime symlink leaves its installation")
            result[relative] = {"link": os.readlink(path)}
        elif path.is_file():
            result[relative] = {"sha256": sha256(path), "execute": path.stat().st_mode & 0o111}
        elif path.is_dir():
            result[relative] = {"directory": True}
        else:
            raise ValueError("Unsupported runtime artifact")
    return result


def executable(home: Path, component: str, name: str) -> Path:
    root = home / "runtime" / component
    from installer.config import absolute_root
    absolute_root(root)
    matches = [p for p in root.rglob(name) if p.is_file() and os.access(p, os.X_OK)
               and p.resolve().is_relative_to(root.resolve())]
    if len(matches) != 1:
        raise ValueError(f"Expected one installed {component}/{name} executable, found {len(matches)}")
    return matches[0]
