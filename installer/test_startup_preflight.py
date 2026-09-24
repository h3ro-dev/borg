"""Selected-port preflight and start output, using temporary homes and this test's own loopback listeners."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from installer import blueprint, cli, config, installation, services


def free_base() -> int:
    """Reserve and release a range without depending on the code under test."""
    span = max(config.PORT_OFFSETS.values()) + 1
    for base in range(38000, 60000, span * 3):
        with contextlib.ExitStack() as stack:
            try:
                for offset in range(span):
                    sock = stack.enter_context(socket.socket())
                    sock.bind(("127.0.0.1", base + offset))
            except OSError:
                continue
            return base
    raise unittest.SkipTest("No free loopback port range for this test")


@contextlib.contextmanager
def listener(port: int):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen()
        yield


class StartupPreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "home"
        self.port_base = free_base()

    def doc(self, **extra):
        ports = {name: self.port_base + offset for name, offset in config.PORT_OFFSETS.items()}
        return {"home": str(self.root), "ports": ports, "external_access": {"enabled": False}, **extra}

    def test_cli_refuses_occupied_selected_port_before_owner_state_or_downloads(self):
        args = ["install", "--home", str(self.root), "--owner", "owner", "--port-base", str(self.port_base)]
        memory = self.port_base + config.PORT_OFFSETS["memory"]
        stderr = io.StringIO()
        with listener(memory), \
             patch.object(config, "initialize", side_effect=AssertionError("created owner state")), \
             patch.object(installation.dependencies, "prepare", side_effect=AssertionError("downloaded")), \
             contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(args), 1)
            self.assertEqual(cli.main([*args, "--validate-only"]), 1)
        self.assertIn(f"{memory} (memory)", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(self.root.exists())

    def test_install_refuses_before_connector_config_and_dependencies(self):
        doc = self.doc(instance_id="00000000-0000-4000-8000-000000000000")
        with listener(doc["ports"]["connector"]), \
             patch.object(installation, "preflight"), \
             patch.object(services, "running", return_value=False), \
             patch.object(config, "write_connector_config", side_effect=AssertionError("wrote config")), \
             patch.object(installation.dependencies, "prepare", side_effect=AssertionError("downloaded")), \
             self.assertRaisesRegex(RuntimeError, "connector"):
            installation.install(doc)

    def test_shell_entry_refuses_before_bootstrap_writes_or_downloads(self):
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        curl = bin_dir / "curl"
        curl.write_text('#!/bin/sh\n: > "$BORG_TEST_DOWNLOADED"\nexit 81\n')
        curl.chmod(0o700)
        downloaded = self.base / "download-attempted"
        script = Path(__file__).resolve().parents[1] / "install.sh"
        with listener(self.port_base + config.PORT_OFFSETS["qdrant_grpc"]):
            result = subprocess.run(["/bin/sh", str(script), "--home", str(self.root),
                                     "--owner", "owner", "--port-base", str(self.port_base)],
                                    capture_output=True, text=True, timeout=10,
                                    env={**os.environ, "PATH": str(bin_dir) + ":/usr/bin:/bin",
                                         "BORG_TEST_DOWNLOADED": str(downloaded)})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("already in use", result.stderr)
        self.assertFalse(self.root.exists())
        self.assertFalse(downloaded.exists())

    def test_qdrant_grpc_port_conflict_is_refused(self):
        doc = self.doc()
        with listener(doc["ports"]["qdrant_grpc"]), self.assertRaisesRegex(RuntimeError, r"\(qdrant\)"):
            services.check_ports(doc)

    def test_unselected_services_do_not_block(self):
        doc = self.doc()
        with listener(doc["ports"]["gateway"]), listener(doc["ports"]["tunnel_metrics"]):
            services.check_ports(doc)  # External access is not enabled.
        tools = {"profile": "tools", "components": ["beads"]}
        with patch.object(blueprint, "selected_machine", return_value=tools), \
             listener(doc["ports"]["memory"]), listener(doc["ports"]["inbox"]), listener(doc["ports"]["conductor"]):
            services.check_ports(doc)
            with listener(doc["ports"]["connector"]), self.assertRaisesRegex(RuntimeError, "connector"):
                services.check_ports(doc)
        with listener(doc["ports"]["inbox"]), self.assertRaisesRegex(RuntimeError, "inbox"):
            services.check_ports(doc)

    def test_this_instance_may_rerun_but_an_unowned_listener_is_refused(self):
        doc = self.doc(instance_id="00000000-0000-4000-8000-000000000000")
        with listener(doc["ports"]["memory"]):
            with patch.object(services, "running", side_effect=lambda d, name: name == "memory"):
                services.check_ports(doc)
            with patch.object(services, "running", return_value=False), self.assertRaisesRegex(RuntimeError, "memory"):
                services.check_ports(doc)
        # Without an instance the running check is never consulted.
        with listener(doc["ports"]["memory"]), \
             patch.object(services, "running", side_effect=AssertionError("consulted")), \
             self.assertRaises(RuntimeError):
            services.check_ports(self.doc())

    def test_start_rechecks_ports_for_a_race_and_installs_nothing(self):
        doc = self.doc(instance_id="00000000-0000-4000-8000-000000000000")
        rows = {"qdrant": {"port": doc["ports"]["qdrant"], "label": "fixture"}}
        with listener(doc["ports"]["qdrant_grpc"]), \
             patch.object(services, "specifications", return_value=rows), \
             patch.object(services, "running", return_value=False), \
             patch.object(services, "install_definition", side_effect=AssertionError("installed")), \
             self.assertRaisesRegex(RuntimeError, "occupied outside this BORG service: qdrant"):
            services.start(doc, ["qdrant"])

    def test_start_output_reports_process_state_without_claiming_readiness(self):
        doc = config.initialize(self.root, "owner", port_base=self.port_base)
        out = io.StringIO()
        with patch.object(services, "start", return_value={"qdrant": "running", "memory": "starting"}), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["start", "--home", str(self.root)]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["state"], "started_not_verified")
        self.assertEqual(result["readiness"], "not_verified")
        self.assertEqual(result["processes"], {"qdrant": "process_running", "memory": "process_starting"})
        self.assertEqual(result["verify"], str(Path(doc["home"]) / "bin/borg") + " doctor")
        self.assertNotIn('"ready"', out.getvalue())
        self.assertNotIn('"running"', out.getvalue())


if __name__ == "__main__":
    unittest.main()
