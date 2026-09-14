from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen
import uuid


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from borg_coordination.portable import PortableError, service_document, beads_environment, init_beads


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _private(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) == 0o600


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"credential", "token", "secret"} or _contains_secret_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


class PortableCoordinationTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.homes: list[Path] = []
        self.processes: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for process in reversed(self.processes):
            self._stop(process)
        self.temporary.cleanup()

    def _stop(self, process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def _install(self, name: str) -> tuple[Path, Path]:
        home = self.root / name
        source = home / "app" / "coordination"
        source.parent.mkdir(parents=True)
        shutil.copytree(SOURCE_ROOT, source)
        python = home / "mem0" / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.homes.append(home)
        return home, source

    def _cli(
        self,
        source: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(source / "bin" / "borg-coordination"), *args],
            text=True,
            capture_output=True,
            check=check,
            timeout=20,
        )

    def _bootstrap(self, name: str, owner: str, port: int) -> tuple[Path, Path, dict]:
        home, source = self._install(name)
        completed = self._cli(
            source,
            "bootstrap",
            "--home",
            str(home),
            "--owner",
            owner,
            "--port",
            str(port),
        )
        receipt = json.loads(completed.stdout)
        self.assertFalse(_contains_secret_field(receipt), receipt)
        return home, source, receipt

    def _start(self, home: Path) -> subprocess.Popen[str]:
        service_file = home / "coordination" / "service.json"
        service = json.loads(service_file.read_text())
        environment = os.environ.copy()
        environment.update(service["env"])
        process = subprocess.Popen(
            service["args"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        self.processes.append(process)
        assert process.stdout is not None
        started = json.loads(process.stdout.readline())
        self.assertEqual(started["status"], "serving")
        return process

    def _call(
        self,
        source: Path,
        config: Path,
        operation: str,
        params: dict,
        *,
        request_id: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        params_path = self.root / f"params-{uuid.uuid4()}.json"
        params_path.write_text(json.dumps(params))
        args = [
            str(source / "comms" / "bin" / "inbox"),
            "call",
            "--config",
            str(config),
            "--operation",
            operation,
            "--params-file",
            str(params_path),
        ]
        if request_id is not None:
            args.extend(["--request-id", request_id])
        return subprocess.run(
            [sys.executable, *args],
            text=True,
            capture_output=True,
            check=check,
            timeout=20,
        )

    def _enroll(self, home: Path, source: Path, agent: str) -> dict:
        completed = self._cli(
            source,
            "enroll",
            "--home",
            str(home),
            "--agent",
            agent,
            "--runtime",
            "synthetic",
            "--machine",
            "local",
        )
        result = json.loads(completed.stdout)
        self.assertFalse(_contains_secret_field(result), result)
        return result

    def test_bootstrap_is_private_confined_idempotent_and_loopback_only(self) -> None:
        port = _free_port()
        home, source, first = self._bootstrap("home-one", "acme-owner", port)
        coordination = home / "coordination"
        config_path = coordination / "config.json"
        service_path = coordination / "service.json"
        policy_path = coordination / "owner-policy.md"
        data_path = coordination / "data"

        self.assertTrue(_private(config_path))
        self.assertTrue(_private(service_path))
        config = json.loads(config_path.read_text())
        service = json.loads(service_path.read_text())
        self.assertEqual(set(service), {"args", "env"})
        self.assertEqual(config["owner_id"], "acme-owner")
        self.assertEqual(config["borg_home"], str(home.resolve()))
        self.assertEqual(config["beads_dir"], str((home / "beads").resolve()))
        self.assertEqual(config["policy_file"], str(policy_path.resolve()))
        self.assertEqual(config["endpoint"], f"http://127.0.0.1:{port}")
        self.assertEqual(service["args"][0], str(home.resolve() / "mem0/venv/bin/python"))
        self.assertEqual(service["args"][1], str((source / "comms/bin/inbox").resolve()))
        self.assertEqual(service["args"][2:5], ["serve", "--state-dir", str(data_path.resolve())])
        self.assertIn("127.0.0.1", service["args"])
        self.assertNotIn("0.0.0.0", service["args"])
        self.assertEqual(service["env"]["BORG_COORDINATION_CONFIG"], str(config_path.resolve()))
        self.assertTrue(all(Path(value).is_absolute() for value in service["env"].values()))
        self.assertNotIn("James", policy_path.read_text())
        self.assertNotIn("Utlyze", policy_path.read_text())

        for path in (
            Path(first["owner_client_config"]),
            Path(first["connector_client_config"]),
            Path(first["owner_credential_file"]),
            Path(first["connector_credential_file"]),
        ):
            path.resolve().relative_to(data_path.resolve())
            self.assertTrue(_private(path), path)

        second = json.loads(
            self._cli(
                source,
                "bootstrap",
                "--home",
                str(home),
                "--owner",
                "acme-owner",
                "--port",
                str(port),
            ).stdout
        )
        self.assertEqual(first["connector_client_config"], second["connector_client_config"])
        conflict = self._cli(
            source,
            "bootstrap",
            "--home",
            str(home),
            "--owner",
            "different-owner",
            "--port",
            str(port),
            check=False,
        )
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("owner_conflict", conflict.stderr)

        environment = os.environ.copy()
        environment.update(service["env"])
        policy_probe = (
            "import json; from comms.hub.policy import read_policy; "
            "print(json.dumps(read_policy(include_body=True)))"
        )
        verified = json.loads(
            subprocess.run(
                [sys.executable, "-c", policy_probe],
                text=True,
                capture_output=True,
                check=True,
                env=environment,
            ).stdout
        )
        self.assertEqual(verified["state"], "verified")
        self.assertIn("authenticated grants", verified["body"].lower())
        policy_path.write_text(policy_path.read_text() + "\nOwner-added rule.\n")
        mismatch = json.loads(
            subprocess.run(
                [sys.executable, "-c", policy_probe],
                text=True,
                capture_output=True,
                check=True,
                env=environment,
            ).stdout
        )
        self.assertEqual(mismatch["state"], "hash_mismatch")
        with self.assertRaises(PortableError) as policy_error:
            service_document(home)
        self.assertEqual(policy_error.exception.code, "policy_changed")
        pinned = json.loads(
            self._cli(source, "policy-pin", "--home", str(home)).stdout
        )
        self.assertEqual(pinned["status"], "pinned")
        verified_again = json.loads(
            subprocess.run(
                [sys.executable, "-c", policy_probe],
                text=True,
                capture_output=True,
                check=True,
                env=environment,
            ).stdout
        )
        self.assertEqual(verified_again["state"], "verified")

        clean_environment = os.environ.copy()
        clean_environment.pop("BORG_COORDINATION_CONFIG", None)
        native_policy = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    str(source / "comms/bin/inbox"),
                    "stdio",
                    "--config",
                    first["owner_client_config"],
                ],
                input=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "inbox_policy", "arguments": {}},
                    }
                )
                + "\n",
                text=True,
                capture_output=True,
                check=True,
                env=clean_environment,
            ).stdout
        )
        structured = native_policy["result"]["structuredContent"]
        self.assertEqual(structured["state"], "verified")
        self.assertIn("Owner-added rule", structured["body"])

    def test_beads_uses_pinned_upstream_native_init(self) -> None:
        home, source, _ = self._bootstrap("beads-home", "beads-owner", _free_port())
        command_receipt = json.loads(
            self._cli(source, "beads-command", "--home", str(home)).stdout
        )
        self.assertEqual(command_receipt["cwd"], str((home / "beads").resolve()))
        command = command_receipt["command"]
        self.assertEqual(
            command,
            [
                "bd",
                "init",
                "--non-interactive",
                "--init-if-missing",
                "--skip-agents",
                "--skip-hooks",
                "--prefix",
                "beads-owner",
            ],
        )
        initialized = json.loads(
            self._cli(source, "init-beads", "--home", str(home)).stdout
        )
        self.assertEqual(initialized["version"], "1.2.2")
        self.assertTrue((home / "beads/.beads").is_dir())
        repeated = json.loads(
            self._cli(source, "init-beads", "--home", str(home)).stdout
        )
        self.assertEqual(repeated["status"], "initialized")
        self.assertEqual(initialized["project_id"], repeated["project_id"])

    def test_beads_environment_preserves_system_home_and_discards_foreign_routing(self) -> None:
        environment = {**os.environ, "BEADS_DIR": str(self.root / "foreign"),
                       "BEADS_DOLT_SERVER_MODE": "1", "DOLT_ROOT_PATH": str(self.root),
                       "BD_DB": str(self.root / "foreign.db")}
        with patch.dict(os.environ, environment, clear=True):
            actual = beads_environment(self.root / "own")
        self.assertEqual(actual["BEADS_DIR"], str((self.root / "own/beads/.beads").resolve()))
        self.assertEqual(actual.get("HOME"), os.environ.get("HOME"))
        for key in ("BEADS_DOLT_SERVER_MODE", "DOLT_ROOT_PATH", "BD_DB"):
            self.assertNotIn(key, actual)

    def test_beads_redirect_is_refused_before_upstream_execution(self) -> None:
        home, _, _ = self._bootstrap("redirect-home", "redirect-owner", _free_port())
        directory = home / "beads/.beads"
        directory.mkdir(parents=True)
        redirect = directory / "redirect"
        redirect.write_text(str(self.root / "foreign"))
        with patch("borg_coordination.portable.subprocess.run") as execute:
            with self.assertRaises(PortableError):
                init_beads(home)
            execute.assert_not_called()
        self.assertEqual(redirect.read_text(), str(self.root / "foreign"))

    def test_nested_beads_storage_redirect_is_preserved_and_refused(self) -> None:
        home, _, _ = self._bootstrap("nested-home", "nested-owner", _free_port())
        storage = home / "beads/.beads/embeddeddolt/nested_owner"
        storage.mkdir(parents=True)
        foreign = self.root / "foreign-storage"
        foreign.mkdir()
        (foreign / "owner-data").write_text("preserved")
        target = storage / ".dolt"
        target.symlink_to(foreign, target_is_directory=True)
        with patch("borg_coordination.portable.subprocess.run") as execute:
            with self.assertRaises(PortableError):
                init_beads(home)
            execute.assert_not_called()
        self.assertTrue(target.is_symlink())
        self.assertEqual((foreign / "owner-data").read_text(), "preserved")

    def test_native_protocol_isolated_replay_safe_and_persistent(self) -> None:
        first_port, second_port = _free_port(), _free_port()
        first_home, first_source, first_receipt = self._bootstrap(
            "first-home", "first-owner", first_port
        )
        second_home, second_source, _ = self._bootstrap(
            "second-home", "second-owner", second_port
        )
        first_process = self._start(first_home)
        second_process = self._start(second_home)

        status = json.loads(
            self._cli(first_source, "status", "--home", str(first_home)).stdout
        )
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["principal"], "first-owner")
        self.assertNotIn("credential", json.dumps(status).lower())

        alpha = self._enroll(first_home, first_source, "agent-alpha")
        beta = self._enroll(first_home, first_source, "agent-beta")
        alpha_config = Path(alpha["client_config"])
        beta_config = Path(beta["client_config"])
        owner_config = Path(first_receipt["owner_client_config"])
        self.assertNotEqual(alpha_config, beta_config)

        send_id = str(uuid.uuid4())
        send_params = {
            "to": ["agent-beta"],
            "kind": "information",
            "subject": "synthetic handoff",
            "body": "fixture only",
            "scope": "/fixture",
        }
        sent = json.loads(
            self._call(
                first_source,
                alpha_config,
                "messages.send",
                send_params,
                request_id=send_id,
            ).stdout
        )
        replay = json.loads(
            self._call(
                first_source,
                alpha_config,
                "messages.send",
                send_params,
                request_id=send_id,
            ).stdout
        )
        self.assertEqual(sent, replay)

        read = json.loads(
            self._call(
                first_source,
                beta_config,
                "messages.get",
                {"message_id": sent["message"]["id"]},
            ).stdout
        )
        self.assertEqual(read["message"]["subject"], "synthetic handoff")

        polled = json.loads(
            self._call(
                first_source,
                beta_config,
                "messages.poll",
                {"limit": 1, "lease_seconds": 30, "reconcile": True},
                request_id=str(uuid.uuid4()),
            ).stdout
        )
        message = polled["messages"][0]
        self.assertEqual(message["id"], sent["message"]["id"])
        acknowledged = json.loads(
            self._call(
                first_source,
                beta_config,
                "messages.ack",
                {
                    "message_id": message["id"],
                    "state": "acknowledged",
                    "lease_id": message["delivery"]["lease_id"],
                    "receipt_ref": "synthetic-receipt",
                },
                request_id=str(uuid.uuid4()),
            ).stdout
        )
        self.assertEqual(acknowledged["delivery"]["state"], "acknowledged")

        assigned = json.loads(
            self._call(
                first_source,
                owner_config,
                "assignments.assign",
                {
                    "work_id": "fixture-work",
                    "assignee": "agent-alpha",
                    "scope": "/fixture",
                    "summary": "synthetic assignment",
                },
                request_id=str(uuid.uuid4()),
            ).stdout
        )
        self.assertEqual(assigned["assignment"]["version"], 1)
        reassigned = json.loads(
            self._call(
                first_source,
                owner_config,
                "assignments.reassign",
                {
                    "work_id": "fixture-work",
                    "assignee": "agent-beta",
                    "scope": "/fixture",
                    "summary": "synthetic transfer",
                    "expected_version": 1,
                },
                request_id=str(uuid.uuid4()),
            ).stdout
        )
        self.assertEqual(reassigned["assignment"]["version"], 2)
        stale = self._call(
            first_source,
            owner_config,
            "assignments.reassign",
            {
                "work_id": "fixture-work",
                "assignee": "agent-alpha",
                "scope": "/fixture",
                "summary": "stale transfer",
                "expected_version": 1,
            },
            request_id=str(uuid.uuid4()),
            check=False,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("version_conflict", stale.stderr)

        wrong_file = first_home / "coordination/data/clients/wrong.credential.json"
        wrong_file.write_text(json.dumps({"credential": "not-a-real-credential"}))
        os.chmod(wrong_file, 0o600)
        wrong_config = first_home / "coordination/data/clients/wrong.config.json"
        wrong_config.write_text(
            json.dumps(
                {
                    "endpoint": f"http://127.0.0.1:{first_port}",
                    "credential_file": str(wrong_file),
                    "agent_id": "agent-alpha",
                }
            )
        )
        denied = self._call(
            first_source,
            wrong_config,
            "agents.list",
            {},
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('"code":"unauthorized"', denied.stderr)

        foreign_config = first_home / "coordination/data/clients/foreign.config.json"
        foreign = json.loads(alpha_config.read_text())
        foreign["endpoint"] = f"http://127.0.0.1:{second_port}"
        foreign_config.write_text(json.dumps(foreign))
        denied_foreign = self._call(
            first_source,
            foreign_config,
            "agents.list",
            {},
            check=False,
        )
        self.assertNotEqual(denied_foreign.returncode, 0)
        self.assertIn('"code":"unauthorized"', denied_foreign.stderr)

        self._stop(first_process)
        self.processes.remove(first_process)
        self._start(first_home)
        after_restart = json.loads(
            self._call(
                first_source,
                owner_config,
                "assignments.list",
                {"work_id": "fixture-work"},
            ).stdout
        )
        self.assertEqual(after_restart["assignments"][0]["version"], 2)
        self.assertEqual(after_restart["assignments"][0]["assignee"], "agent-beta")
        self.assertIsNone(second_process.poll())


if __name__ == "__main__":
    unittest.main()
