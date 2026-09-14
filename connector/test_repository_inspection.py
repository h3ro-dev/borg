"""Regression tests for the isolated BORG repository-observation candidate.
All write fixtures are disposable directories under this owned canary directory.
No live repository, credential, memory, agent, or service is changed.
"""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
import repository_inspection as m
HEAD = "1" * 40


def output_for(args):
    if args[0] == "rev-parse":
        return HEAD + "\n"
    if args[0] == "branch":
        return "main\n"
    if args[0] == "log":
        return HEAD + "\x002026-09-12T12:00:00-06:00\tfixture commit\n"
    return " M tracked.py\n?? untracked.txt\n"


def git_ok(command, **kwargs):
    index = command.index("-C")
    return subprocess.CompletedProcess(command, 0, output_for(command[index + 2:]), "")


class RepositoryInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="inspection-test-", dir=HERE)
        self.root = Path(self.tmp.name)
        self.repo = self.make_repo("plimsoll")

    def tearDown(self):
        self.tmp.cleanup()

    def make_repo(self, name):
        repo = self.root / name
        (repo / ".git").mkdir(parents=True)
        return repo

    def test_complete_fields_and_porcelain_spaces(self):
        with patch.object(m.subprocess, "run", side_effect=git_ok):
            row = m._repo_snapshot(self.repo, time.monotonic() + 2)
        self.assertEqual(row["status"], "OBSERVED")
        self.assertTrue(row["inspection_complete"])
        self.assertEqual(row["dirty_paths"], [" M tracked.py", "?? untracked.txt"])
        self.assertEqual(row["head"], HEAD)
        self.assertEqual(row["dirty_count"], 2)

    def test_clean_tree_is_not_unknown(self):
        def clean(command, **kwargs):
            result = git_ok(command, **kwargs)
            if command[-2:] == ["status", "--short"]:
                result.stdout = ""
            return result
        with patch.object(m.subprocess, "run", side_effect=clean):
            row = m._repo_snapshot(self.repo, time.monotonic() + 2)
        self.assertTrue(row["inspection_complete"])
        self.assertEqual(row["dirty_count"], 0)

    def test_expired_budget_never_starts_git(self):
        with patch.object(m.subprocess, "run") as run:
            row = m._repo_snapshot(self.repo, time.monotonic() - 1)
        run.assert_not_called()
        self.assertEqual(row["status"], "NOT_INSPECTED")

    def test_missing_path_is_distinct(self):
        with patch.object(m.subprocess, "run") as run:
            row = m._repo_snapshot(self.root / "absent", time.monotonic() + 2)
        run.assert_not_called()
        self.assertEqual(row["status"], "UNAVAILABLE")
        self.assertFalse(row["inspection_budget_exhausted"])

    def test_path_symlink_is_not_followed(self):
        alias = self.root / "plimsoll-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        with patch.object(m.subprocess, "run", side_effect=git_ok):
            rows = m._repos_matching("plimsoll", (self.root,), 6)
        self.assertEqual([r["path"] for r in rows], [str(self.repo)])

    def test_changed_head_never_claims_consistent_commit(self):
        def changed(command, **kwargs):
            result = git_ok(command, **kwargs)
            if "log" in command:
                result.stdout = "2" * 40 + "\x002026-09-12T12:00:00-06:00\tchanged\n"
            return result
        with patch.object(m.subprocess, "run", side_effect=changed):
            row = m._repo_snapshot(self.repo, time.monotonic() + 2)
        self.assertEqual(row["status"], "PARTIAL")
        self.assertEqual(row["field_status"]["latest_commit"], "INCONSISTENT")
        self.assertIsNone(row["latest_commit"])

    def test_malformed_log_is_explicit(self):
        def malformed(command, **kwargs):
            result = git_ok(command, **kwargs)
            if "log" in command:
                result.stdout = "malformed\n"
            return result
        with patch.object(m.subprocess, "run", side_effect=malformed):
            row = m._repo_snapshot(self.repo, time.monotonic() + 2)
        self.assertEqual(row["field_errors"]["latest_commit"]["reason"], "unexpected_git_output")
        self.assertFalse(row["inspection_complete"])

    def test_timeout_is_not_reported_as_missing_repository(self):
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        with patch.object(m.subprocess, "run", side_effect=timeout):
            row = m._repo_snapshot(self.repo, time.monotonic() + 2)
        self.assertEqual(row["status"], "TIMEOUT")
        self.assertTrue(all(v == "TIMEOUT" for v in row["field_status"].values()))

    def test_one_slow_repository_does_not_starve_other_five(self):
        for n in range(5):
            self.make_repo("plimsoll-fixture-" + str(n))
        def slow(command, **kwargs):
            index = command.index("-C")
            if command[index + 1] == str(self.repo):
                time.sleep(kwargs["timeout"])
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return git_ok(command, **kwargs)
        started = time.monotonic()
        with patch.object(m.subprocess, "run", side_effect=slow):
            rows = m._repos_matching("plimsoll", (self.root,), 6, budget_seconds=0.3)
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(r["inspection_complete"] for r in rows), 5)
        self.assertLess(time.monotonic() - started, 0.8)

    def test_total_budget_and_three_reader_bound(self):
        for n in range(8):
            self.make_repo("plimsoll-fixture-" + str(n))
        lock = threading.Lock()
        state = {"active": 0, "peak": 0}
        def slow(command, **kwargs):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(kwargs["timeout"])
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            finally:
                with lock:
                    state["active"] -= 1
        started = time.monotonic()
        with patch.object(m.subprocess, "run", side_effect=slow):
            rows = m._repos_matching("plimsoll", (self.root,), 9, budget_seconds=0.3)
        self.assertEqual(len(rows), 9)
        self.assertLessEqual(state["peak"], 3)
        self.assertGreaterEqual(state["peak"], 2)
        self.assertLess(time.monotonic() - started, 0.8)

    def test_zero_limit_and_zero_budget_start_no_git(self):
        with patch.object(m.subprocess, "run") as run:
            self.assertEqual(m._repos_matching("plimsoll", (self.root,), 0), [])
            self.assertEqual(m._repos_matching("plimsoll", (self.root,), 6, 0), [])
        run.assert_not_called()

    def test_limit_and_project_name_filter(self):
        self.make_repo("other-project")
        for n in range(4):
            self.make_repo("plimsoll-fixture-" + str(n))
        with patch.object(m.subprocess, "run", side_effect=git_ok):
            rows = m._repos_matching("plimsoll", (self.root,), 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("plimsoll" in Path(r["path"]).name for r in rows))

    def test_git_error_is_bounded_and_payload_free(self):
        with patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], 128, "", "fixture failure")):
            row = m._repo_snapshot(self.repo, time.monotonic() + 2)
        self.assertEqual(row["status"], "UNAVAILABLE")
        self.assertEqual(row["field_errors"]["head"], {"reason": "git_error", "exit_code": 128})
        self.assertNotIn("fixture failure", str(row))

    def test_dirty_output_is_bounded(self):
        def many(command, **kwargs):
            result = git_ok(command, **kwargs)
            if command[-2:] == ["status", "--short"]:
                result.stdout = "".join("?? file" + str(n) + "\n" for n in range(40))
            return result
        with patch.object(m.subprocess, "run", side_effect=many):
            row = m._repo_snapshot(self.repo, time.monotonic() + 2)
        self.assertEqual(len(row["dirty_paths"]), 25)
        self.assertEqual(row["dirty_count"], 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
