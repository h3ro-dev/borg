"""Owner-run enrollment of explicitly identified resident connectors."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
import stat
import sys

from installer import config


def manage(doc: dict, args) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "connector"))
    from fleet_tools import Fleet, read_registry, validate_registry
    root = Path(doc["home"])
    path = root / "borg-context/fleet.json"
    if args.operation == "list":
        return {"state": "configured_only", **read_registry(path),
                "notice": "Use borg call fleet_hosts for fresh authenticated target discovery."}
    if not args.host:
        raise ValueError("Fleet enrollment or disabling requires a host ID")
    lock_path = path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "w") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("Unsafe fleet enrollment lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        connector_path = root / "borg-context/config.json"
        expected = {"identity": {k: doc[k] for k in ("instance_id", "owner", "home")},
                    "fleet": {"registry_file": str(path)}}

        def prepare_connector():
            connector = config.read_private(connector_path)
            restart = any(k not in connector for k in expected)
            for key, value in expected.items():
                if key in connector and connector[key] != value:
                    raise ValueError("Owner-edited connector enrollment settings need reconciliation")
                connector[key] = value
            return connector, restart

        prepare_connector()  # Refuse known conflicts before contacting the target.
        registry = read_registry(path) if path.exists() else {"schema": "borg-fleet/v1", "hosts": []}
        existing = next((r for r in registry["hosts"] if r["id"] == args.host), None)
        if args.operation == "disable":
            if existing is None:
                raise ValueError("Fleet host is not enrolled")
            existing["enabled"] = False
        else:
            if not all([args.ssh_alias, args.remote_home, args.owner, args.instance_id]):
                raise ValueError("Fleet add requires --ssh-alias, --remote-home, --owner and --instance-id from the target's borg_identity")
            row = {"id": args.host, "ssh_alias": args.ssh_alias, "home": str(args.remote_home),
                   "owner": args.owner, "instance_id": args.instance_id, "enabled": True,
                   "label": args.label or args.host, "roles": args.role}
            validate_registry({"schema": "borg-fleet/v1", "hosts": [row]})
            if existing is not None and any(existing[k] != row[k] for k in
                    ("ssh_alias", "home", "owner", "instance_id")):
                raise ValueError("Host ID already pins another installation; enroll a distinct ID")

            async def verify():
                fleet = Fleet(path)
                try:
                    async with asyncio.timeout(20), fleet.connection(row) as client:
                        return await fleet.verify(client, row)
                finally:
                    await fleet.close()
            try:
                identity = asyncio.run(verify())
            except Exception:
                raise RuntimeError("Fleet enrollment refused: SSH and the supplied target identity were not verified. No registry change was made.") from None
            if existing is not None:
                registry["hosts"].remove(existing)
            registry["hosts"].append(row)
        validate_registry(registry)
        # The SSH probe may have taken seconds. Re-read and merge under the same
        # lock used by private-file writers; hold it through registry commit.
        with config.private_writer(connector_path) as write_connector:
            connector, restart = prepare_connector()
            if restart:
                write_connector(json.dumps(connector, indent=2) + "\n", replace=True)
            # A config conflict/failure must never leave a host enrolled. If the
            # registry write fails, keep the compatible config upgrade for retry.
            try:
                config.write_private(path, json.dumps(registry, indent=2) + "\n", replace=path.exists())
            except (OSError, ValueError) as exc:
                if restart:
                    raise RuntimeError("Fleet registry update failed after upgrading connector settings; "
                                       "restart the connector before retrying enrollment.") from exc
                raise
        return {"state": "enrolled" if args.operation == "add" else "disabled", "host": args.host,
                "identity": identity if args.operation == "add" else None,
                "restart_connector_required": restart,
                "notice": "Enrollment does not prove workload capacity, provider login or OS permission."}
