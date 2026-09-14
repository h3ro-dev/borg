"""Value-blind service handles for BORG-owned provider connections.

The connector never reads or returns a credential.  A registry contains only
provider metadata and a login URL; actual tokens stay in the existing vault or
the provider's native browser flow, including MFA and consent.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError

TOOL_NAMES = ["credential_list", "credential_status", "credential_handoff"]
NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
MAX_SERVICES = 100


def _safe_url(value: object) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("service login URL must be HTTPS and credential-free")
    return str(value)


class CredentialStore:
    def __init__(self, config: dict):
        from runtime_paths import borg_home
        self.config = config
        self.registry = Path(config.get("registry") or (borg_home() / "borg-context/credentials.json"))

    def _rows(self) -> list[dict]:
        try:
            info = self.registry.stat()
            if (self.registry.is_symlink() or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_size > 128_000):
                raise ValueError
            raw = json.loads(self.registry.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise ToolError("BORG service-handle registry is unavailable") from None
        rows = raw.get("services") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise ToolError("BORG service-handle registry is invalid")
        result = []
        for row in rows[:MAX_SERVICES]:
            if not isinstance(row, dict) or not NAME.fullmatch(str(row.get("name", ""))):
                continue
            try:
                login_url = _safe_url(row.get("login_url", ""))
            except ValueError:
                continue
            scopes = row.get("scopes", [])
            if not isinstance(scopes, list):
                scopes = []
            result.append({
                "name": str(row["name"]),
                "provider": str(row.get("provider") or row["name"]),
                "status": str(row.get("status") or "human_connection_required"),
                "login_url": login_url,
                "scopes": [str(scope) for scope in scopes[:30]],
                "storage": "external_vault_or_native_provider_session",
            })
        return result

    def list(self) -> dict:
        return {
            "backend": "value_blind",
            "services": self._rows(),
            "notice": "Only provider metadata is returned. Tokens, cookies, secrets, MFA and consent stay in the native vault or provider UI.",
        }

    def status(self, name: str) -> dict:
        if not NAME.fullmatch(str(name)):
            raise ToolError("BORG service handle name is invalid")
        for row in self._rows():
            if row["name"] == name:
                return {**row, "credential_values_returned": False}
        return {"name": name, "status": "not_configured", "credential_values_returned": False}

    def handoff(self, name: str) -> dict:
        row = self.status(name)
        if row.get("status") == "not_configured":
            raise ToolError("BORG service handle is not configured")
        return {
            "name": row["name"],
            "provider": row["provider"],
            "state": "human_action_required",
            "reason": "Complete provider login, MFA or consent in the native provider flow.",
            "login_url": row["login_url"],
            "recommended_next_tool": "browser_start_session",
            "browser_start_arguments": {"url": row["login_url"], "headless": False},
            "credential_values_returned": False,
        }


def mount_credentials(server, config):
    store = CredentialStore(config)
    descriptions = {
        "credential_list": "List configured value-blind provider service handles and their login state.",
        "credential_status": "Read one value-blind provider handle status without reading its credential.",
        "credential_handoff": "Return a native provider login or consent handoff; the human completes MFA and no secret is returned.",
    }
    annotations = {
        "credential_list": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "credential_status": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "credential_handoff": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    }
    for name in TOOL_NAMES:
        server.tool(name=name, annotations=annotations[name], description=descriptions[name])(getattr(store, name.removeprefix("credential_")))
