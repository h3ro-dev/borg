from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comms.hub.service import HubService, TransportConfigurationError


class PackagingTests(unittest.TestCase):
    def test_provenance_manifest_matches_every_supplied_native_file(self) -> None:
        manifest = json.loads((ROOT / "PROVENANCE.json").read_text())
        self.assertEqual(manifest["schema"], "borg-coordination-provenance/v1")
        self.assertEqual(manifest["upstream_file_count"], 45)
        self.assertEqual(len(manifest["files"]), 45)
        for item in manifest["files"]:
            target = ROOT / item["path"]
            self.assertTrue(target.is_file(), item["path"])
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(digest, item["packaged_sha256"], item["path"])
            self.assertIn(item["status"], {"byte_exact", "portable_patch"})
            if item["status"] == "byte_exact":
                self.assertEqual(item["upstream_sha256"], item["packaged_sha256"])

    def test_runtime_has_no_private_deployment_defaults(self) -> None:
        forbidden = (
            "/Users/",
            ".codex/AGENTS.md",
            "studio0",
            "jamess-macbook-pro-2",
            "3c1f9c4b8d77efc85c363700b196f69db46295ccb434719b27c7388d991473f6",
        )
        for base in (ROOT / "comms", ROOT / "fleet_browser", ROOT / "fleet_desktop", ROOT / "borg_coordination"):
            for path in base.rglob("*.py"):
                if "tests" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                for value in forbidden:
                    self.assertNotIn(value, text, f"{value!r} in {path.relative_to(ROOT)}")

    def test_dependency_pins_are_explicit(self) -> None:
        dependencies = json.loads((ROOT / "DEPENDENCIES.json").read_text())
        self.assertEqual(dependencies["python"]["minimum"], "3.10")
        self.assertEqual(dependencies["python"]["third_party_core"], [])
        self.assertEqual(dependencies["beads"]["project"], "gastownhall/beads")
        self.assertEqual(dependencies["beads"]["version"], "1.2.2")
        self.assertEqual(
            dependencies["beads"]["commit"],
            "6c124203e771433a3550c348771a5b5e27fd3c21",
        )
        self.assertEqual(
            dependencies["optional_python"]["fleet_desktop"],
            ["vncdotool==1.4.2"],
        )
        core = (ROOT / "requirements-core.lock").read_text()
        self.assertNotIn("==", core)
        optional = (ROOT / "requirements-optional-fleet.lock").read_text().splitlines()
        self.assertIn("vncdotool==1.4.2", optional)

    def test_unconfigured_estate_integration_is_explicitly_disabled(self) -> None:
        class AuthorizingStore:
            @staticmethod
            def authorize(actor: str, action: str, scope: str) -> dict[str, object]:
                return {
                    "allowed": actor == "owner"
                    and action == "estate.read"
                    and scope == "/"
                }

        service = HubService(
            store=AuthorizingStore(),
            credentials={"owner": "synthetic-local-test-token"},
        )
        with self.assertRaises(TransportConfigurationError) as raised:
            service.call("owner", "estate.read", {"action": "context"})
        self.assertEqual(raised.exception.code, "integration_disabled")


if __name__ == "__main__":
    unittest.main()
