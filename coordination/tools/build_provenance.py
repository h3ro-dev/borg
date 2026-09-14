#!/usr/bin/env python3
"""Build a public source-hash manifest from the private handoff manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


PORTABLE_PATCHES = {
    "comms/bin/inbox": "discover installation-local owner policy config",
    "comms/bin/inbox-native": "discover installation-local owner policy config",
    "comms/hub/adapters.py": "generic product naming",
    "comms/hub/cli.py": "generic product naming and policy reference",
    "comms/hub/estate.py": "estate provider is explicit and disabled by default",
    "comms/hub/fleet_context.py": "deployment source paths removed",
    "comms/hub/native_config.py": "deployment-specific implicit runtime removed",
    "comms/hub/policy.py": "installation-local owner policy pin",
    "comms/hub/service.py": "portable enrollment grants and optional integrations",
    "comms/hub/store.py": "generic owner documentation",
    "comms/hub/tests/test_agents_list_boundaries.py": "private work and estate fixture names removed",
    "comms/hub/tests/test_inbox_estate.py": "private work reference removed from test documentation",
    "fleet_desktop/tart.py": "private integration guest prefix replaced",
}

PUBLIC_RENAMES_BY_SHA256 = {
    "059677ea218c6cd33cae8438c5da4a2609175a0ba11dc6bd475b127ddf6f5467":
        "comms/hub/tests/test_agents_list_boundaries.py",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(input_manifest: Path, source_root: Path) -> dict:
    private = json.loads(input_manifest.read_text(encoding="utf-8"))
    if not isinstance(private, dict):
        raise ValueError("input manifest must be an object")
    files = []
    prefix = "inputs/inbox/"
    for input_path, record in sorted(private.items()):
        if not input_path.startswith(prefix) or not isinstance(record, dict):
            raise ValueError("input manifest contains an unexpected entry")
        upstream = record.get("sha256")
        if not isinstance(upstream, str) or len(upstream) != 64:
            raise ValueError("input manifest contains an invalid hash")
        upstream_relative = input_path[len(prefix) :]
        relative = PUBLIC_RENAMES_BY_SHA256.get(upstream, upstream_relative)
        target = source_root / relative
        packaged = digest(target)
        status = "byte_exact" if packaged == upstream else "portable_patch"
        if status == "portable_patch" and relative not in PORTABLE_PATCHES:
            raise ValueError(f"unrecorded source change: {relative}")
        item = {
            "path": relative,
            "upstream_sha256": upstream,
            "packaged_sha256": packaged,
            "status": status,
        }
        if status == "portable_patch":
            item["change"] = PORTABLE_PATCHES[relative]
        files.append(item)
    if set(PORTABLE_PATCHES) - {item["path"] for item in files}:
        raise ValueError("portable patch table names a missing upstream file")
    return {
        "schema": "borg-coordination-provenance/v1",
        "upstream_release": "owner-supplied-native-inbox-source",
        "upstream_verification": "root supplied source verified from active service metadata",
        "private_origin_paths_included": False,
        "upstream_file_count": len(files),
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-manifest", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source_root = Path(args.source_root).resolve()
    output = Path(args.output).resolve()
    value = build(Path(args.input_manifest), source_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, prefix=f".{output.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    os.chmod(output, 0o644)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
