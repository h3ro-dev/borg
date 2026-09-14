"""Operator wiring for the existing Inbox identity, assignments and fleet planner."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from .gateway import BrowserGateway
from .remote import RemoteSupervisor
from .routing import RoutedSupervisor
from .store import ResourceError


def private_config(path):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077
            or path.stat().st_size > 128 * 1024):
        raise ResourceError("invalid_config", "Browser configuration must be private and owned")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ResourceError("invalid_config", "Browser configuration must be an object")
    return value


class WorkAuthority:
    def __init__(self, store, allowed_work_ids):
        self.store = store
        if allowed_work_ids is not None and (not isinstance(allowed_work_ids, list)
                or not all(isinstance(x, str) and x for x in allowed_work_ids)):
            raise ResourceError("invalid_config", "Pilot work allowlist is invalid")
        self.allowed = set(allowed_work_ids) if allowed_work_ids is not None else None

    def record(self, actor, work_id):
        if self.allowed is not None and work_id not in self.allowed:
            return None
        # Use the supported public Store call. A missing/truncated row refuses
        # admission; it never guesses ownership from a user-supplied work id.
        rows = self.store.call(actor, "assignments.list", {"work_id": work_id})["assignments"]
        row = next((x for x in rows if x.get("work_id") == work_id and x.get("assignee") == actor), None)
        if row is None or not self.store.authorize(actor, "browser.use", row["scope"])["allowed"]:
            return None
        return row

    def allowed_work(self, actor, work_id):
        return self.record(actor, work_id) is not None

    def revision(self, actor, work_id):
        row = self.record(actor, work_id)
        return str(row["version"]) if row is not None else None

    def operator(self, actor):
        return self.store.authorize(actor, "browser.operator", "/")["allowed"] is True


class FleetAdmission:
    def __init__(self, planner, ram_bytes, cpu_units, runner=subprocess.run):
        if not isinstance(planner, str) or not Path(planner).is_absolute() or not Path(planner).is_file():
            raise ResourceError("invalid_config", "Installed fleet planner is required")
        if type(ram_bytes) is not int or ram_bytes < 256 * 1024 * 1024:
            raise ResourceError("invalid_config", "Measured browser RAM envelope is required")
        if type(cpu_units) not in (int, float) or not 0 < cpu_units <= 16:
            raise ResourceError("invalid_config", "Measured browser CPU envelope is required")
        # Keep planner and collector execution on the broker's verified runtime.
        # The system shebang can select a different macOS Python/permission context.
        self.command = [sys.executable, planner, "--live", "--ram-bytes", str(ram_bytes), "--cpu-units", str(cpu_units), "--json"]
        self.ram_bytes = ram_bytes
        self.runner = runner

    def __call__(self, host):
        result = self.runner(self.command, capture_output=True, timeout=30, check=False)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            return False
        report = json.loads(result.stdout)
        rows = report.get("machines", [])
        if len(rows) != 7 or len({r.get("name") for r in rows}) != 7:
            return False
        row = next((r for r in rows if r.get("name") == host), None)
        if row is None:
            return False
        observed = datetime.fromisoformat(row["observedAt"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        raw, resource = row.get("raw", {}), row.get("resource", {})
        pages, page_size, ram = raw.get("vm_stat_pages", {}), raw.get("vm_stat_page_size_bytes"), raw.get("physical_ram_bytes")
        counts = [pages.get(k) for k in ("active", "wired", "compressor_occupied")]
        if (type(page_size) is not int or type(ram) is not int or ram <= 0
                or any(type(x) is not int or x < 0 for x in counts)):
            return False
        used = sum(counts) * page_size / ram
        return (0 <= age <= 180 and row.get("reachable") is True and row.get("stale") is False
                and row.get("state") == "OK" and not row.get("holdReason")
                and type(row.get("loadPerCore")) in (int, float) and row["loadPerCore"] < 1
                and raw.get("kernel_pressure_level") == 1
                and used < .92 and resource.get("ram_available_bytes", 0) >= self.ram_bytes
                and resource.get("admission") in {"FITS_FLOOR", "FITS_RECLAIM"}
                and type(resource.get("effective_new_slots")) in (int, float)
                and resource["effective_new_slots"] >= 1)


def gateway_factory(config_path):
    """Read fixed operator configuration once; never accept tool-selected paths."""
    config = private_config(config_path)
    def create(service):
        authority = WorkAuthority(service.store, config.get("allowed_work_ids", []))
        admission = FleetAdmission(**config["admission"])
        if "supervisors" in config:
            if "supervisor" in config or not isinstance(config["supervisors"], dict):
                raise ResourceError("invalid_config", "Use one fixed supervisor configuration")
            supervisor = RoutedSupervisor({host: RemoteSupervisor(**options)
                                           for host, options in config["supervisors"].items()})
        else:
            supervisor = RemoteSupervisor(**config["supervisor"])
        browser = BrowserGateway(config["state_dir"], supervisor, host=config["host"],
            authorize_work=authority.allowed_work, work_revision=authority.revision,
            operator_check=authority.operator, admission_check=admission,
            profiles=config.get("profiles", {}), max_sessions=config.get("max_sessions", 2))
        if "desktop" not in config:
            return browser
        from fleet_desktop.integration import FleetGateway
        try:
            return FleetGateway(browser, service, config["desktop"])
        except BaseException:
            browser.close()
            raise
    return create
