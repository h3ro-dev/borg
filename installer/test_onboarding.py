"""Onboarding must describe real source contracts without touching owner secrets."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from installer import config, onboarding
from installer.cli import parser


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "owner borg"
        self.root.mkdir(mode=0o700)
        self.doc = {"schema": config.SCHEMA, "home": str(self.root), "owner": "new-owner",
                    "instance_id": str(uuid.uuid4()), "projects": [str(self.root)],
                    "ports": {key: 19760 + value for key, value in config.PORT_OFFSETS.items()},
                    "models": {}, "external_access": {"enabled": False}}

    def write(self, relative, value, *, mode=0o600):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        path.chmod(mode)
        return path

    def configured(self):
        self.write("bin/borg", "#!/bin/sh\nexit 0\n", mode=0o700)
        self.write("app/borg.py", "# fixture\n")
        node = self.write("runtime/node/bin/node", "#!/bin/sh\nexit 0\n", mode=0o700)
        conductor = {"schemaVersion": 1, "borgHome": str(self.root), "owner": self.doc["owner"],
                     "instance_id": self.doc["instance_id"], "runtime": {"nodeBin": str(node)},
                     "conductors": [{"id": "primary", "codexHome": str(self.root / "conductors/primary/profile"),
                                     "accountPin": "a" * 64}],
                     "providers": {"claude": {"enabled": True}, "grok": {"enabled": True}}}
        self.write("conductors/config.json", conductor)
        self.write("conductors/primary/borg-client-receipt.json", {
            "state": "configured", "profile": str(self.root / "conductors/primary/profile"),
            "hooks": 4, "native_trust_verified": True, "mcp_server": "borg", "account_credentials_imported": False})
        return conductor

    def steps(self):
        return {step["id"]: step for step in onboarding.plan(self.doc)["steps"]}

    def test_clean_owner_has_ordered_serializable_actionable_steps(self):
        result = onboarding.plan(self.doc)
        self.assertEqual(result["schema"], "borg-onboarding/v1")
        self.assertEqual([step["id"] for step in result["steps"]], [
            "installation", "local-services", "codex-login", "codex-client", "codex-conductor",
            "claude", "grok", "cursor", "adapters", "web-connector", "own-machines", "os-permissions"])
        self.assertEqual(result["state"], "action_required")
        self.assertIsNone(result["ready"])
        self.assertEqual(result["steps"][0]["state"], "missing")
        self.assertEqual(result["steps"][0]["commands"][0]["argv"],
                         ["./install.sh", "--home", str(self.root), "--owner", "new-owner"])
        self.assertIn("Native login", self.steps()["codex-login"]["missing_requirements"][0])
        json.dumps(result, allow_nan=False)

    def test_configured_owner_requires_live_proof(self):
        self.configured()
        result = onboarding.plan(self.doc)
        steps = self.steps()
        self.assertEqual(result["state"], "verification_required")
        self.assertEqual(steps["codex-client"]["state"], "configured")
        self.assertEqual(steps["codex-login"]["state"], "configured")
        self.assertEqual(steps["codex-conductor"]["state"], "configured")
        self.assertEqual(len(steps["codex-conductor"]["commands"]), 2)
        self.assertEqual(steps["claude"]["state"], "manual_setup_required")
        self.assertEqual(steps["grok"]["state"], "manual_setup_required")
        self.assertEqual(steps["cursor"]["state"], "manual_setup_required")
        self.assertFalse(any(step["ready"] is True for step in steps.values()))
        self.assertIsNone(result["ready"])

    def test_pin_uses_the_conductor_digest_contract(self):
        conductor = self.configured()
        for pin in [None, "", "sha256:" + "a" * 64, True, "not-a-digest"]:
            conductor["conductors"][0]["accountPin"] = pin
            self.write("conductors/config.json", conductor)
            self.assertFalse(self.steps()["codex-login"]["observed"]["account_pin_present"])

    def test_optional_native_commands_target_dedicated_profiles(self):
        steps = self.steps()
        for provider, variable in [("claude", "CLAUDE_CONFIG_DIR"), ("grok", "GROK_HOME")]:
            commands = steps[provider]["commands"]
            native = [command for command in commands if command["argv"][0] == provider]
            self.assertEqual(len(native), 3)
            for command in native:
                self.assertEqual(command["env"], {variable: str(self.root / "providers" / provider / "profile")})
                self.assertEqual(command["cwd"], str(self.root))
                self.assertTrue(command["requires"])
            mcp = next(command for command in native if command["argv"][1:3] == ["mcp", "add"])
            self.assertEqual(mcp["argv"][-4:], [str(self.root / "bin/borg"), "mcp-stdio", "--home", str(self.root)])
        cursor = steps["cursor"]["commands"]
        self.assertEqual(cursor[0]["argv"][:3], ["mkdir", "-p", "-m"])
        self.assertEqual(cursor[1]["argv"], ["cursor-agent", "login"])
        self.assertEqual(cursor[2]["argv"], ["cursor-agent", "models"])
        self.assertEqual(cursor[1]["env"], {})
        self.assertNotIn("CURSOR_CONFIG_DIR", json.dumps(cursor))

    def test_partial_receipts_and_foreign_identity_never_count_as_configured(self):
        conductor = self.configured()
        self.write("conductors/primary/borg-client-receipt.json", {"state": "configured", "hooks": 4})
        self.assertEqual(self.steps()["codex-client"]["state"], "incomplete")
        for key in ["owner", "instance_id", "borgHome"]:
            foreign = {**conductor, key: "foreign-value"}
            self.write("conductors/config.json", foreign)
            result = onboarding.plan(self.doc)
            self.assertNotIn("foreign-value", json.dumps(result))
            self.assertEqual(self.steps()["codex-login"]["state"], "incomplete")
            self.assertEqual(self.steps()["codex-conductor"]["state"], "incomplete")

    def test_configuration_never_echoes_arbitrary_fields(self):
        conductor = self.configured()
        marker = "DO-NOT-EMIT-OWNER-SECRET"
        conductor["credentials"] = marker
        conductor["runtime"]["nodeBin"] = "/foreign/" + marker
        conductor["providers"]["claude"]["binary"] = "/foreign/" + marker
        self.write("conductors/config.json", conductor)
        self.write("borg-context/fleet.json", {"schema": "borg-fleet/v1", "hosts": [{"id": marker, "ssh_alias": marker}]})
        self.write("models/mlx/prepared.json", {"adapters": [{"state": "prepared_for_canary", "active": False,
                                                            "generate_command": [marker]}]})
        self.doc["external_access"] = {"enabled": True, "public_url": marker}
        result = onboarding.plan(self.doc)
        self.assertNotIn(marker, json.dumps(result))
        self.assertEqual(self.steps()["own-machines"]["observed"]["enabled_host_entries"], 1)
        self.assertEqual(self.steps()["adapters"]["state"], "prepared")
        self.assertIsNone(self.steps()["web-connector"]["ready"])

    def test_only_allowlisted_metadata_is_opened_and_no_process_or_network(self):
        self.configured()
        self.write("models/mlx/prepared.json", {"adapters": []})
        self.write("borg-context/hosts.json", [])
        for relative in ["conductors/primary/profile/auth.json", "borg-context/private/authorization",
                         "mem0/data/owner-token", "cloudflare/credentials.json"]:
            self.write(relative, "SECRET_SENTINEL")
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        original = os.open
        reads = []

        def guarded(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if not flags & os.O_DIRECTORY:
                self.assertIn(str(path), {"config.json", "borg-client-receipt.json", "prepared.json", "fleet.json"})
                reads.append(str(path))
            return original(path, flags, *args, **kwargs)

        doc_before = copy.deepcopy(self.doc)
        with patch("installer.onboarding.os.open", side_effect=guarded), \
             patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
             patch("subprocess.Popen", side_effect=AssertionError("no subprocess")), \
             patch("socket.socket", side_effect=AssertionError("no network")), \
             patch.dict(os.environ, {"HOME": "/unrelated-home", "CODEX_HOME": "/unrelated-profile",
                                    "BORG_HOME": "/unrelated-borg", "PATH": "", "CLAUDE_CONFIG_DIR": "/unrelated-claude"}):
            result = onboarding.plan(self.doc)
        self.assertEqual(len(reads), 4)
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
        self.assertNotIn("/unrelated", json.dumps(result))
        self.assertEqual(self.doc, doc_before)
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_malformed_oversized_world_readable_and_fifo_metadata_fail_closed(self):
        for content, mode in [("not json", 0o600), ("[]", 0o600), ("x" * 65537, 0o600), ("{}", 0o644),
                              ("[" * 2000, 0o600)]:
            self.write("conductors/config.json", content, mode=mode)
            self.assertEqual(self.steps()["codex-conductor"]["state"], "incomplete")
        target = self.root / "conductors/config.json"
        target.unlink()
        os.mkfifo(target, 0o600)
        self.assertEqual(self.steps()["codex-conductor"]["observed"]["metadata"], "unsafe_or_oversized")

    def test_symlink_file_directory_and_hardlink_are_rejected(self):
        foreign = self.base / "foreign"
        foreign.mkdir()
        target = foreign / "config.json"
        target.write_text('{"owner":"private-sentinel"}')
        target.chmod(0o600)
        directory = self.root / "conductors"
        directory.symlink_to(foreign, target_is_directory=True)
        self.assertEqual(self.steps()["codex-conductor"]["state"], "incomplete")
        directory.unlink()
        directory.mkdir()
        redirected = directory / "config.json"
        redirected.symlink_to(target)
        self.assertEqual(self.steps()["codex-conductor"]["state"], "incomplete")
        redirected.unlink()
        os.link(target, redirected)
        self.assertEqual(self.steps()["codex-conductor"]["observed"]["metadata"], "unsafe_or_oversized")
        self.assertNotIn("private-sentinel", json.dumps(onboarding.plan(self.doc)))

    def test_every_borg_command_uses_supported_parser_and_exact_home(self):
        self.configured()
        for step in onboarding.plan(self.doc)["steps"]:
            for command in step["commands"]:
                argv = command["argv"]
                if argv[0] == str(self.root / "bin/borg"):
                    args = parser().parse_args(argv[1:])
                    self.assertEqual(args.home, self.root)
                    self.assertNotEqual(args.command, "onboard")  # No recursive setup plan.

    def test_clean_owner_available_commands_execute_without_installing(self):
        # Exercise generated install arguments through its safe help path.
        checkout = Path(__file__).resolve().parents[1]
        command = self.steps()["installation"]["commands"][0]["argv"]
        result = subprocess.run([*command, "--help"], cwd=checkout, text=True,
                                capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_wrong_root_schema_or_owner_without_writing(self):
        for field, value in [("schema", "unknown"), ("owner", "invalid owner"), ("home", "relative")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                onboarding.plan({**self.doc, field: value})
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
