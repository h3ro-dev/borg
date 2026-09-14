"""Resolve only this owner's independent BORG installation."""
import os
from pathlib import Path
from urllib.parse import urlsplit


def borg_home() -> Path:
    value = Path(os.environ.get("BORG_HOME", str(Path.home() / ".borg"))).expanduser()
    if not value.is_absolute() or value.resolve() != value:
        raise ValueError("BORG_HOME must be an absolute canonical path without symlinks")
    return value


def loopback_mcp_url(value: str) -> str:
    """Local bearer credentials may only reach the configured loopback MCP."""
    if not isinstance(value, str):
        raise ValueError("BORG upstream must be a URL string")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/mcp" or parsed.port is None or parsed.port < 1):
        raise ValueError("BORG upstream must be an explicit loopback HTTP MCP URL")
    return value
