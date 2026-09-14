"""Pure unit tests for the bounded BORG connection watchdog."""
import unittest
import json
import io
import urllib.error
from pathlib import Path
import tempfile
from contextlib import nullcontext
from unittest.mock import patch

import connection_watchdog as w


class WatchdogTests(unittest.TestCase):
    @patch.object(w, "configure_instance")
    def test_once_performs_one_cycle_and_does_not_sleep(self, _configure):
        with patch("sys.argv", ["watchdog", "--once"]), patch.dict(w.os.environ, {}, clear=True), \
                patch.object(w, "instance_lock", return_value=nullcontext(True)), \
                patch.object(w, "load_state", return_value={}), patch.object(w, "event"), \
                patch.object(w, "cycle") as cycle, patch.object(w.time, "sleep") as sleep:
            self.assertEqual(w.main(), 0)
            cycle.assert_called_once_with({})
            sleep.assert_not_called()

    @patch.object(w, "configure_instance")
    def test_second_watchdog_does_not_probe_or_spend_restart_budget(self, _configure):
        with tempfile.TemporaryDirectory() as directory, patch.object(w, "STATE_DIR", Path(directory)):
            with w.instance_lock() as first:
                self.assertTrue(first)
                with patch("sys.argv", ["watchdog", "--once"]), patch.dict(w.os.environ, {}, clear=True), \
                        patch.object(w, "load_state") as load, patch.object(w, "cycle") as cycle:
                    self.assertEqual(w.main(), 0)
                    load.assert_not_called()
                    cycle.assert_not_called()
            with w.instance_lock() as next_owner:
                self.assertTrue(next_owner)

    def test_state_publication_does_not_touch_an_old_writers_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / ".state.tmp"
            old.write_text("pending old writer")
            with patch.object(w, "STATE_DIR", root), patch.object(w, "STATE_FILE", root / "state.json"):
                w.save_state({"version": 1, "components": {}})
            self.assertEqual(old.read_text(), "pending old writer")
            self.assertEqual(json.loads((root / "state.json").read_text()), {"version": 1, "components": {}})
            self.assertEqual(list(root.glob(".state-*")), [])

    def test_gateway_requires_its_own_borg_authentication_metadata(self):
        for origin, body, expected in [
            ("https://alice.example", {"error": "BORG owner authentication required"}, (True, "ready")),
            ("https://bob.example", {"error": "BORG owner authentication required"}, (False, "identity_mismatch")),
            ("https://alice.example", {"error": "Other service"}, (False, "identity_mismatch"))]:
            header = 'Bearer resource_metadata="' + origin + '/.well-known/oauth-protected-resource"'
            error = urllib.error.HTTPError(w.URLS["gateway"], 401, "Unauthorized",
                                           {"WWW-Authenticate": header}, io.BytesIO(json.dumps(body).encode()))
            with patch.object(w, "secure_text", return_value=json.dumps({"public_url": "https://alice.example"})), \
                    patch.object(w.urllib.request, "urlopen", side_effect=error):
                self.assertEqual(w.probe_gateway(), expected)

    def test_unrelated_pass_does_not_hide_connector_failure(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, *args):
                return json.dumps({"result": {"structuredContent": {
                    "connector": {"status": "FAIL"}, "mem0": {"status": "PASS"}}}}).encode()
        with patch.object(w, "inbound_authorization", return_value="Bearer synthetic"), \
                patch.object(w.urllib.request, "urlopen", return_value=Response()):
            self.assertEqual(w.probe_adapter(), (False, "invalid_status"))

    def test_independent_watchdog_cannot_restart_owner_services(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            config = Path(directory) / "config.json"
            doc = {"version": 1, "services": {"adapter": "com.example.unrelated-service"},
                   "urls": {"adapter": "http://127.0.0.1:18770/mcp"},
                   "service_manager": "launchd"}
            config.write_text(json.dumps(doc))
            config.chmod(0o600)
            with self.assertRaises(ValueError):
                w.configure_instance(config)
            doc["services"]["adapter"] = "local.borg.independent.connector"
            config.write_text(json.dumps(doc))
            with patch.object(w, "SERVICES", {}), patch.object(w, "URLS", {}), \
                    patch.object(w, "SERVICE_MANAGER", "launchd"):
                w.configure_instance(config)
                self.assertEqual(w.URLS["adapter"], "http://127.0.0.1:18770/mcp")
                with patch.object(w, "event"), patch.object(w, "save_state"), \
                        patch.object(w, "probe_adapter", return_value=(True, "ready")), \
                        patch.object(w, "probe_gateway") as gateway:
                    w.cycle({"components": {}})
                    gateway.assert_not_called()

    def test_two_failures_restart_once(self):
        state = {"version": 1, "components": {}}
        calls = []
        restart = lambda component: calls.append(component) or True
        with patch.object(w, "event"):
            w.record_probe(state, "adapter", False, "unreachable", restart=restart, now=1000)
            self.assertEqual(calls, [])
            w.record_probe(state, "adapter", False, "unreachable", restart=restart, now=1001)
        self.assertEqual(calls, ["adapter"])
        self.assertEqual(state["components"]["adapter"]["failures"], 0)

    def test_authentication_mismatch_never_restarts(self):
        state = {"version": 1, "components": {}}
        calls = []
        restart = lambda component: calls.append(component) or True
        with patch.object(w, "event") as event:
            for stamp in range(10):
                w.record_probe(state, "adapter", False, "authentication_mismatch",
                               restart=restart, now=2000 + stamp)
        self.assertEqual(calls, [])
        self.assertTrue(any(call.args[1] == "manual_attention_required" for call in event.call_args_list))

    def test_restart_budget_stops_flapping(self):
        state = {"version": 1, "components": {"adapter": {
            "failures": 1, "last_restart": 0.0,
            "restarts": [3000.0, 3100.0, 3200.0]}}}
        calls = []
        with patch.object(w, "event") as event:
            w.record_probe(state, "adapter", False, "unreachable",
                           restart=lambda c: calls.append(c) or True, now=3300.0)
        self.assertEqual(calls, [])
        self.assertTrue(any(call.args[1] == "restart_budget_exhausted" for call in event.call_args_list))

    def test_success_clears_failure_counter(self):
        state = {"version": 1, "components": {"tunnel": {
            "failures": 4, "last_restart": 0.0, "restarts": []}}}
        with patch.object(w, "event"):
            w.record_probe(state, "tunnel", True, "ready", now=4000.0)
        self.assertEqual(state["components"]["tunnel"]["failures"], 0)
        self.assertEqual(state["components"]["tunnel"]["last_ok"], 4000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
