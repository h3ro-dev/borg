"""Synthetic release-boundary tests. No credentials, network or private data."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("capsule", ROOT / "tools/build_capsule.py")
capsule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capsule)

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home().resolve())
        self.base = Path(self.tmp.name)
        self.root = self.base / "reference"
        self.root.mkdir()
        (self.root / "README.md").write_text("Synthetic architecture.\n")
        self.manifest(["README.md", "release.json"])

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, names):
        (self.root / "release.json").write_text(json.dumps({"version":1,"kind":"architecture-reference-only","files":names}))

    def test_source_package_passes(self):
        self.assertGreater(len(capsule.collect(ROOT)), 10)

    def test_unknown_files_do_not_enter_archive(self):
        (self.root / "unlisted.txt").write_text("PRIVATE SYNTHETIC CONTENT")
        out = self.base / "out.zip"
        capsule.build(self.root, out)
        with zipfile.ZipFile(out) as z:
            self.assertNotIn("borg-architecture/unlisted.txt", z.namelist())
            self.assertEqual(len(z.namelist()), 3)

    def test_reproducible_archive_and_checksums(self):
        one, two = self.base / "one.zip", self.base / "two.zip"
        capsule.build(self.root, one); capsule.build(self.root, two)
        self.assertEqual(one.read_bytes(), two.read_bytes())
        with zipfile.ZipFile(one) as z:
            checks = json.loads(z.read("borg-architecture/CHECKSUMS.json"))
            for name, digest in checks["files"].items():
                self.assertEqual(hashlib.sha256(z.read("borg-architecture/" + name)).hexdigest(), digest)

    def test_existing_output_preserved(self):
        out = self.base / "out.zip";out.write_bytes(b"existing")
        with self.assertRaises(capsule.ReleaseError):capsule.build(self.root, out)
        self.assertEqual(out.read_bytes(), b"existing")

    def test_traversal_and_absolute_paths_denied(self):
        for name in ("../other.md", "/other.md", "a/../other.md", "a//other.md", "a\\other.md", "C:/other.md"):
            with self.subTest(name=name), self.assertRaises(capsule.ReleaseError):capsule.safe_read(self.root, name)

    def test_symlink_file_and_directory_denied(self):
        outside = self.base / "outside.md";outside.write_text("synthetic")
        (self.root / "linked.md").symlink_to(outside)
        with self.assertRaises(capsule.ReleaseError):capsule.safe_read(self.root, "linked.md")
        (self.root / "link").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(capsule.ReleaseError):capsule.safe_read(self.root, "link/outside.md")

    def test_symlink_root_denied(self):
        link = self.base / "root-link";link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(capsule.ReleaseError):capsule.collect(link)

    def test_fifo_denied_without_waiting_for_writer(self):
        os.mkfifo(self.root / "pipe.md")
        with self.assertRaises(capsule.ReleaseError):capsule.safe_read(self.root, "pipe.md")

    def test_hardlink_denied(self):
        os.link(self.root / "README.md", self.root / "hard.md")
        with self.assertRaises(capsule.ReleaseError):capsule.safe_read(self.root, "hard.md")

    def test_model_database_and_secret_paths_denied(self):
        for name in ("model.safetensors", "history.db", ".env", "logs/latest.txt", "tokens/example.txt", "data/facts.json"):
            with self.subTest(name=name), self.assertRaises(capsule.ReleaseError):capsule.safe_read(self.root, name)

    def test_binary_and_large_payloads_denied(self):
        for raw in (b"\x00", b"\xff"):
            with self.assertRaises(capsule.ReleaseError):capsule.inspect_text("sample.md", raw, ())
        (self.root / "big.md").write_bytes(b"a" * (capsule.MAX_FILE_BYTES + 1))
        with self.assertRaises(capsule.ReleaseError):capsule.safe_read(self.root, "big.md")

    def test_credentials_denied_without_echo(self):
        value = "sk-" + "a" * 40
        with self.assertRaises(capsule.ReleaseError) as caught:capsule.inspect_text("sample.md", value.encode(), ())
        self.assertNotIn(value, str(caught.exception))

    def test_personal_path_and_email_denied(self):
        path = "/" + "Users" + "/" + "operator" + "/private"
        email = "person" + "@" + "not-an-example.edu"
        for text in (path, email):
            with self.assertRaises(capsule.ReleaseError):capsule.inspect_text("sample.md", text.encode(), ())
        capsule.inspect_text("sample.md", b"person@example.invalid", ())

    def test_private_terms_denied_without_echo(self):
        value = "SyntheticPrivateIdentifier"
        with self.assertRaises(capsule.ReleaseError) as caught:capsule.inspect_text("sample.md", value.encode(), (value,))
        self.assertNotIn(value, str(caught.exception))

    def test_invalid_manifest_and_duplicate_names_denied(self):
        for doc in ([], {}, {"version":1,"kind":"architecture-reference-only","files":["release.json","release.json"]}):
            (self.root / "release.json").write_text(json.dumps(doc))
            with self.assertRaises(capsule.ReleaseError):capsule.collect(self.root)

    def test_synthetic_configuration_has_safe_defaults(self):
        data = json.loads((ROOT / "examples/borg.example.json").read_text())
        self.assertTrue(data["example_only"])
        self.assertEqual(data["context"]["default_profile"], "read-context")
        self.assertFalse(data["capture"]["training_allowed"])
        self.assertFalse(data["execution"]["enabled"])
        self.assertFalse(data["privacy"]["payload_logging"])

    def test_model_manifest_cannot_imply_approval(self):
        data = json.loads((ROOT / "examples/model-manifest.example.json").read_text())
        self.assertFalse(data["production_approved"])
        self.assertIsNone(data["artifact_sha256"])
        self.assertTrue(all(value in ("unknown", "not_run") for value in data["gates"].values()))

    def test_memory_fixture_is_candidate_and_unprojected(self):
        data = json.loads((ROOT / "examples/memory-event.example.json").read_text())
        self.assertEqual(data["authority"], "candidate")
        self.assertFalse(data["training_allowed"])
        self.assertEqual(set(data["projection_status"].values()), {"pending"})

if __name__ == "__main__":
    unittest.main()
