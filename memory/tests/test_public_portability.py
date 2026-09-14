"""Public memory source must remain owner- and estate-independent."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2]
GUARD = SOURCE / "installer" / "release_guard.py"
SPEC = importlib.util.spec_from_file_location("borg_release_guard_for_memory", GUARD)
assert SPEC and SPEC.loader
release_guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_guard
SPEC.loader.exec_module(release_guard)


class PublicPortabilityTests(unittest.TestCase):
    def test_release_guard_has_no_memory_findings(self):
        _, findings = release_guard.scan(SOURCE)
        memory_findings = [finding for finding in findings if finding.path.startswith("memory/")]
        summary = [f"{finding.rule} {finding.path}:{finding.line}" for finding in memory_findings]
        self.assertEqual([], summary)


if __name__ == "__main__":
    unittest.main()
