"""Per-service descriptor capacity and payload-free resource observations."""
from __future__ import annotations

import os
import resource

DESCRIPTOR_TARGET = 8192


def configure_descriptor_limit() -> dict:
    """Raise only this service's soft limit within its existing hard limit."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = DESCRIPTOR_TARGET if hard == resource.RLIM_INFINITY else min(DESCRIPTOR_TARGET, hard)
    error = None
    if soft != resource.RLIM_INFINITY and soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (OSError, ValueError) as exc:
            error = type(exc).__name__
    result = descriptor_status()
    if error:
        result["configuration_error"] = error
    return result


def descriptor_status() -> dict:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    opened = None
    for directory in ("/dev/fd", "/proc/self/fd"):
        try:
            # The directory enumeration itself briefly uses one descriptor.
            opened = max(0, len(os.listdir(directory)) - 1)
            break
        except OSError:
            continue
    remaining = None if opened is None or soft == resource.RLIM_INFINITY else max(0, soft - opened)
    state = "UNKNOWN" if remaining is None else "DEGRADED" if remaining < 512 else "AVAILABLE"
    return {"state": state, "soft_limit": soft, "hard_limit": hard,
            "target_soft_limit": DESCRIPTOR_TARGET, "open_descriptors": opened,
            "remaining_descriptors": remaining,
            "notice": "Resource headroom is not proof that a command completed."}
