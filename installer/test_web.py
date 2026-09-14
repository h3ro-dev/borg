import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "connector"))
from installer.config import initialize, load, write_private
from installer.web import configure, configure_watchdog


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "owner"
        self.doc = initialize(self.root, "alpha")
        self.credentials = self.root / "cloudflare/credentials.json"
        write_private(self.credentials, json.dumps({
            "AccountTag": "synthetic-account", "TunnelID": "11111111-1111-4111-8111-111111111111",
            "TunnelSecret": base64.b64encode(b"synthetic-only-secret-for-tests!!").decode()}))
        self.options = {"public_url": "https://borg.example.test", "issuer": "https://owner.cloudflareaccess.com",
                        "audience": "a" * 64, "owner_email": "owner@example.test",
                        "tunnel_credentials": self.credentials}

    def test_web_configuration_preserves_credentials_and_pins_only_owned_services(self):
        before = self.credentials.read_bytes()
        result = configure(self.doc, **self.options)
        self.assertEqual(result["state"], "configured_not_started")
        self.assertEqual(self.credentials.read_bytes(), before)
        gateway = json.loads((self.root / "borg-context/cloudflare/config.json").read_text())
        self.assertEqual(gateway["upstream_url"], "http://127.0.0.1:18766/mcp")
        tunnel = json.loads((self.root / "cloudflare/cloudflared.json").read_text())
        self.assertEqual(tunnel["ingress"], [{"hostname": "borg.example.test", "path": "^/mcp$",
                                            "service": "http://127.0.0.1:18769"}, {"service": "http_status:404"}])
        self.assertNotIn("TunnelSecret", json.dumps(tunnel))
        watchdog = json.loads((self.root / "borg-context/watchdog/config.json").read_text())
        self.assertEqual(set(watchdog["services"]), {"adapter", "gateway", "tunnel"})
        self.assertTrue(all(self.doc["instance_id"] in x for x in watchdog["services"].values()))
        self.assertEqual(configure(load(self.root), **self.options), result)

    def test_other_owner_credentials_and_invalid_gateway_are_rejected_before_activation(self):
        foreign = Path(self.temp.name) / "foreign.json"
        write_private(foreign, self.credentials.read_text())
        for changes in [{"tunnel_credentials": foreign}, {"public_url": "https://borg.example.test/path"},
                        {"issuer": "https://unrelated.example.test"}, {"owner_email": "*"}, {"audience": "wrong"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                configure(self.doc, **{**self.options, **changes})
            self.assertFalse(load(self.root)["external_access"]["enabled"])
            self.assertFalse((self.root / "cloudflare/cloudflared.json").exists())

    def test_local_watchdog_has_no_gateway_or_original_fleet_labels(self):
        configure_watchdog(self.doc)
        path = self.root / "borg-context/watchdog/config.json"
        body = json.loads(path.read_text())
        self.assertEqual(body["services"], {"adapter": "local.borg." + self.doc["instance_id"] + ".connector"})
        self.assertEqual(body["urls"], {"adapter": "http://127.0.0.1:18766/mcp"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
