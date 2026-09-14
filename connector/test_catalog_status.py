import json
import os
import tempfile
import unittest
from pathlib import Path

from catalog_status import load_catalog_status


class CatalogStatusTests(unittest.TestCase):
    def test_unconfigured_is_explicit(self):
        result = load_catalog_status({})
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["schema"], "borg-web-gpt-catalog-status/v1")

    def test_receipt_is_bounded_and_secret_free(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "status.json"
            path.write_text(json.dumps({
                "schema": "borg-web-gpt-catalog-status/v1",
                "status": "refresh_rollout_required",
                "observed_at": "2026-09-13T22:05:00Z",
                "current_catalog": {"tool_count": 60, "schema_sha256": "abc"},
                "summary": {"eligible_accounts": 17,
                             "connected_accounts_on_previous_catalog": 15,
                             "refresh_verified_bindings": 1,
                             "connected_accounts_remaining_to_verify": 14,
                             "pending_setup_accounts": 2,
                             "excluded_accounts": 1},
                "account_rows": [{"account": "owner@example.com", "oauth_token": "withheld"}],
            }))
            os.chmod(path, 0o600)
            result = load_catalog_status({"status_file": str(path)})
            self.assertEqual(result["summary"]["connected_accounts_remaining_to_verify"], 14)
            self.assertEqual(result["current_catalog"]["tool_count"], 60)
            self.assertNotIn("account_rows", result)
            self.assertNotIn("oauth_token", json.dumps(result))

    def test_unreadable_receipt_is_not_treated_as_ready(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "status.json"
            path.write_text("{}")
            os.chmod(path, 0o644)
            result = load_catalog_status({"status_file": str(path)})
            self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
