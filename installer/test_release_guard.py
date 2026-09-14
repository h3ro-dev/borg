from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from installer import release_guard
from installer.adapter_contract import REQUIRED_ADAPTERS, verify_release


class ReleaseGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        (self.root / "adapters").mkdir()
        self.adapters = []
        for name in sorted(REQUIRED_ADAPTERS):
            relative = f"adapters/{name}/adapters.safetensors"
            data = b"\xff\x00public-test-weight:" + name.encode()
            target = self.root / relative
            target.parent.mkdir()
            target.write_bytes(data)
            self.adapters.append({"name": name, "weights_present": True, "active": False,
                "weights": {"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}})
        self._write_manifest(self.adapters)

    def _write_manifest(self, adapters: list[dict[str, object]]) -> None:
        (self.root / "adapters" / "MANIFEST.json").write_text(
            json.dumps({"schema": "borg-adapters/v2", "distribution": {"weights_included": True}, "adapters": adapters}),
            encoding="utf-8",
        )

    def _run(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = release_guard.main(["--root", str(self.root), *arguments])
        return result, output.getvalue()

    def test_clean_public_source_passes_and_inventory_has_only_hashes(self) -> None:
        (self.root / "README.md").write_text(
            "Docs: https://github.com/h3ro-dev/borg\ncontact: owner@example.com\n",
            encoding="utf-8",
        )
        files, findings = release_guard.scan(self.root)
        self.assertEqual(findings, [])
        self.assertEqual(len(files), 5)
        self.assertIn("README.md", [item["path"] for item in files])
        self.assertEqual(sum(str(item["path"]).endswith(".safetensors") for item in files), 3)
        self.assertTrue(all(len(str(item["sha256"])) == 64 for item in files))

    def test_private_path_and_secret_are_reported_without_disclosing_values(self) -> None:
        secret = "sk-" + "prodlive_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        private_home = "/" + "Users/prior-owner/private"
        (self.root / "runtime.py").write_text(
            f'root = "{private_home}"\napi_key = "{secret}"\n',
            encoding="utf-8",
        )
        status, output = self._run("--json")
        self.assertEqual(status, 1)
        self.assertIn('"private-user-path"', output)
        self.assertIn('"openai-token"', output)
        self.assertNotIn("prior-owner", output)
        self.assertNotIn(secret, output)

    def test_public_examples_and_intentional_test_placeholders_are_allowed(self) -> None:
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_fixture.py").write_text(
            'endpoint = "https://owner.example/mcp"\napi_key = "sk-test_XXXXXXXXXXXXXXXXXXXXXXXX"\n',
            encoding="utf-8",
        )
        _, findings = release_guard.scan(self.root)
        self.assertEqual(findings, [])

    def test_real_looking_secret_in_test_is_still_rejected(self) -> None:
        tests = self.root / "tests"
        tests.mkdir()
        secret = "sk-" + "prodlive_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        (tests / "test_fixture.py").write_text(
            f'api_key = "{secret}"\n',
            encoding="utf-8",
        )
        _, findings = release_guard.scan(self.root)
        self.assertIn("openai-token", {finding.rule for finding in findings})

    def test_exact_line_allowlist_can_preserve_public_author_credit(self) -> None:
        source = self.root / "AUTHORS.md"
        line = "Historical author: maintainer@" + "public-project.dev"
        source.write_text(line + "\n", encoding="utf-8")
        _, findings = release_guard.scan(self.root)
        email = next(finding for finding in findings if finding.rule == "unapproved-email")
        allowlist = self.root / "PUBLIC-ALLOWLIST.json"
        allowlist.write_text(
            json.dumps(
                {
                    "schema": release_guard.ALLOWLIST_SCHEMA,
                    "entries": [
                        {
                            "rule": email.rule,
                            "path": email.path,
                            "line": email.line,
                            "line_sha256": email.line_sha256,
                            "reason": "Public project maintainer attribution",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        _, allowed_findings = release_guard.scan(self.root, allowlist_path=allowlist)
        self.assertFalse(any(finding.rule == "unapproved-email" for finding in allowed_findings))

    def test_non_waivable_private_path_cannot_be_allowlisted(self) -> None:
        source = self.root / "runtime.py"
        line = 'root = "' + "/" + 'Users/prior-owner/private"'
        source.write_text(line + "\n", encoding="utf-8")
        allowlist = self.root / "PUBLIC-ALLOWLIST.json"
        allowlist.write_text(
            json.dumps(
                {
                    "schema": release_guard.ALLOWLIST_SCHEMA,
                    "entries": [
                        {
                            "rule": "private-user-path",
                            "path": "runtime.py",
                            "line": 1,
                            "line_sha256": hashlib.sha256(line.encode()).hexdigest(),
                            "reason": "must not be accepted",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(release_guard.GuardConfigurationError):
            release_guard.scan(self.root, allowlist_path=allowlist)

    def test_manifest_pinned_adapter_weight_is_the_only_accepted_binary(self) -> None:
        _, findings = release_guard.scan(self.root)
        self.assertEqual(findings, [])
        (self.root / "mystery.bin").write_bytes(b"\xff\x00unknown")
        _, findings = release_guard.scan(self.root)
        self.assertIn("unexpected-binary", {finding.rule for finding in findings})

    def test_missing_manifest_declared_weight_fails(self) -> None:
        verify_release(self.root)
        (self.root / self.adapters[0]["weights"]["path"]).unlink()
        _, findings = release_guard.scan(self.root)
        self.assertIn("declared-release-file-missing", {finding.rule for finding in findings})
        with self.assertRaises(ValueError):
            verify_release(self.root)

    def test_removed_or_disabled_adapter_cannot_make_the_release_pass(self) -> None:
        disabled = json.loads(json.dumps(self.adapters))
        disabled[0]["weights_present"] = False
        for rows in [[], self.adapters[1:], disabled]:
            with self.subTest(rows=len(rows)):
                self._write_manifest(rows)
                with self.assertRaises(release_guard.GuardConfigurationError):
                    release_guard.scan(self.root)
                with self.assertRaises(ValueError):
                    verify_release(self.root)

    def test_forbidden_report_and_symlink_fail(self) -> None:
        (self.root / "REPORT.md").write_text("private receipt", encoding="utf-8")
        (self.root / "link").symlink_to(self.root / "adapters" / "MANIFEST.json")
        _, findings = release_guard.scan(self.root)
        rules = {finding.rule for finding in findings}
        self.assertIn("forbidden-release-artifact", rules)
        self.assertIn("symlink", rules)

    def test_written_inventory_detects_changed_and_unexpected_files(self) -> None:
        (self.root / "README.md").write_text("public\n", encoding="utf-8")
        inventory = self.root / "PUBLIC-INVENTORY.json"
        status, _ = self._run("--write-inventory", str(inventory))
        self.assertEqual(status, 0)
        status, _ = self._run("--inventory", str(inventory))
        self.assertEqual(status, 0)
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        (self.root / "new.py").write_text("new\n", encoding="utf-8")
        status, output = self._run("--inventory", str(inventory), "--json")
        self.assertEqual(status, 1)
        self.assertIn('"inventory-file-changed"', output)
        self.assertIn('"inventory-file-unexpected"', output)


if __name__ == "__main__":
    unittest.main()
