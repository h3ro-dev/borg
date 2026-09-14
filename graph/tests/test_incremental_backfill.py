#!/usr/bin/env python3
"""Focused, dependency-free tests for the GF-02 feed state machine."""

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backfill  # noqa: E402


DEFAULT_SCOPE = "personal:fixture-owner"


class FakeQdrant:
    def __init__(self, pages):
        self.pages = pages
        self.offsets = []

    def scroll(self, offset, limit):
        self.offsets.append(offset)
        return self.pages.get(offset, ([], None))


class FakeGraph:
    def __init__(self, failures=0):
        self.calls = []
        self.failures = failures

    async def add_episode(self, group):
        self.calls.append(group)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("forced extraction failure")
        return {"nodes": len(group["rows"]), "edges": 0}


class FakeProjector:
    def __init__(self, failures=0):
        self.calls = []
        self.failures = failures

    async def project(self, identity, pending):
        self.calls.append((identity, pending))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("forced projection failure")


def point(point_id, text="fact", run_id="run-a", scope="team:incremental-fixture"):
    payload = {
        "data": text,
        "run_id": run_id,
        "user_id": "fixture-owner",
        "thread_date": "2026-09-03",
        "kind": "incremental-fixture",
    }
    if scope is not None:
        payload["scope"] = scope
    return {"id": point_id, "payload": payload}


def run_once(source, graph, state_path, scope_path):
    return asyncio.run(
        backfill.run_once(
            source,
            graph,
            state_path=state_path,
            scope_map_path=scope_path,
            page_limit=100,
        )
    )


class IncrementalBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "backfill-state.json"
        self.scope_map = root / "scope-graphs.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load_state(self):
        return backfill.load_state(self.state)

    def test_cursor_resumes_from_next_page_offset(self):
        first = FakeQdrant({None: ([point("p1")], "cursor-1")})
        graph = FakeGraph()
        first_receipt = run_once(first, graph, self.state, self.scope_map)

        self.assertEqual(first.offsets, [None])
        self.assertEqual(self.load_state()["scan_offset"], "cursor-1")
        self.assertEqual(first_receipt["cursor_after"], "cursor-1")

        second = FakeQdrant({"cursor-1": ([point("p2")], None)})
        run_once(second, graph, self.state, self.scope_map)
        state = self.load_state()
        self.assertEqual(second.offsets, ["cursor-1"])
        self.assertIsNone(state["scan_offset"])
        self.assertEqual(state["scan_epoch"], 1)

    def test_cursor_wrap_finds_point_inserted_before_offset(self):
        graph = FakeGraph()
        first = FakeQdrant({None: ([point("p-middle")], "cursor-1")})
        run_once(first, graph, self.state, self.scope_map)
        second = FakeQdrant({"cursor-1": ([point("p-last")], None)})
        run_once(second, graph, self.state, self.scope_map)

        inserted_before_offset = point("p-before", text="inserted before cursor")
        wrapped = FakeQdrant({None: ([inserted_before_offset], "cursor-1")})
        receipt = run_once(wrapped, graph, self.state, self.scope_map)

        self.assertEqual(wrapped.offsets, [None])
        self.assertEqual(receipt["new_points"], 1)
        self.assertEqual(graph.calls[-1]["rows"][0]["point_id"], "p-before")

    def test_same_point_and_digest_is_idempotent(self):
        graph = FakeGraph()
        source = FakeQdrant({None: ([point("same")], None)})
        run_once(source, graph, self.state, self.scope_map)
        receipt = run_once(source, graph, self.state, self.scope_map)

        self.assertEqual(len(graph.calls), 1)
        self.assertEqual(receipt["new_episodes"], 0)
        self.assertEqual(receipt["duplicate_episodes"], 0)
        self.assertEqual(len(self.load_state()["graph_processed"]), 1)

    def test_changed_point_digest_reprocesses_once(self):
        graph = FakeGraph()
        run_once(FakeQdrant({None: ([point("changed", "old")], None)}), graph,
                 self.state, self.scope_map)
        second = run_once(
            FakeQdrant({None: ([point("changed", "new")], None)}),
            graph,
            self.state,
            self.scope_map,
        )
        third = run_once(
            FakeQdrant({None: ([point("changed", "new")], None)}),
            graph,
            self.state,
            self.scope_map,
        )

        self.assertEqual(len(graph.calls), 2)
        self.assertEqual(second["new_episodes"], 1)
        self.assertEqual(third["new_episodes"], 0)
        self.assertNotEqual(graph.calls[0]["episode_id"], graph.calls[1]["episode_id"])

    def test_new_point_on_old_run_id_creates_delta(self):
        graph = FakeGraph()
        run_once(FakeQdrant({None: ([point("old-1", run_id="old-run")], None)}),
                 graph, self.state, self.scope_map)
        run_once(
            FakeQdrant({None: ([
                point("old-1", run_id="old-run"),
                point("new-2", text="delta", run_id="old-run"),
            ], None)}),
            graph,
            self.state,
            self.scope_map,
        )

        self.assertEqual(len(graph.calls), 2)
        self.assertEqual([r["point_id"] for r in graph.calls[-1]["rows"]], ["new-2"])
        self.assertNotEqual(graph.calls[0]["episode_id"], graph.calls[-1]["episode_id"])

    def test_failure_stays_retryable(self):
        failing = FakeGraph(failures=1)
        receipt = run_once(FakeQdrant({None: ([point("retry")], None)}), failing,
                           self.state, self.scope_map)
        failed_state = self.load_state()
        self.assertEqual(receipt["retry_count"], 1)
        self.assertEqual(failed_state["graph_processed"], {})
        self.assertEqual(len(failed_state["retry"]), 1)

        succeeding = FakeGraph()
        run_once(FakeQdrant({None: ([point("retry")], None)}), succeeding,
                 self.state, self.scope_map)
        self.assertEqual(len(succeeding.calls), 1)
        self.assertEqual(self.load_state()["retry"], {})
        self.assertEqual(len(self.load_state()["graph_processed"]), 1)

    def test_projection_failure_stays_pending_without_graph_readd(self):
        graph = FakeGraph()
        source = FakeQdrant({None: ([point("projection")], None)})
        run_once(source, graph, self.state, self.scope_map)
        projector = FakeProjector(failures=1)
        result = asyncio.run(backfill.drain_projection(self.state, projector))

        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(self.load_state()["projection_pending"]), 1)

        rerun_graph = FakeGraph()
        run_once(source, rerun_graph, self.state, self.scope_map)
        self.assertEqual(rerun_graph.calls, [])
        self.assertEqual(len(projector.calls), 1)

    def test_missing_scope_maps_configured_personal_owner(self):
        graph = FakeGraph()
        run_once(FakeQdrant({None: ([point("missing-scope", scope=None)], None)}),
                 graph, self.state, self.scope_map)

        self.assertEqual(graph.calls[0]["scope"], DEFAULT_SCOPE)
        self.assertTrue(graph.calls[0]["group_id"].startswith("memscope_"))
        self.assertEqual(set(self.load_state()["graph_processed"]), {"missing-scope"})

    def test_scope_group_key_is_valid_and_collision_checked(self):
        try:
            scope = importlib.import_module("graph_scope")
        except ModuleNotFoundError as exc:
            self.fail(f"graph_scope.py is required: {exc}")

        key = scope.group_key_for_scope("team:memory-one-door-canary-a")
        self.assertRegex(key, r"^memscope_[0-9a-f]{24}$")
        self.assertTrue(scope.is_valid_group_key(key))

        registry = scope.ScopeRegistry(self.scope_map)
        with mock.patch.object(scope, "group_key_for_scope", return_value="memscope_collision"):
            registry.ensure_scope("team:first")
            with self.assertRaises(scope.ScopeCollisionError):
                registry.ensure_scope("team:second")

    def test_prune_projected_pending_drops_ledger_done_only(self):
        state = backfill.new_state()
        state["projection_pending"] = {
            "done-id": {"identity": "done-id"},
            "open-id": {"identity": "open-id"},
        }
        ledger_path = Path(self.tmp.name) / "graph-recall-projector-ledger.json"
        ledger_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "delivered": {"done-id": {"delivered_at_mdt": "now", "upserted": 1}},
                    "resolved": {},
                    "failures": {},
                }
            ),
            encoding="utf-8",
        )
        removed = backfill.prune_projected_pending(state, ledger_path)
        self.assertEqual(removed, 1)
        self.assertEqual(set(state["projection_pending"]), {"open-id"})

    def test_coordinator_prunes_ledger_done_pending(self):
        identity = "episode:point:" + ("c" * 64)
        state = backfill.new_state()
        state["projection_pending"][identity] = {"identity": identity}
        backfill.save_state_atomic(self.state, state)
        ledger_path = self.state.with_name("graph-recall-projector-ledger.json")
        ledger_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "delivered": {identity: {"delivered_at_mdt": "now", "upserted": 1}},
                    "resolved": {},
                    "failures": {},
                }
            ),
            encoding="utf-8",
        )
        called = []
        original = backfill.prune_projected_pending

        def wrapped(state_obj, path):
            called.append(str(path))
            return original(state_obj, path)

        with mock.patch.object(backfill, "prune_projected_pending", side_effect=wrapped):
            run_once(
                FakeQdrant({None: ([], None)}),
                FakeGraph(),
                self.state,
                self.scope_map,
            )
        self.assertEqual(len(called), 1)
        self.assertNotIn(identity, self.load_state()["projection_pending"])

    def test_state_write_is_atomic(self):
        state = backfill.new_state({"legacy-run": {"nodes": 1}})
        replace = Path(backfill.os.__file__) if False else None
        with mock.patch.object(backfill.os, "replace", wraps=__import__("os").replace) as atomic_replace:
            backfill.save_state_atomic(self.state, state)

        atomic_replace.assert_called_once()
        self.assertEqual(backfill.load_state(self.state)["schema"], 2)
        self.assertFalse(list(self.state.parent.glob(f".{self.state.name}.*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
