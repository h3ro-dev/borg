"""Native BORG remote execution over the owner's existing SSH estate."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import stat
from pathlib import Path

from fastmcp.exceptions import ToolError

from job_tools import JobStore

TOOL_NAMES = ["remote_list_hosts", "remote_status", "remote_start", "remote_read_output", "remote_cancel"]
HOST_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class RemoteStore:
    def __init__(self, config: dict):
        from runtime_paths import borg_home
        self.config = config
        self.hosts_path = Path(config.get("hosts_file") or (borg_home() / "borg-context/hosts.json"))
        self.root = Path(config.get("jobs_root") or (borg_home() / "borg-context")) / "remote"
        self.jobs = JobStore(self.root)

    def _hosts(self) -> list[dict]:
        try:
            info = self.hosts_path.stat()
            if (self.hosts_path.is_symlink() or info.st_size > 128_000 or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode)):
                raise ValueError
            value = json.loads(self.hosts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise ToolError("BORG remote host registry is unavailable") from None
        if not isinstance(value, list):
            raise ToolError("BORG remote host registry is invalid")
        rows = []
        for row in value:
            if not isinstance(row, dict) or not HOST_NAME.fullmatch(str(row.get("name", ""))):
                continue
            if not row.get("enabled", True):
                continue
            rows.append(row)
        return rows

    def _host(self, name: str) -> dict:
        if not HOST_NAME.fullmatch(str(name)):
            raise ToolError("BORG remote host name is invalid")
        for row in self._hosts():
            if row.get("name") == name:
                return row
        raise ToolError("BORG remote host is not configured")

    @staticmethod
    def _probe(row: dict) -> tuple[str, str]:
        if row.get("backend") == "local":
            cpu_count = os.cpu_count() or 1
            load = os.getloadavg()[0] / cpu_count
            return ("admission_denied", "load_high") if load >= 1.5 else ("ready", "local")
        alias = str(row.get("ssh_alias") or row.get("name"))
        try:
            result = subprocess.run(["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", alias,
                                     "/usr/sbin/sysctl", "-n", "hw.ncpu", "vm.loadavg"],
                                    capture_output=True, text=True, timeout=7, check=False)
        except subprocess.TimeoutExpired:
            return "timeout", "ssh_timeout"
        except OSError:
            return "unavailable", "ssh_unavailable"
        if result.returncode != 0:
            return "unavailable", "ssh_failed"
        lines = result.stdout.splitlines()
        try:
            cpu_count = max(1, int(lines[0].strip()))
            load = float(lines[1].strip().strip("{} ").split()[0])
        except (IndexError, ValueError):
            return "unknown", "resource_probe_invalid"
        return ("admission_denied", "load_high") if load / cpu_count >= 1.5 else ("ready", "ssh")

    def list_hosts(self) -> dict:
        rows = []
        for row in self._hosts():
            status, reason = self._probe(row)
            rows.append({"name": row["name"], "backend": row.get("backend", "ssh"),
                         "capabilities": row.get("capabilities", ["jobs"]),
                         "status": status, "reason": reason})
        return {"hosts": rows, "notice": "Host status is a fresh probe; it is not proof that a later command completed."}

    def status(self, name: str) -> dict:
        row = self._host(name); status, reason = self._probe(row)
        return {"name": row["name"], "backend": row.get("backend", "ssh"), "status": status,
                "reason": reason, "capabilities": row.get("capabilities", ["jobs"])}

    def start(self, name: str, command: str, timeout_ms: int = 1000) -> dict:
        row = self._host(name)
        if not isinstance(command, str) or not command.strip() or len(command) > 16_000:
            raise ToolError("BORG remote command is empty or too long")
        status, reason = self._probe(row)
        if status != "ready":
            raise ToolError(f"BORG remote host is not ready: {reason}")
        if row.get("backend") == "local":
            return {**self.jobs.start(command, timeout_ms=timeout_ms), "host": row["name"]}
        alias = str(row.get("ssh_alias") or row["name"])
        # Pass the complete remote shell invocation as one SSH argument. SSH
        # joins argv before invoking the remote shell; splitting ``-c`` and
        # the script would make the first script token become zsh's $0.
        remote_script = "/bin/zsh -lc " + shlex.quote(command)
        wrapped = "/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=4 " + shlex.quote(alias) + " " + shlex.quote(remote_script)
        return {**self.jobs.start(wrapped, timeout_ms=timeout_ms), "host": row["name"]}

    def read_output(self, job_id: str, stream: str = "combined", offset: int = 0, length: int = 256_000) -> dict:
        return self.jobs.read_output(job_id, stream, offset, length)

    def cancel(self, job_id: str) -> dict:
        return self.jobs.cancel(job_id)


def mount_remote(server, config, handoff=None):
    store = RemoteStore(config)
    descriptions = {
        "remote_list_hosts": "Probe configured BORG remote hosts and return fresh identity-free readiness metadata.",
        "remote_status": "Probe one configured BORG remote host without running a user command.",
        "remote_start": "Start a bounded command job on one admitted BORG remote host over the existing owner SSH estate.",
        "remote_read_output": "Read credential-scrubbed output from one BORG remote job.",
        "remote_cancel": "Cancel one owned BORG remote job after process identity verification.",
    }
    annotations = {
        "remote_list_hosts": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "remote_status": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "remote_start": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        "remote_read_output": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "remote_cancel": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    }
    methods = {"remote_list_hosts": "list_hosts", "remote_status": "status", "remote_start": "start",
               "remote_read_output": "read_output", "remote_cancel": "cancel"}
    for name in TOOL_NAMES:
        function = getattr(store, methods[name])
        if handoff:
            function = handoff.store_function(store, name, function)
        server.tool(name=name, annotations=annotations[name], description=descriptions[name])(function)
    return store
