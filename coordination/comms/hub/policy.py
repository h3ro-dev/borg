"""Read an installation owner's explicitly pinned coordination policy."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

MAX_POLICY_BYTES = 65536
MAX_CONFIG_BYTES = 128 * 1024
CONFIG_ENV = "BORG_COORDINATION_CONFIG"


def _configuration() -> tuple[Path | None, dict[str, Any] | None]:
    configured = os.environ.get(CONFIG_ENV)
    if not configured:
        return None, None
    path = Path(configured).expanduser()
    if not path.is_absolute():
        return path, None
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_CONFIG_BYTES:
            return path, None
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return path, None
    return path, value if isinstance(value, dict) else None


def read_policy(*, include_body: bool = False, path: str | os.PathLike[str] | None = None):
    """Return only owner-pinned policy bytes; never fall back to another installation."""

    config_path, config = _configuration()
    expected_version = config.get("policy_version") if config else None
    expected_sha256 = config.get("policy_sha256") if config else None
    source_value = path if path is not None else (config.get("policy_file") if config else None)
    result: dict[str, Any] = {
        "expected_version": expected_version,
        "expected_sha256": expected_sha256,
        "work_effect": "preserve_current",
        "fetch_tool": "inbox_policy",
    }
    if (
        config_path is None
        or config is None
        or not isinstance(source_value, (str, os.PathLike))
        or not isinstance(expected_version, str)
        or not expected_version
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
    ):
        result["state"] = "unconfigured"
        result["next_action"] = "Configure and pin this installation's owner policy."
        return result

    source = Path(source_value).expanduser()
    try:
        source.resolve().relative_to(config_path.resolve().parent)
    except (OSError, ValueError):
        result["state"] = "path_mismatch"
        result["next_action"] = "Keep the owner policy inside this coordination installation."
        return result

    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(source, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_POLICY_BYTES:
            result["state"] = "unavailable"
        else:
            body = os.read(descriptor, MAX_POLICY_BYTES + 1)
            observed = hashlib.sha256(body).hexdigest()
            result["observed_sha256"] = observed
            if len(body) > MAX_POLICY_BYTES or observed != expected_sha256:
                result["state"] = "hash_mismatch"
            else:
                result.update(state="verified", version=expected_version, sha256=observed)
                if include_body:
                    result["body"] = body.decode("utf-8")
    except (OSError, UnicodeError):
        result["state"] = "unavailable"
        result.pop("body", None)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if result["state"] != "verified":
        result["next_action"] = (
            "Continue current authorized work and ask the installation owner to pin the local policy."
        )
    return result


def policy_instructions(policy):
    """Expose only hash-verified bytes with an explicit continuity boundary."""

    if policy["state"] == "verified":
        return (
            "\n\nOwner's verified coordination rules "
            f"(version {policy['version']}, SHA256 {policy['sha256']}). "
            "Apply these rules while continuing your current objective.\n\n"
            + policy["body"]
        )
    return (
        "\n\nOwner policy load: "
        + policy["state"]
        + ". "
        + policy["next_action"]
        + " Use inbox_policy to recheck the pinned version."
    )
