#!/usr/bin/env python3
"""Focused tests for the graph-only zero-episode supervisor behavior."""

import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "lane-supervisor"
LOADER = importlib.machinery.SourceFileLoader(
    "lane_supervisor_incremental", str(MODULE_PATH)
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


class ReceiptCollector:
    def __init__(self):
        self.lines = []

    def write(self, target, kind, state, action, reason, **detail):
        record = {
            "target": target,
            "kind": kind,
            "state": state,
            "action": action,
            "reason": reason,
            "detail": detail,
        }
        self.lines.append(record)
        return record


def lane(name, incremental=False):
    value = {
        "name": name,
        "script": "backfill.py" if name == "graph" else f"{name}.py",
        "lock": f"{name}.lock",
        "log": f"{name}.log",
        "stall_s": 100,
        "start_re": r"START\b.*\bepisodes=(\d+)",
        "incremental": incremental,
    }
    if name == "graph":
        value["progress_stall_s"] = 60
    return value


class LaneSupervisorIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(supervisor, "read_lock", return_value=None),
            mock.patch.object(supervisor, "log_age_s", return_value=None),
            mock.patch.object(supervisor, "last_queued", return_value=0),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_graph_zero_queue_is_idle(self):
        receipts = ReceiptCollector()
        restart = mock.patch.object(
            supervisor,
            "restart",
            return_value={"state": "idle", "action": "restart"},
        )
        with restart as restart_fn:
            result = supervisor.check_lane(
                receipts, lane("graph", incremental=True), dry=False
            )

        self.assertEqual(result["state"], "idle")
        self.assertEqual(result["action"], "restart")
        restart_fn.assert_called_once()

    def test_optional_non_graph_lane_is_unavailable_in_portable_mode(self):
        receipts = ReceiptCollector()
        restart = mock.patch.object(supervisor, "restart")
        with restart as restart_fn:
            supervisor.check_lane(receipts, lane("ox"), dry=False)

        self.assertEqual(receipts.lines[-1]["state"], "unavailable")
        self.assertEqual(receipts.lines[-1]["action"], "skip")
        restart_fn.assert_not_called()

    def test_graph_clean_exit_schedules_next_pass_as_poll(self):
        # A finished pass (RUN-END after RUN-START, lock gone, episodes > 0) is
        # not a crash: the next pass is a paced poll outside the restart budget.
        receipts = ReceiptCollector()
        with mock.patch.object(supervisor, "last_queued", return_value=23), \
             mock.patch.object(supervisor, "clean_exit", return_value=True), \
             mock.patch.object(
                 supervisor, "restart", return_value={"state": "idle", "action": "poll"}
             ) as restart_fn:
            result = supervisor.check_lane(
                receipts, lane("graph", incremental=True), dry=False
            )
        self.assertEqual(result["action"], "poll")
        restart_fn.assert_called_once()
        self.assertEqual(restart_fn.call_args.kwargs.get("poll"), True)
        self.assertEqual(restart_fn.call_args.args[2], "incremental-next-pass")

    def test_non_incremental_optional_lane_never_restarts_in_portable_mode(self):
        receipts = ReceiptCollector()
        with mock.patch.object(supervisor, "last_queued", return_value=5), \
             mock.patch.object(supervisor, "clean_exit", return_value=True), \
             mock.patch.object(supervisor, "restart", return_value={"state": "dead", "action": "restart"}) as restart_fn:
            supervisor.check_lane(receipts, lane("ox"), dry=False)
        restart_fn.assert_not_called()
        self.assertEqual(receipts.lines[-1]["state"], "unavailable")

    def test_graph_lane_launches_one_worker_from_installer_configuration(self):
        graph = next(item for item in supervisor.LANES if item["name"] == "graph")
        self.assertEqual(graph["script"], "backfill.py")
        self.assertEqual(graph["argv"], [
            "backfill.py", "--limit", "96", "--fetch", "12000",
            "--scan-points", "150000", "--scan-pages", "32",
            "--episodes", "96", "--episode-timeout", "420",
            "--pass-budget", "600", "--cleanup-margin", "30", "--workers", "1",
        ])
        self.assertEqual(graph["stall_s"], 90 * 60)
        self.assertEqual(graph["env"].get("PYTHONUNBUFFERED"), "1")
        self.assertNotIn("GRAPH_LLM_URLS", graph["env"])

    def test_fresh_stationary_graph_heartbeat_becomes_stalled(self):
        receipts = ReceiptCollector()
        progress = {
            "age_s": 61,
            "attempts_done": 4,
            "episodes_done": 4,
            "facts_done": 4,
            "failed": 0,
        }
        with mock.patch.object(supervisor, "read_lock", return_value=123), \
             mock.patch.object(supervisor, "pid_alive", return_value=True), \
             mock.patch.object(supervisor, "lane_pid_matches", return_value=True), \
             mock.patch.object(supervisor, "log_age_s", return_value=1), \
             mock.patch.object(supervisor, "graph_progress_snapshot", return_value=progress):
            result = supervisor.check_lane(receipts, lane("graph", incremental=True), dry=True)

        self.assertEqual(result["state"], "stalled")
        self.assertEqual(result["detail"]["progress_age_s"], 61)
        self.assertEqual(result["detail"]["would_action"], "kill+restart")

    def test_advancing_graph_counters_remain_healthy(self):
        receipts = ReceiptCollector()
        progress = {
            "age_s": 10,
            "attempts_done": 5,
            "episodes_done": 5,
            "facts_done": 6,
            "failed": 0,
        }
        with mock.patch.object(supervisor, "read_lock", return_value=123), \
             mock.patch.object(supervisor, "pid_alive", return_value=True), \
             mock.patch.object(supervisor, "lane_pid_matches", return_value=True), \
             mock.patch.object(supervisor, "log_age_s", return_value=1), \
             mock.patch.object(supervisor, "graph_progress_snapshot", return_value=progress):
            result = supervisor.check_lane(receipts, lane("graph", incremental=True), dry=True)

        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["action"], "noop")

    def test_graph_progress_parser_uses_counter_change_time_not_heartbeat_freshness(self):
        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "graph.log"
            log_path.write_text(
                "2026-09-05 10:00 RUN-START slice_episodes=2\n"
                "2026-09-05 10:00 WORKER-PROGRESS worker=0 shard=0 groups=2 "
                "attempts_done=1 episodes_done=1 facts_done=1 failed=0 "
                "progress_changed_epoch=100\n"
                "2026-09-05 10:30 WORKER-PROGRESS worker=0 shard=0 groups=2 "
                "attempts_done=1 episodes_done=1 facts_done=1 failed=0 "
                "progress_changed_epoch=100\n",
                encoding="utf-8",
            )
            with mock.patch.object(supervisor.time, "time", return_value=200):
                snapshot = supervisor.graph_progress_snapshot(log_path)

        self.assertEqual(snapshot["age_s"], 100)
        self.assertEqual(snapshot["attempts_done"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
