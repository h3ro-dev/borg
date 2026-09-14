"""Contract changes must be discoverable without adding or renaming a tool."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from capabilities import build_manifest, schema_fingerprint


class CapabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.tools = [{"name": "computer_read_file", "inputSchema": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"]}, "annotations": {"readOnlyHint": True}}]

    def test_input_output_and_annotations_change_fingerprint(self):
        changes = [
            ("inputSchema", {"type": "object", "properties": {"file": {"type": "string"}}}),
            ("outputSchema", {"type": "object", "required": ["content"]}),
            ("annotations", {"readOnlyHint": False}),
        ]
        for key, value in changes:
            with self.subTest(key=key):
                changed = deepcopy(self.tools)
                changed[0][key] = value
                self.assertNotEqual(schema_fingerprint(self.tools), schema_fingerprint(changed))

    def test_wire_order_does_not_change_fingerprint(self):
        other = {"name": "borg_status", "inputSchema": {"type": "object"}}
        reversed_fields = dict(reversed(list(self.tools[0].items())))
        self.assertEqual(schema_fingerprint(self.tools + [other]),
                         schema_fingerprint([other, reversed_fields]))

    def test_configuration_does_not_claim_health(self):
        settings = SimpleNamespace(computer={"backend": "native"}, browser={"backend": "native"})
        manifest = build_manifest(settings, self.tools)
        self.assertEqual(manifest["domains"]["browser"]["status"], "configured")
        self.assertNotIn("ready", {row["status"] for row in manifest["domains"].values()})


if __name__ == "__main__":
    unittest.main()
