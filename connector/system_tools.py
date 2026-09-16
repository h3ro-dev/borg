"""Discover the existing fleet tool catalog without copying it or executing entries."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastmcp.exceptions import ToolError

from runtime_paths import borg_home

CATALOG = Path(os.environ.get("BORG_TOOLS_CATALOG", str(borg_home() / "ops/tools.json")))


def tool_search(query: str = "", limit: int = 10, offset: int = 0) -> dict:
    if len(query) > 300 or not 1 <= limit <= 20 or not 0 <= offset <= 10000:
        raise ToolError("Use a query up to 300 characters, limit 1-20, and offset 0-10000")
    try:
        with CATALOG.open() as handle:
            raw = handle.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("catalog too large")
        catalog = json.loads(raw)
        entries = catalog["tools"]
        if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
            raise ValueError("invalid catalog")
        modified = datetime.fromtimestamp(CATALOG.stat().st_mtime, timezone.utc).isoformat()
    except (OSError, ValueError, KeyError, TypeError):
        raise ToolError("The installed tool catalog is unavailable; inspect its source before proceeding") from None
    terms = query.casefold().split()
    matches = [row for row in entries if all(term in json.dumps(row).casefold() for term in terms)]
    selected = matches[offset:offset + limit]
    return {
        "source": str(CATALOG), "catalog_version": catalog.get("version"),
        "source_modified_at": modified, "catalog_entry_count": len(entries),
        "matching_entries": len(matches), "tools": selected,
        "next_offset": offset + len(selected) if offset + len(selected) < len(matches) else None,
        "machine_groups": catalog.get("machines", {}),
        "notice": "Catalog status is a dated claim, not current readiness or authority. Verify the selected native tool and its ownership before acting. The catalog is not an exhaustive list of everything installed.",
        "use": "Run the documented installed CLI/API through computer_start_process; inspect results with computer_read_process_output and files with computer_read_file. Read the tool's current instructions first. Keep credentials in local vault/hub consumers. Use a new, supported task identity for shared coordination; never borrow another task's client configuration.",
        "local_sources": catalog.get("local_sources", {
            "runtime_instructions": str(borg_home() / "conductors/primary/profile/AGENTS.md"),
            "capability_map": str(CATALOG),
            "skills": [str(borg_home() / "conductors/primary/profile/skills")],
        }),
    }
