from pathlib import Path
import tempfile
import unittest

from installer import health


class ConductorAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "borg"
        self.token = self.root / "conductors/primary/profile/.conductor/http-token"

    def write_token(self, mode=0o600):
        self.token.parent.mkdir(parents=True, mode=0o700)
        self.token.write_text("a" * 64 + "\n")
        self.token.chmod(mode)

    def subject(self):
        self.assertTrue(hasattr(health, "conductor_headers"), "installer health must expose conductor_headers")
        return health.conductor_headers

    def test_private_conductor_token_becomes_a_bearer_header(self):
        self.write_token()
        self.assertEqual(self.subject()(self.root), {
            "Authorization": "Bearer " + "a" * 64,
        })

    def test_missing_token_preserves_report_mode_compatibility(self):
        self.assertEqual(self.subject()(self.root), {})

    def test_unsafe_or_malformed_token_is_never_sent(self):
        self.write_token(mode=0o644)
        with self.assertRaises(ValueError):
            self.subject()(self.root)
        self.token.chmod(0o600)
        self.token.write_text("not-a-token\n")
        with self.assertRaises(ValueError):
            self.subject()(self.root)


if __name__ == "__main__":
    unittest.main()
