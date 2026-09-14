import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from installer.config import initialize, load, environment


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_two_homes_have_distinct_stores_and_credentials_and_repeat_is_stable(self):
        a = initialize(self.root / "a", "alpha")
        b = initialize(self.root / "b", "beta", port_base=19760)
        self.assertNotEqual(a["instance_id"], b["instance_id"])
        self.assertNotEqual(a["memory"]["collection"], b["memory"]["collection"])
        ea, eb = environment(a), environment(b)
        for key in ["BORG_QDRANT_URL", "BORG_QDRANT_COLLECTION", "BORG_HISTORY_DB",
                    "BORG_FALKORDB_GRAPH", "BORG_MEMORY_SCOPE"]:
            self.assertNotEqual(ea[key], eb[key])
        auth = self.root / "a/borg-context/private/authorization"
        before = hashlib.sha256(auth.read_bytes()).hexdigest()
        self.assertEqual(initialize(self.root / "a", "alpha"), a)
        self.assertEqual(hashlib.sha256(auth.read_bytes()).hexdigest(), before)
        other = self.root / "b/borg-context/private/authorization"
        self.assertNotEqual(auth.read_bytes(), other.read_bytes())
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load(self.root / "a"), a)

    def test_existing_unrecognized_directory_is_not_adopted(self):
        home = self.root / "old"
        home.mkdir(mode=0o700)
        (home / "existing.txt").write_text("retain")
        with self.assertRaises(ValueError):
            initialize(home, "owner")
        self.assertEqual((home / "existing.txt").read_text(), "retain")

    def test_symlink_home_and_wrong_owner_are_rejected(self):
        initialize(self.root / "a", "alpha")
        (self.root / "link").symlink_to(self.root / "a", target_is_directory=True)
        with self.assertRaises(ValueError):
            initialize(self.root / "link", "alpha")
        with self.assertRaises(ValueError):
            initialize(self.root / "a", "beta")

    def test_generated_connector_configuration_uses_native_registry_contracts(self):
        initialize(self.root / "a", "alpha")
        root = self.root / "a/borg-context"
        config = json.loads((root / "config.json").read_text())
        self.assertEqual(json.loads((root / "hosts.json").read_text()), [])
        self.assertEqual(json.loads((root / "credentials.json").read_text()), {"version": 1, "services": []})
        self.assertEqual(config["credentials"]["registry"], str(root / "credentials.json"))
        self.assertEqual(config["browser"]["root"], str(root / "browser"))
        self.assertNotIn("desktop", config)


if __name__ == "__main__":
    unittest.main()
