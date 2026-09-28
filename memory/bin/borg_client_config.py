"""Minimal owned-home resolver for a remote fleet client.

The fleet hook never runs a local extractor, embedder, graph, or Mem0 server.
Keep those model requirements in borg_config.py, which still validates the full
server; importing a remote client must not invent local model identities.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import uuid


class ClientConfigError(ValueError):
    """The client route does not belong to this owner and BORG home."""


def _directory(path: Path, *, owner_only: bool = True) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ClientConfigError("BORG client directory is unavailable") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & (0o077 if owner_only else 0o022)):
        raise ClientConfigError("BORG client directory is not owner-controlled")


def _home() -> Path:
    raw = os.environ.get("BORG_HOME", "")
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute() or path == Path("/"):
        raise ClientConfigError("BORG_HOME must be an absolute owned home")
    try:
        if path.resolve(strict=True) != path or any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ClientConfigError("BORG_HOME has a symlink or noncanonical parent")
    except OSError as exc:
        raise ClientConfigError("BORG_HOME is unavailable") from exc
    _directory(path)
    return path


def _document(home: Path) -> dict:
    path = home / "config.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
                raise ClientConfigError("BORG client config is not an owned private file")
            doc = json.loads(stream.read(1024 * 1024 + 1))
    except (OSError, ValueError) as exc:
        raise ClientConfigError("BORG client config is unavailable or invalid") from exc
    if not isinstance(doc, dict):
        raise ClientConfigError("BORG client config must be an object")
    return doc


def mem0_root() -> Path:
    """Resolve only this instance's client state root, without server models."""
    home = _home()
    doc = _document(home)
    owner = doc.get("owner")
    instance = doc.get("instance_id")
    try:
        canonical_instance = str(uuid.UUID(instance)) == instance
    except (ValueError, TypeError, AttributeError):
        canonical_instance = False
    if (doc.get("schema") != "borg-install/v1" or doc.get("home") != str(home)
            or not isinstance(owner, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", owner)
            or not canonical_instance
            or (os.environ.get("BORG_OWNER_ID") not in (None, owner))):
        raise ClientConfigError("BORG client instance or owner differs")
    root = home / "mem0"
    _directory(root)
    raw_base = os.environ.get("MEM0_FLEET_BASE")
    if raw_base is not None and raw_base != str(root):
        raise ClientConfigError("MEM0_FLEET_BASE must be this BORG home's mem0 root")
    return root
