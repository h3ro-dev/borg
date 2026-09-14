"""Secret-free status for web GPT BORG action-catalog rollouts.

The ChatGPT connector caches an MCP action snapshot per account. BORG can
publish rollout state as metadata so an owner can tell which bindings need a
refresh without exposing OAuth clients, tokens, cookies or passwords.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

SCHEMA = "borg-web-gpt-catalog-status/v1"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        info = path.stat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_size > 128_000):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_catalog_status(config: object) -> dict[str, Any]:
    """Return a bounded catalog summary, or an explicit unavailable state."""
    path_value = config.get("status_file") if isinstance(config, dict) else None
    if not path_value:
        return {"status": "not_configured", "schema": SCHEMA}
    data = _read(Path(str(path_value)))
    if not data:
        return {
            "status": "unavailable",
            "schema": SCHEMA,
            "notice": "Account catalog status could not be read; refresh state requires native account verification.",
        }

    # Keep the web-facing manifest bounded and value-blind. The durable
    # receipt may contain per-account labels, but discovery exposes rollout
    # counts and the exact action required.
    current = data.get("current_catalog") if isinstance(data.get("current_catalog"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return {
        "status": str(data.get("status") or "observed"),
        "schema": str(data.get("schema") or SCHEMA),
        "observed_at": data.get("observed_at"),
        "previous_catalog": {
            "tool_count": current.get("previous_tool_count"),
            "schema_sha256": current.get("previous_schema_sha256"),
            "observed_at": current.get("previous_observed_at"),
        },
        "current_catalog": {
            "tool_count": current.get("tool_count"),
            "schema_sha256": current.get("schema_sha256"),
            "observed_at": current.get("observed_at"),
        },
        "summary": {
            "eligible_accounts": summary.get("eligible_accounts"),
            "connected_accounts_on_previous_catalog": summary.get("connected_accounts_on_previous_catalog"),
            "refresh_verified_bindings": summary.get("refresh_verified_bindings"),
            "connected_accounts_remaining_to_verify": summary.get("connected_accounts_remaining_to_verify"),
            "pending_setup_accounts": summary.get("pending_setup_accounts"),
            "excluded_accounts": summary.get("excluded_accounts"),
        },
        "required_action": "Refresh or reconnect each existing BORG app binding after the action snapshot changes; verify a native borg_status call per account.",
        "notice": "A refreshed action catalog is separate from OAuth authentication and separate from provider-level permissions.",
    }
