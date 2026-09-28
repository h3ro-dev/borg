"""Opt-in tools memory must bind one owned instance and native trust."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from installer import blueprint, cli, clients, config, health, onboarding, release_guard, remote_memory, services


FIXTURE = Path(__file__).with_name("fixtures") / "blueprint-v1.json"


class RemoteMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "borg home"
        value = json.loads(FIXTURE.read_text())
        value["machines"][0].update(profile="tools", components=["codex"], integrations=[])
        with patch.object(blueprint, "runtime_platform", return_value="macos-arm64"):
            self.doc = config.initialize(self.root, "james", blueprint_selection={
                "input": value, "machine_id": "node-1"})
        self.profile = self.root / "conductors/primary/profile"
        self.profile.mkdir(mode=0o700)
        config.write_private(self.profile / "config.toml", "# owned profile\n")
        node = self._file("runtime/node/bin/node", executable=True)
        self._file("runtime/npm/node_modules/.bin/codex", executable=True)
        self._file("mem0/bin/mem0-fleet-configure", executable=True)
        source = Path(__file__).resolve().parents[1]
        for name in ("mem0-fleet-hook", "mem0-mcp-curl"):
            target = self.root / "mem0/bin" / name
            shutil.copy2(source / "memory/bin" / name, target)
            target.chmod(0o700)
        config.write_private(self.root / "conductors/config.json", json.dumps({
            "owner": "james", "instance_id": self.doc["instance_id"], "borgHome": str(self.root),
            "runtime": {"nodeBin": str(node)},
            "conductors": [{"id": "primary", "codexHome": str(self.profile)}]}))
        self.args = {"machine": "studio-d63163377e6a", "hub_machine": "studio0",
                     "endpoint": "https://studio0.tail.example/mcp",
                     "principal": f"borg-life-studio-d63163377e6a-{self.doc['instance_id']}",
                     "read_scopes": ["ops", "personal:james", "team:project"],
                     "write_scope": "personal:james"}
        self.env_patch = patch.object(services, "service_environment", return_value={"PATH": "/usr/bin:/bin"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.source_patch = patch.object(remote_memory, "_verify_source")
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)

    def _file(self, relative: str, *, executable: bool = False) -> Path:
        path = self.root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700 if executable else 0o600)
        return path

    def stage(self):
        return remote_memory.stage(self.doc, **self.args)

    def _reviewed_release(self) -> Path:
        source = Path(__file__).resolve().parents[1]
        release = self.root / "tools/releases" / ("a" * 40)
        for relative in ("borg.py", "installer/cli.py", "installer/remote_memory.py",
                         "memory/bin/mem0-fleet-configure", "memory/bin/mem0-fleet-hook",
                         "memory/bin/mem0-mcp-curl"):
            target = release / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        (release / "RELEASE-ALLOWLIST.json").write_text(json.dumps({
            "schema": release_guard.ALLOWLIST_SCHEMA, "entries": []}))
        adapters = []
        for name in sorted(("graphiti-extraction-qwen3-1.7b", "graphiti-extraction-qwen3-4b",
                            "capture-extraction-qwen3-4b")):
            relative = f"adapters/{name}/adapters.safetensors"
            payload = ("fixture:" + name).encode()
            target = release / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(payload)
            adapters.append({"name": name, "weights_present": True, "active": False,
                "weights": {"path": relative, "bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest()}})
        (release / "adapters/MANIFEST.json").write_text(json.dumps({
            "schema": "borg-adapters/v2", "distribution": {"weights_included": True},
            "adapters": adapters}))
        for directory in (self.root / "tools", self.root / "tools/releases", release, *release.rglob("*")):
            if directory.is_dir():
                directory.chmod(0o700)
        files, findings = release_guard.scan(release, skip={"RELEASE-INVENTORY.json"},
                                              allowlist_path=release / "RELEASE-ALLOWLIST.json")
        self.assertEqual(findings, [])
        inventory = release_guard._inventory_document(files, "RELEASE-INVENTORY.json")
        (release / "RELEASE-INVENTORY.json").write_text(json.dumps(inventory))
        (release / "RELEASE-INVENTORY.json").chmod(0o600)
        return release

    def _receipt(self, *, apply: bool, trusted: bool) -> dict:
        # These values come from native hooks/list eventName and the bundled
        # configurator receipt, not the PascalCase config.toml hook keys.
        trust = {f"key-{event}": {"event": event,
                 "status_after" if apply else "status_before": "trusted"}
                 for event in ("sessionStart", "userPromptSubmit", "stop", "sessionEnd")} if trusted else {}
        if not apply:
            trust["state_upsert_required"] = not trusted
        profile = {"path": str(self.profile / "config.toml"), "trust": trust,
                   "changed_fields": [] if trusted else ["hooks.SessionStart"],
                   "missing_fields": [] if trusted else ["hooks.SessionStart"],
                   "unrelated_trust_preserved": True}
        return {"status": "PASS" if apply else "CHECK", "machine": self.args["machine"],
                "codex": {"selected": 1, "changed": 0 if trusted else 1,
                          "profiles": [profile]}}

    def test_stage_is_private_idempotent_and_never_prints_token(self):
        first = self.stage()
        second = self.stage()
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "STAGED_NEEDS_GRANT")
        token_path = self.root / "mem0/data/fleet-token"
        token = token_path.read_text().strip()
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), first["token_sha256"])
        self.assertNotIn(token, json.dumps(first))
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "mem0/data/remote-client.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(first["read_scopes"], ["ops", "personal:james", "team:project"])
        self.assertEqual((self.profile / "config.toml").read_text(), "# owned profile\n")

    def test_stage_refuses_foreign_route_and_out_of_policy_scope(self):
        for changed in ({"endpoint": "http://127.0.0.1:8795/mcp"},
                        {"endpoint": "https://user:pass@studio0.tail.example/mcp"},
                        {"read_scopes": ["*"]}, {"write_scope": "team:project"},
                        {"principal": "someone-else"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                remote_memory.stage(self.doc, **(self.args | changed))
        self.assertFalse((self.root / "mem0/data/fleet-token").exists())
        self.stage()
        with self.assertRaisesRegex(ValueError, "binding differs"):
            remote_memory.stage(self.doc, **(self.args | {"endpoint": "https://other.tail.example/mcp"}))
        manifest = self.root / "mem0/data/remote-client.json"
        manifest.unlink()
        with self.assertRaisesRegex(ValueError, "Unrecognized remote memory route"):
            self.stage()

    def test_check_requires_exact_whoami_and_four_native_trusted_hooks(self):
        self.stage()
        def operation(args, env, *, timeout):
            if Path(args[0]).name == "mem0-mcp-curl":
                return {"principal": self.args["principal"], "allowed_scopes": self.args["read_scopes"],
                        "write_scope": self.args["write_scope"]}
            return self._receipt(apply=False, trusted=True)
        with patch.object(remote_memory, "_run", side_effect=operation):
            result = remote_memory.check(self.doc)
        self.assertEqual(result["state"], "VERIFIED")
        self.assertTrue(result["route_authenticated"])
        self.assertTrue(result["native_trust_verified"])
        self.assertEqual(result["lifecycle_e2e"], "NOT_VERIFIED")
        self.assertEqual(remote_memory.EVENTS, {"sessionStart", "userPromptSubmit", "stop", "sessionEnd"})
        with patch.object(remote_memory, "_run", side_effect=lambda args, env, timeout: (
                {"principal": "foreign", "allowed_scopes": self.args["read_scopes"],
                 "write_scope": self.args["write_scope"]} if Path(args[0]).name == "mem0-mcp-curl"
                else self._receipt(apply=False, trusted=True))):
            self.assertEqual(remote_memory.check(self.doc)["state"], "NEEDS_GRANT")

    def test_enable_requires_grant_before_native_write_and_reports_refusal(self):
        self.stage()
        calls = []
        def no_grant(args, env, *, timeout):
            calls.append(args)
            return {"principal": "foreign"}
        with patch.object(remote_memory, "_run", side_effect=no_grant):
            result = remote_memory.enable(self.doc)
        self.assertEqual(result["state"], "NEEDS_GRANT")
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0][0]).name, "mem0-mcp-curl")
        def refused(args, env, *, timeout):
            if Path(args[0]).name == "mem0-mcp-curl":
                return {"principal": self.args["principal"], "allowed_scopes": self.args["read_scopes"],
                        "write_scope": self.args["write_scope"]}
            if "--check" not in args:
                raise remote_memory.NativeRefusal("native 403")
            return self._receipt(apply=False, trusted=False)
        with patch.object(remote_memory, "_run", side_effect=refused):
            result = remote_memory.enable(self.doc)
        self.assertEqual(result["state"], "NATIVE_REFUSAL")
        self.assertNotIn("403", json.dumps(result))

    def test_enable_uses_native_check_apply_readback_and_keeps_e2e_unverified(self):
        self.stage()
        calls = []
        def operation(args, env, *, timeout):
            calls.append(args)
            if Path(args[0]).name == "mem0-mcp-curl":
                return {"principal": self.args["principal"], "allowed_scopes": self.args["read_scopes"],
                        "write_scope": self.args["write_scope"]}
            if "--check" not in args:
                return self._receipt(apply=True, trusted=True)
            return self._receipt(apply=False, trusted=len(calls) > 3)
        with patch.object(remote_memory, "_run", side_effect=operation):
            result = remote_memory.enable(self.doc)
        self.assertEqual(result["state"], "VERIFIED")
        self.assertEqual(result["lifecycle_e2e"], "NOT_VERIFIED")
        self.assertEqual(["--check" in args for args in calls[1:]], [True, False, True])
        self.assertEqual((self.profile / "config.toml").read_text(), "# owned profile\n")

    def test_timed_out_apply_is_uncertain_and_never_replayed_in_same_call(self):
        self.stage()
        calls = []
        def operation(args, env, *, timeout):
            calls.append(args)
            if Path(args[0]).name == "mem0-mcp-curl":
                return {"principal": self.args["principal"], "allowed_scopes": self.args["read_scopes"],
                        "write_scope": self.args["write_scope"]}
            if "--check" in args:
                return self._receipt(apply=False, trusted=False)
            raise remote_memory.NativeUncertain("timed out after native CAS")
        with patch.object(remote_memory, "_run", side_effect=operation):
            result = remote_memory.enable(self.doc)
        self.assertEqual(result["state"], "NATIVE_UNCERTAIN")
        self.assertEqual(["--check" in args for args in calls[1:]], [True, False])
        with patch.object(remote_memory.subprocess, "run", side_effect=subprocess.TimeoutExpired("native", 1)):
            with self.assertRaises(remote_memory.NativeUncertain):
                remote_memory._run(["native"], {}, timeout=1)

    def test_staged_binding_is_rejected_if_file_or_runtime_changes(self):
        self.stage()
        endpoint = self.root / "mem0/data/fleet-endpoint"
        endpoint.write_text("https://foreign.example/mcp\n")
        with self.assertRaisesRegex(ValueError, "endpoint differs"):
            remote_memory.check(self.doc)
        endpoint.write_text(self.args["endpoint"] + "\n")
        endpoint.chmod(0o644)
        with self.assertRaises(ValueError):
            remote_memory.check(self.doc)
        endpoint.chmod(0o600)
        conductor_path = self.root / "conductors/config.json"
        conductor = config.read_private(conductor_path)
        conductor["instance_id"] = "another"
        config.write_private(conductor_path, json.dumps(conductor), replace=True)
        with self.assertRaisesRegex(ValueError, "identity differs"):
            remote_memory.check(self.doc)

    def test_installed_driver_and_curl_must_match_reviewed_source(self):
        self._file("mem0/bin/mem0-mcp-curl", executable=True)
        with self.assertRaisesRegex(ValueError, "runtime differs"):
            self.stage()

    def test_source_only_release_uses_bundled_helper_and_refuses_drift_or_redirect(self):
        release = self._reviewed_release()
        self.source_patch.stop()
        with patch.object(remote_memory, "SOURCE_ROOT", release):
            staged = self.stage()
            self.assertEqual(staged["state"], "STAGED_NEEDS_GRANT")
            self.assertEqual(remote_memory._paths(self.doc)["helper"], release / "memory/bin/mem0-fleet-configure")
            self.assertNotEqual((self.root / "mem0/bin/mem0-fleet-configure").read_bytes(),
                                (release / "memory/bin/mem0-fleet-configure").read_bytes())
            helper = release / "memory/bin/mem0-fleet-configure"
            original = helper.read_bytes()
            helper.write_bytes(original + b"\n# drift\n")
            with self.assertRaisesRegex(ValueError, "inventory"):
                remote_memory.check(self.doc)
            helper.write_bytes(original)
            helper.chmod(0o700)
            link = release / "installer/cli.py"
            link.unlink()
            link.symlink_to(release / "borg.py")
            with self.assertRaises(ValueError):
                remote_memory.check(self.doc)
            link.unlink()
            shutil.copy2(Path(__file__).resolve().parents[1] / "installer/cli.py", link)
            release.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "owner-controlled"):
                remote_memory.check(self.doc)
        foreign = self.root.parent / "foreign-source"
        foreign.mkdir(mode=0o700)
        with patch.object(remote_memory, "SOURCE_ROOT", foreign), self.assertRaisesRegex(ValueError, "reviewed release"):
            remote_memory.check(self.doc)

    def test_provisioning_environment_discards_route_test_and_policy_overrides(self):
        self.stage()
        paths = remote_memory._identity(self.doc)
        manifest = remote_memory._read_manifest(self.doc, paths)
        with patch.dict(os.environ, {"MEM0_FLEET_TEST_CAPTURE_JSON": '{"principal":"foreign"}',
                                  "MEM0_MCP_CURL_HOOK": "/foreign/driver",
                                  "MEM0_FLEET_ENDPOINT": "https://foreign.example/mcp",
                                  "BORG_POLICY_FILE": "/foreign/policy",
                                  "MEMORY_CODEX_BIN": "/foreign/codex"}):
            env = remote_memory._environment(self.doc, paths, manifest)
        for name in ("MEM0_FLEET_TEST_CAPTURE_JSON", "MEM0_MCP_CURL_HOOK", "MEM0_FLEET_ENDPOINT",
                     "BORG_POLICY_FILE", "MEMORY_CODEX_BIN"):
            self.assertNotIn(name, env)
        self.assertEqual(env["CODEX_HOME"], str(self.profile))
        self.assertEqual(env["MEM0_MACHINE"], self.args["machine"])
        self.assertEqual(env["BORG_LOCAL_MACHINE_ID"], self.args["hub_machine"])

    def test_onboarding_mentions_opt_in_without_reading_token_or_claiming_acceptance(self):
        self.stage()
        result = onboarding.plan(self.doc)
        step = next(row for row in result["steps"] if row["id"] == "remote-memory-client")
        self.assertEqual(step["state"], "staged")
        self.assertEqual(step["observed"]["lifecycle_e2e"], "NOT_VERIFIED")
        self.assertNotIn((self.root / "mem0/data/fleet-token").read_text().strip(), json.dumps(result))
        self.assertEqual([command["argv"][1] for command in step["commands"]], ["memory-client", "memory-client"])

    def test_cli_stage_and_check_expose_only_safe_json(self):
        args = ["memory-client", "stage", "--home", str(self.root), "--machine", self.args["machine"],
                "--hub-machine", self.args["hub_machine"], "--endpoint", self.args["endpoint"],
                "--principal", self.args["principal"], "--write-scope", self.args["write_scope"]]
        for scope in self.args["read_scopes"]:
            args += ["--read-scope", scope]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["schema"], remote_memory.SCHEMA)
        self.assertNotIn((self.root / "mem0/data/fleet-token").read_text().strip(), output.getvalue())

    def test_generated_native_hooks_bind_home_and_exact_old_form_stays_reviewed(self):
        native = runpy.run_path(str(Path(__file__).resolve().parents[1] / "memory/bin/mem0-fleet-configure"))
        with patch.dict(os.environ, {"BORG_LOCAL_MACHINE_ID": self.args["hub_machine"]}):
            for event, mode in (("SessionStart", "prime"), ("UserPromptSubmit", "start"),
                                ("Stop", "end"), ("SessionEnd", "end")):
                with self.subTest(event=event):
                    command = native["command_for"](self.root, self.args["machine"], "codex", mode)
                    old = native["command_for"](self.root, self.args["machine"], "codex", mode,
                                                 include_home=False)
                    self.assertIn("BORG_HOME=", command)
                    self.assertIn("MEM0_FLEET_TOKEN_FILE=", command)
                    self.assertIn("MEM0_FLEET_ENDPOINT_FILE=", command)
                    self.assertIn("MEM0_FLEET_ENDPOINT=", command)
                    self.assertIn("MEM0_FLEET_BASE=", command)
                    self.assertIn("env -u MEM0_FLEET_TEST_CAPTURE_JSON -u MEM0_FLEET_TEST_SEARCH_JSON", command)
                    self.assertNotIn("BORG_LOCAL_MACHINE_ID=", command)
                    self.assertNotIn("BORG_HOME=", old)
                    self.assertTrue(native["reviewed_hook"](command, event, "codex", self.root, self.args["machine"]))
                    self.assertTrue(native["reviewed_hook"](old, event, "codex", self.root, self.args["machine"]))
                    self.assertFalse(native["reviewed_hook"](command + " ; curl foreign", event,
                                                             "codex", self.root, self.args["machine"]))

    def test_actual_bundled_check_receipt_uses_native_event_names(self):
        self.stage()
        native = runpy.run_path(str(Path(__file__).resolve().parents[1] / "memory/bin/mem0-fleet-configure"))
        hooks = {"state": {}}
        rows = []
        names = {"SessionStart": "sessionStart", "UserPromptSubmit": "userPromptSubmit",
                 "Stop": "stop", "SessionEnd": "sessionEnd"}
        with patch.dict(os.environ, {"BORG_LOCAL_MACHINE_ID": self.args["hub_machine"]}):
            for event, mode in (("SessionStart", "prime"), ("UserPromptSubmit", "start"),
                                ("Stop", "end"), ("SessionEnd", "end")):
                command = native["command_for"](self.root, self.args["machine"], "codex", mode)
                child = {"type": "command", "command": command,
                         "timeout": 3 if event in {"SessionStart", "Stop", "SessionEnd"} else 20}
                if event == "UserPromptSubmit":
                    child["additionalContextLimit"] = 900
                group = {"hooks": [child]}
                if event == "SessionStart":
                    group["matcher"] = "startup|resume|clear|compact"
                hooks[event] = [group]
                key = f"key-{event}"
                digest = "sha256:" + format(len(rows) + 1, "064x")
                hooks["state"][key] = {"trusted_hash": digest}
                rows.append({"eventName": names[event], "sourcePath": str(self.profile / "config.toml"),
                             "command": command, "matcher": group.get("matcher", ""),
                             "key": key, "currentHash": digest, "trustStatus": "trusted"})
            class RPC:
                def call(self, method, args):
                    if method == "config/read":
                        return {"layers": [{"name": {"type": "user", "file": str(self.profile_path)},
                                            "config": {"hooks": hooks}, "version": "v1"}]}
                    if method == "hooks/list":
                        return {"data": [{"hooks": rows}]}
                    raise AssertionError(method)
                def close(self):
                    pass
            RPC.profile_path = self.profile / "config.toml"
            native["codex_plan"].__globals__["NativeRPC"] = lambda *a: RPC()
            profile_receipt = native["codex_plan"](self.profile / "config.toml", self.profile,
                                                    self.root, self.args["machine"], False, "codex")
        self.assertEqual({row["event"] for key, row in profile_receipt["trust"].items()
                          if key != "state_upsert_required"}, set(names.values()))
        self.assertFalse(profile_receipt["trust"]["state_upsert_required"])
        wrapped = {"status": "CHECK", "machine": self.args["machine"],
                   "codex": {"selected": 1, "changed": 0, "profiles": [profile_receipt]}}
        paths = remote_memory._identity(self.doc)
        manifest = remote_memory._read_manifest(self.doc, paths)
        with patch.object(remote_memory, "_run", return_value=wrapped):
            _, trusted = remote_memory._plan(self.doc, paths, manifest, apply=False)
        self.assertTrue(trusted, {"changed": profile_receipt["changed_fields"],
                                  "missing": profile_receipt.get("missing_fields"),
                                  "trust_count": len(profile_receipt["trust"])})

    def test_tools_health_never_calls_unstaged_fleet_hooks_capture_free(self):
        mcp = {"command": str(self.root / "mem0/venv/bin/python"),
               "args": [str(self.root / "app/borg.py"), "mcp-stdio", "--home", str(self.root)]}
        client = {"mcp_servers": {"borg": mcp}, "hooks": {}}
        def native_response():
            body = json.dumps({"result": {"config": client}}).encode()
            return SimpleNamespace(open=lambda *a, **k: io.BytesIO(body))
        with patch.object(health, "conductor_headers", return_value={}), \
             patch.object(health.urllib.request, "build_opener", side_effect=lambda *a: native_response()):
            self.assertEqual(health.tools_client_health(self.doc)["state"], "configured_without_capture")
            client["hooks"] = {"Stop": [{"hooks": [{"command": "env MEM0_MACHINE=studio mem0-fleet-hook end"}]}]}
            self.assertEqual(health.tools_client_health(self.doc)["state"], "incomplete")
            self.assertEqual(health.tools_client_health(self.doc, remote={"native_trust_verified": True})["state"],
                             "configured_with_remote_capture")

    def test_tools_client_rerun_preserves_remote_and_inbox_hooks(self):
        self.stage()
        root = self.root
        mcp = {"command": str(root / "mem0/venv/bin/python"),
               "args": [str(root / "app/borg.py"), "mcp-stdio", "--home", str(root)],
               "startup_timeout_sec": 30, "tool_timeout_sec": 120}
        hooks = {"Stop": [{"hooks": [{"command": "inbox-native report"},
                                     {"command": f"env BORG_HOME={root} {root}/mem0/bin/mem0-fleet-hook end"}]}],
                 "state": {"inbox-key": {"trusted_hash": "sha256:inbox"},
                           "memory-key": {"trusted_hash": "sha256:memory"}}}
        current = {"mcp_servers": {"borg": mcp}, "hooks": hooks}
        writes = []
        class RPC:
            def call(self, method, args):
                if method == "config/batchWrite":
                    writes.extend(args["edits"])
                    return {"status": "ok"}
                return {}
            def close(self):
                pass
        native = SimpleNamespace(NativeRPC=lambda *a: RPC(),
            raw_user_layer=lambda *a: {"config": current, "version": "v1"},
            hooks_data=lambda *a: [])
        with patch.object(clients.importlib.machinery.SourceFileLoader, "load_module", return_value=native):
            receipt = clients.configure_clients(self.doc)
        self.assertEqual(receipt["hooks"], 0)
        self.assertEqual([row["keyPath"] for row in writes], ["mcp_servers.borg", "hooks.state"])
        self.assertEqual(writes[1]["value"], hooks["state"])
        self.assertEqual(current["hooks"], hooks)


if __name__ == "__main__":
    unittest.main()
