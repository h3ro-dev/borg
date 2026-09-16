"""Blueprint import, selected lifecycle, and owner custody using temporary homes only."""
import contextlib
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from installer import blueprint as bp, cli, clients, config, health, installation, onboarding, services

FIXTURE = Path(__file__).with_name("fixtures") / "blueprint-v1.json"
SOURCE = Path(__file__).resolve().parents[1]


class BlueprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "owner home"
        self.value = bp.read_json(FIXTURE)
        self.registry = bp.catalog()
        self.runtime = patch.object(bp, "runtime_platform", return_value="macos-arm64")
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def write(self, value=None):
        path = self.base / "plan.json"
        path.write_text(json.dumps(self.value if value is None else value))
        return path

    def initialize(self, profile="tools", components=(), integrations=()):
        row = self.value["machines"][0]
        row.update(profile=profile, components=list(components), integrations=list(integrations))
        return config.initialize(self.root, "owner", blueprint_selection={"input": self.value, "machine_id": "node-1"})

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode)
                for p in self.root.rglob("*") if p.is_file()}

    def test_valid_fixture_and_maximum_bounds(self):
        self.assertIs(bp.validate(self.value, self.registry), self.value)
        w = self.value["machines"][0]["workload"]
        w.update(agents=128, browsers=32, builds=32, memory_millions=100, project_gb=100000, context_tokens=32768)
        bp.validate(self.value, self.registry)
        row = self.value["machines"][0]
        self.value["machines"] = [dict(row, id="node-" + str(i)) for i in range(100)]
        bp.validate(self.value, self.registry)
        self.value["machines"].append(dict(row, id="too-many"))
        with self.assertRaises(ValueError):
            bp.validate(self.value, self.registry)

    def test_unknown_missing_and_wrong_shapes_at_every_level(self):
        for target in [(), ("machines", 0), ("machines", 0, "workload")]:
            for mode in ["extra", "missing", "shape"]:
                value = copy.deepcopy(self.value)
                obj = value
                for key in target:
                    obj = obj[key]
                if mode == "extra":
                    obj["shell"] = "arbitrary"
                elif mode == "missing":
                    del obj[next(iter(obj))]
                elif target:
                    parent = value
                    for key in target[:-1]:
                        parent = parent[key]
                    parent[target[-1]] = []
                else:
                    value = []
                with self.subTest(target=target, mode=mode), self.assertRaises(ValueError):
                    bp.validate(value, self.registry)
        for key, values in {"schema": [None, "bad"], "catalog_version": [None, "old"],
                            "goal": [None, "bad", {}], "machines": [None, {}, [], [None]]}.items():
            for value in values:
                changed = dict(self.value, **{key: value})
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    bp.validate(changed, self.registry)

    def test_identifiers_lists_and_labels(self):
        invalid = {"id": ["A", "x" * 33, "x/y", "", None, []],
                   "label": ["", " ", "x" * 61, "x\n", "x\x7f", "x\u202e", "x\u2028", "😀" * 31, None],
                   "components": [["codex", "codex"], ["github"], ["unknown"], [None], {}, None],
                   "integrations": [["github", "github"], ["codex"], ["unknown"], [True]],
                   "profile": ["bad", {}, None], "platform": ["bad", {}, None]}
        for key, values in invalid.items():
            for value in values:
                changed = copy.deepcopy(self.value)
                changed["machines"][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    bp.validate(changed, self.registry)
        self.value["machines"].append(copy.deepcopy(self.value["machines"][0]))
        with self.assertRaises(ValueError):
            bp.validate(self.value, self.registry)

    def test_numeric_types_bounds_and_finite_values(self):
        for name, upper in [("agents", 128), ("browsers", 32), ("builds", 32), ("project_gb", 100000), ("memory_millions", 100)]:
            values = [-1, upper + 1, True, None, "1", float("nan"), float("inf")]
            if name != "memory_millions":
                values.append(0.5)
            for value in values:
                changed = copy.deepcopy(self.value)
                changed["machines"][0]["workload"][name] = value
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    bp.validate(changed, self.registry)
        for key, values in [("context_tokens", [8192.0, True, 0, 65536]), ("training", [0, "false", None])]:
            for value in values:
                changed = copy.deepcopy(self.value)
                changed["machines"][0]["workload"][key] = value
                with self.assertRaises(ValueError):
                    bp.validate(changed, self.registry)

    def test_dependencies_profile_platform_and_training(self):
        row = self.value["machines"][0]
        for components in [["router"], ["claude"], ["training"]]:
            row["components"] = components
            with self.assertRaises(ValueError):
                bp.validate(self.value, self.registry)
        row["components"] = ["training", "adapters"]
        row["workload"]["training"] = True
        bp.validate(self.value, self.registry)
        for key, value in [("profile", "tools"), ("platform", "linux-arm64"), ("platform", "windows")]:
            changed = copy.deepcopy(self.value)
            changed["machines"][0][key] = value
            with self.assertRaises(ValueError):
                bp.validate(changed, self.registry)
        row.update(components=["codex"], platform="windows")
        row["workload"]["training"] = False
        bp.validate(self.value, self.registry)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            bp.check_runtime(row)
        row["platform"] = "linux-x64"
        with self.assertRaisesRegex(ValueError, "does not match"):
            bp.check_runtime(row)

    def test_duplicate_json_keys_nonfinite_and_size(self):
        path = self.base / "bad.json"
        for text in ['{"schema":1,"schema":2}', '{"x":{"a":1,"a":2}}', '{"x":NaN}', '[', 'x' * (1024 * 1024 + 1)]:
            path.write_text(text)
            with self.assertRaises(ValueError):
                bp.load(path)

    def test_inspect_never_loads_home_and_validates_unselected_machines(self):
        path = self.write()
        with patch.object(config, "load", side_effect=AssertionError("loaded home")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["blueprint", "inspect", str(path), "--machine", "node-1"]), 0)
        report = json.loads(out.getvalue())
        self.assertIn("conductor", report["machines"][0]["services"])
        self.assertFalse(self.root.exists())
        self.value["machines"].append(dict(self.value["machines"][0], id="node-2", components=["invalid"]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["blueprint", "inspect", str(self.write()), "--machine", "node-1"]), 1)
        with self.assertRaises(ValueError):
            bp.machine(self.value, "unknown")

    def test_invalid_cli_and_shell_leave_no_owner_state(self):
        self.value["machines"][0]["workload"]["agents"] = 129
        path = self.write()
        args = ["--home", str(self.root), "--owner", "owner", "--blueprint", str(path), "--machine", "node-1", "--no-start"]
        with patch.object(config, "initialize", side_effect=AssertionError("created state")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["install", *args]), 1)
        result = subprocess.run([str(SOURCE / "install.sh"), *args], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agents", result.stderr)
        self.assertFalse(self.root.exists())
        self.value["machines"][0]["workload"]["agents"] = 2
        self.value["machines"][0]["platform"] = "windows"
        self.write()
        result = subprocess.run([str(SOURCE / "install.sh"), *args], capture_output=True, text=True)
        self.assertIn("unsupported", result.stderr)
        self.assertFalse(self.root.exists())

    def test_rerun_identity_and_private_blueprint_preservation(self):
        doc = self.initialize(components=["codex"])
        self.assertEqual((self.root / "blueprint.json").stat().st_mode & 0o777, 0o600)
        before = self.snapshot()
        self.assertEqual(config.initialize(self.root, "owner", blueprint_selection=doc["blueprint"]), doc)
        self.assertEqual(before, self.snapshot())
        for selection in [None, {"input": dict(self.value, goal="research"), "machine_id": "node-1"},
                          {"input": self.value, "machine_id": "missing"}]:
            with self.assertRaises(ValueError):
                config.initialize(self.root, "owner", blueprint_selection=selection)
            self.assertEqual(before, self.snapshot())
        with self.assertRaises(ValueError):
            config.initialize(self.root, "different", blueprint_selection=doc["blueprint"])
        self.assertEqual(before, self.snapshot())

    def test_existing_default_full_cannot_adopt_blueprint(self):
        config.initialize(self.root, "owner")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "new home"):
            self.initialize()
        self.assertEqual(before, self.snapshot())

    def test_stored_blueprint_redirect_or_change_refuses_rerun(self):
        doc = self.initialize()
        path = self.root / "blueprint.json"
        path.write_text('{}')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            bp.check_existing(self.root, doc["blueprint"])
        self.assertEqual(before, self.snapshot())
        path.unlink()
        path.symlink_to(self.root / "config.json")
        with self.assertRaises(ValueError):
            bp.check_existing(self.root, doc["blueprint"])

    def test_service_selection_and_missing_selected_contract(self):
        doc = self.initialize(components=["codex", "inbox"])
        with patch.object(services, "service_environment", return_value={"BORG_OLLAMA_URL": "http://127.0.0.1:1"}), \
             patch.object(services, "executable", side_effect=lambda root, component, name: root / "runtime" / component / name):
            rows = services.specifications(doc)
        self.assertEqual(set(rows), {"connector", "conductor"})  # Contracts have not been installed yet.
        self.assertEqual(set(bp.service_names(doc)), {"connector", "watchdog", "conductor", "inbox"})
        with patch.object(services, "specifications", return_value=rows), self.assertRaises(ValueError):
            services.start(doc, ["memory"])
        self.assertEqual(set(bp.service_names({})), set(bp.FULL_SERVICES) | {"conductor", "inbox"})

    def test_tools_install_prepares_only_selected_services_and_never_pulls_models(self):
        doc = self.initialize(components=["beads"])
        (self.root / "bin").mkdir()
        bootstrap = self.root / "app/coordination/bin/borg-coordination"
        bootstrap.parent.mkdir(parents=True)
        bootstrap.write_text("fixture")
        with patch.object(installation.sys, "prefix", str(self.root / "mem0/venv")), \
             patch.object(installation, "install_sources", return_value="fixture"), \
             patch.object(installation.dependencies, "executable", side_effect=lambda root, component, name: root / "runtime" / component / name), \
             patch.object(services, "service_environment", return_value={}), \
             patch.object(installation.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(clients, "configure_clients"), patch.object(services, "start") as start, \
             patch.object(installation, "wait_for_port") as wait, \
             patch.object(installation.dependencies, "pull_models") as pull, \
             patch.object(health, "wait_for_local_ready", return_value={"local_services_ready": True}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installation.complete_install(doc), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([c[2] for c in commands], ["bootstrap", "init-beads"])
        self.assertTrue((self.root / "bin/bd").is_file())
        pull.assert_not_called()
        start.assert_called_once_with(doc)
        wait.assert_called_once_with(doc["ports"]["connector"])

    def test_default_full_install_keeps_models_brain_and_all_services(self):
        doc = config.initialize(self.root, "owner")
        (self.root / "bin").mkdir()
        bootstrap = self.root / "app/coordination/bin/borg-coordination"
        bootstrap.parent.mkdir(parents=True)
        bootstrap.write_text("fixture")
        with patch.object(installation.sys, "prefix", str(self.root / "mem0/venv")), \
             patch.object(installation, "install_sources", return_value="fixture"), \
             patch.object(installation.dependencies, "executable", side_effect=lambda root, component, name: root / "runtime" / component / name), \
             patch.object(services, "service_environment", return_value={}), \
             patch.object(installation.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(clients, "configure_clients") as configure, patch.object(services, "start") as start, \
             patch.object(installation, "wait_for_port"), \
             patch.object(installation.dependencies, "pull_models", return_value=doc) as pull, \
             patch.object(health, "wait_for_local_ready", return_value={"local_services_ready": True}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installation.complete_install(doc), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([c[2] for c in commands], ["bootstrap", "bootstrap", "init-beads", "initialize"])
        self.assertFalse(any("login" in c for c in commands))
        pull.assert_called_once_with(doc)
        configure.assert_called_once_with(doc)
        self.assertEqual(start.call_args_list[0].args, (doc, ["qdrant", "graph", "ollama"]))
        self.assertEqual(start.call_args_list[1].args, (doc,))

    def test_tools_hooks_disabled_and_provider_login_unselected(self):
        doc = self.initialize()
        with patch.object(os, "execve", side_effect=AssertionError("executed")), self.assertRaises(ValueError):
            clients.hook(doc, "start")
        with patch.object(installation.subprocess, "run", side_effect=AssertionError("login")), self.assertRaises(ValueError):
            installation.login(doc, "codex")
        self.assertEqual(clients.configure_clients(doc), {"state": "not_selected"})

    def test_tools_client_configuration_has_only_mcp_no_memory_hooks(self):
        doc = self.initialize(components=["codex"])
        (self.root / "conductors/primary/profile").mkdir()
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
            raw_user_layer=lambda *a: {"config": {"mcp_servers": {"borg": writes[0]["value"]}} if writes else {}},
            hooks_data=lambda *a: [])
        with patch.object(clients.importlib.machinery.SourceFileLoader, "load_module", return_value=native), \
             patch.object(services, "service_environment", return_value={"PATH": "/usr/bin:/bin"}):
            receipt = clients.configure_clients(doc)
        self.assertEqual(receipt["hooks"], 0)
        self.assertFalse(receipt["account_credentials_imported"])
        self.assertFalse(any(e["keyPath"].startswith("hooks.") and e["keyPath"] != "hooks.state" for e in writes))
        self.assertEqual(writes[0]["value"]["args"], [str(self.root / "app/borg.py"), "mcp-stdio", "--home", str(self.root)])

    def test_selected_onboarding_is_read_only_truthful_and_rerunnable(self):
        doc = self.initialize(components=["grok", "claude", "launch-bus", "fleet"], integrations=["github"])
        before = self.snapshot()
        with patch.object(subprocess, "run", side_effect=AssertionError("executed")):
            result = onboarding.plan(doc)
        steps = {s["id"]: s for s in result["steps"]}
        self.assertNotIn("codex-login", steps)
        self.assertNotIn("adapters", steps)
        self.assertIn("own-machines", steps)
        self.assertIn("selected-github", steps)
        self.assertEqual(steps["grok"]["state"], "manual_setup_required")
        self.assertTrue(steps["grok"]["required"])
        self.assertFalse(any(s["ready"] is True for s in steps.values()))
        self.assertIn("--blueprint", steps["installation"]["commands"][0]["argv"])
        self.assertEqual(before, self.snapshot())

    def test_platform_source_is_required_and_copied(self):
        self.assertIn("platform", installation.DIRECTORIES)
        doc = config.initialize(self.root, "owner")
        paths = [SOURCE / "borg.py", SOURCE / "platform/catalog.json"]
        with patch.object(installation, "source_files", return_value=paths):
            first = installation.install_sources(doc)
            self.assertEqual((self.root / "app/platform/catalog.json").read_bytes(), bp.CATALOG.read_bytes())
            self.assertEqual(first, installation.install_sources(doc))
            (self.root / "app/platform/catalog.json").write_text('{}')
            with self.assertRaises(RuntimeError):
                installation.install_sources(doc)

    def health(self, doc, *, hub_ready=True):
        config.write_private(self.root / "borg-context/watchdog/state.json", json.dumps({
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "components": {"adapter": {"failures": 0, "last_reason": "ready"}}}),
            replace=(self.root / "borg-context/watchdog/state.json").exists())
        fake = SimpleNamespace(private_text=lambda p: "Bearer fixture")
        def native(command, **kwargs):
            if "status" in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "ready" if hub_ready else "failed"}))
            raise AssertionError(command)
        with patch.dict(sys.modules, {"borg_context_server": fake}), \
             patch.object(health.socket, "create_connection", return_value=contextlib.nullcontext()), \
             patch.object(health, "mcp_call", return_value={"connector": {"status": "PASS"}}), \
             patch.object(health, "get_json", side_effect=AssertionError("unselected service queried")), \
             patch.object(services, "service_environment", return_value={}), \
             patch.object(services, "running", return_value=True), \
             patch.object(health.subprocess, "run", side_effect=native):
            return health.status(doc)

    def test_tools_health_ignores_memory_but_requires_selected_inbox(self):
        doc = self.initialize(components=["inbox"])
        result = self.health(doc)
        self.assertTrue(result["local_services_ready"])
        self.assertTrue(result["ready"])
        self.assertNotIn("memory", result["components"])
        self.assertNotIn("capture_hooks", result["components"])
        self.assertFalse(self.health(doc, hub_ready=False)["local_services_ready"])

    def test_external_selections_cannot_be_reported_ready(self):
        doc = self.initialize(components=["grok"], integrations=["github"])
        result = self.health(doc)
        self.assertTrue(result["local_services_ready"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "selected_setup_required")
        self.assertEqual([r["id"] for r in result["selected_setup"]], ["grok", "github"])


if __name__ == "__main__":
    unittest.main()
