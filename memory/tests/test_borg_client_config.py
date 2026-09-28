"""The real fleet driver and curl load on a tools home with no local models."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin"


class ClientConfigSubprocessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / "tools home"
        self.home.mkdir(mode=0o700)
        (self.home / "mem0").mkdir(mode=0o700)
        self.config = self.home / "config.json"
        self.document = {"schema": "borg-install/v1", "home": str(self.home),
                         "owner": "james", "instance_id": "00000000-0000-4000-8000-000000000001"}
        self.save()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("MEM0_") and key not in {"BORG_HOME", "BORG_OWNER_ID"}}
        self.env.update({"BORG_HOME": str(self.home), "BORG_OWNER_ID": "james",
                         "MEM0_FLEET_BASE": str(self.home / "mem0"),
                         "MEM0_FLEET_TOKEN_FILE": str(self.home / "mem0/data/fleet-token"),
                         "MEM0_MACHINE": "studio-test", "MEM0_HARNESS": "codex",
                         "PYTHONDONTWRITEBYTECODE": "1"})

    def save(self):
        self.config.write_text(json.dumps(self.document))
        self.config.chmod(0o600)

    def call(self, name: str, *args: str, env: dict | None = None):
        return subprocess.run([sys.executable, "-B", str(BIN / name), *args],
                              input="{}", env=env or self.env, capture_output=True,
                              text=True, timeout=20, check=False)

    def test_real_driver_and_curl_load_without_local_model_ids(self):
        self.assertNotIn("BORG_EXTRACTION_MODEL_ID", self.document)
        self.assertNotIn("BORG_EMBED_MODEL_ID", self.document)
        replay = self.call("mem0-fleet-hook", "replay")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        environment = {**self.env, "MEM0_FLEET_TEST_CAPTURE_JSON": json.dumps({
            "principal": "bounded-test", "allowed_scopes": ["personal:james"],
            "write_scope": "personal:james"})}
        who = self.call("mem0-mcp-curl", "memory_whoami", "{}", env=environment)
        self.assertEqual(who.returncode, 0, who.stderr)
        self.assertEqual(json.loads(who.stdout)["principal"], "bounded-test")

    def test_native_hook_and_curl_leave_reviewed_release_without_bytecode(self):
        """Direct shebang entry points do not inherit the installer's -B flag."""
        source_bin = self.home.parent / "reviewed-release" / "memory" / "bin"
        source_bin.mkdir(parents=True)
        for name in ("borg_client_config.py", "memory_selection.py",
                     "mem0-fleet-hook", "mem0-mcp-curl"):
            shutil.copy2(BIN / name, source_bin / name)
        environment = dict(self.env)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)
        environment["MEM0_FLEET_TEST_SEARCH_JSON"] = "[]"
        payload = {"session_id": "direct-hook-test", "turn_id": "turn-1",
                   "prompt": "What is the Alder paper lantern color?", "cwd": str(self.home)}
        start = subprocess.run(
            [sys.executable, str(source_bin / "mem0-fleet-hook"), "start"],
            input=json.dumps(payload), env=environment, capture_output=True,
            text=True, timeout=20, check=False,
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        log = self.home / "mem0/data/fleet-hook.log"
        events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(any(row.get("event") == "start" and row.get("status") == "PASS_EMPTY"
                            for row in events), events)
        environment["MEM0_FLEET_TEST_CAPTURE_JSON"] = json.dumps({
            "principal": "bounded-test", "allowed_scopes": ["personal:james"],
            "write_scope": "personal:james"})
        who = subprocess.run(
            [sys.executable, str(source_bin / "mem0-mcp-curl"), "memory_whoami", "{}"],
            env=environment, capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(who.returncode, 0, who.stderr)
        self.assertEqual(json.loads(who.stdout)["principal"], "bounded-test")
        self.assertEqual(list(source_bin.rglob("__pycache__")), [])
        self.assertEqual(list(source_bin.rglob("*.pyc")), [])

    def test_wrong_owner_home_base_or_symlink_refuses_before_client_load(self):
        cases = []
        self.document["owner"] = "someone-else"
        self.save()
        cases.append(self.call("mem0-fleet-hook", "replay"))
        self.document["owner"] = "james"
        self.document["home"] = str(self.home.parent / "foreign-home")
        self.save()
        cases.append(self.call("mem0-fleet-hook", "replay"))
        self.document["home"] = str(self.home)
        self.save()
        cases.append(self.call("mem0-fleet-hook", "replay", env={**self.env,
            "MEM0_FLEET_BASE": str(self.home / "foreign-base")}))
        alias = self.home.parent / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        cases.append(self.call("mem0-mcp-curl", "memory_whoami", "{}", env={**self.env,
            "BORG_HOME": str(alias), "MEM0_FLEET_TEST_CAPTURE_JSON": "{}"}))
        for result in cases:
            with self.subTest(stderr=result.stderr[:120]):
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("bounded-test", result.stdout)


if __name__ == "__main__":
    unittest.main()
