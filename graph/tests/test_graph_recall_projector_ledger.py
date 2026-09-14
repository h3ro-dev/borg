#!/usr/bin/env python3
"""GF-03 tests for projector-owned delivery state and feed-state isolation."""

from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backfill  # noqa: E402


PROJECTOR_PATH = ROOT / "bin" / "graph-recall-projector"
loader = importlib.machinery.SourceFileLoader("gf03_projector", str(PROJECTOR_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
projector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = projector
loader.exec_module(projector)


SCOPE = "team:gf03-projector-ledger"
GROUP_KEY = projector.group_key_for_scope(SCOPE)
DIGEST = "b" * 64
EPISODE_ID = "episode-gf03"
POINT_ID = "point-gf03"
IDENTITY = f"{EPISODE_ID}:{POINT_ID}:{DIGEST}"


def active_edge():
    return {
        "uuid": "edge-gf03",
        "group_id": GROUP_KEY,
        "fact": "The ledger test has a scope-safe graph fact.",
        "fact_embedding": [0.25] * projector.PROJECTION_DIMENSION,
        "episodes": [EPISODE_ID],
        "created_at": "2026-09-03T12:00:00+00:00",
        "valid_at": "2026-09-03T12:00:00+00:00",
        "invalid_at": None,
        "expired_at": None,
        "attributes": {},
    }


class Source:
    def __init__(self):
        self.calls = []

    async def edges(self, group_id):
        self.calls.append(group_id)
        return [copy.deepcopy(active_edge())]


class EmptySource(Source):
    async def edges(self, group_id):
        self.calls.append(group_id)
        return []


class Store:
    def __init__(self):
        self.points = {}
        self.upsert_calls = 0
        self.fail = False

    def ensure_collection(self):
        return None

    def upsert(self, points):
        self.upsert_calls += 1
        if self.fail:
            raise RuntimeError("forced projection failure")
        for point in points:
            self.points[str(point["id"])] = copy.deepcopy(point)


class ProjectorLedgerTests(unittest.IsolatedAsyncioTestCase):
    def pending(self):
        return IDENTITY, {
            "identity": IDENTITY,
            "episode_id": EPISODE_ID,
            "point_id": POINT_ID,
            "payload_digest": DIGEST,
            "scope": SCOPE,
            "run_id": "run-gf03",
            "group_id": GROUP_KEY,
            "status": "pending",
            "created_at_mdt": datetime.now(timezone.utc).isoformat(),
        }

    def setup_files(self, root: Path):
        state_path = root / "backfill-state.json"
        map_path = root / "scope-graphs.json"
        ledger_path = root / "graph-recall-projector-ledger.json"
        map_path.write_text(
            json.dumps({"schema": 1, "scopes": {SCOPE: GROUP_KEY}}),
            encoding="utf-8",
        )
        identity, pending = self.pending()
        state = backfill.new_state()
        state["graph_processed"][POINT_ID] = {
            "point_id": POINT_ID,
            "episode_id": EPISODE_ID,
            "payload_digest": DIGEST,
            "scope": SCOPE,
            "group_id": GROUP_KEY,
        }
        state["projection_pending"][identity] = pending
        backfill.save_state_atomic(state_path, state)
        return state_path, map_path, ledger_path, identity

    async def test_delivered_identity_is_skipped_and_feed_state_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, map_path, ledger_path, identity = self.setup_files(Path(temp))
            before = state_path.read_bytes()
            store = Store()
            lane = projector.GraphRecallProjector(
                Source(),
                store,
                scope_map_path=map_path,
                ledger_path=ledger_path,
            )

            first = await lane.drain(state_path)
            self.assertEqual(first, {"attempted": 1, "delivered": 1, "failed": 0})
            self.assertEqual(state_path.read_bytes(), before)

            second = await lane.drain(state_path)
            self.assertEqual(second, {"attempted": 0, "delivered": 0, "failed": 0})
            self.assertEqual(lane.last_drain_stats["skipped"], 1)
            self.assertEqual(store.upsert_calls, 1)
            self.assertEqual(state_path.read_bytes(), before)

            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertIn(identity, ledger["delivered"])
            self.assertNotIn(identity, ledger["failures"])

    async def test_failed_delivery_retries_without_feed_state_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, map_path, ledger_path, identity = self.setup_files(Path(temp))
            before = state_path.read_bytes()
            store = Store()
            store.fail = True
            lane = projector.GraphRecallProjector(
                Source(),
                store,
                scope_map_path=map_path,
                ledger_path=ledger_path,
            )

            failed = await lane.drain(state_path)
            self.assertEqual(failed, {"attempted": 1, "delivered": 0, "failed": 1})
            self.assertEqual(state_path.read_bytes(), before)
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertIn(identity, ledger["failures"])

            store.fail = False
            delivered = await lane.drain(state_path)
            self.assertEqual(delivered, {"attempted": 1, "delivered": 1, "failed": 0})
            self.assertEqual(state_path.read_bytes(), before)

    async def test_resolved_identity_is_skipped_without_feed_state_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, map_path, ledger_path, identity = self.setup_files(Path(temp))
            state = backfill.load_state(state_path)
            state["projection_pending"][identity]["created_at_mdt"] = (
                datetime.now(timezone.utc) - timedelta(minutes=20)
            ).isoformat()
            backfill.save_state_atomic(state_path, state)
            before = state_path.read_bytes()
            lane = projector.GraphRecallProjector(
                EmptySource(),
                Store(),
                scope_map_path=map_path,
                ledger_path=ledger_path,
            )

            resolved = await lane.drain(state_path)
            self.assertEqual(resolved, {"attempted": 1, "delivered": 0, "failed": 0})
            self.assertEqual(lane.last_drain_stats["resolved"], 1)
            self.assertEqual(state_path.read_bytes(), before)

            skipped = await lane.drain(state_path)
            self.assertEqual(skipped, {"attempted": 0, "delivered": 0, "failed": 0})
            self.assertEqual(lane.last_drain_stats["skipped"], 1)
            self.assertEqual(state_path.read_bytes(), before)

            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertIn(identity, ledger["resolved"])

    def _processed_pending(self, root: Path):
        digest = "a" * 64
        identity = f"episode-wait:point-wait:{digest}"
        scope = SCOPE
        pending = {
            "identity": identity,
            "episode_id": "episode-wait",
            "point_id": "point-wait",
            "payload_digest": digest,
            "scope": scope,
            "run_id": "run-gf03",
            "group_id": GROUP_KEY,
            "status": "pending",
            "created_at_mdt": datetime.now(timezone.utc).isoformat(),
        }
        state_path = root / "backfill-state.json"
        map_path = root / "scope-graphs.json"
        ledger_path = root / "graph-recall-projector-ledger.json"
        map_path.write_text(
            json.dumps({"schema": 1, "scopes": {scope: GROUP_KEY}}),
            encoding="utf-8",
        )
        state = backfill.new_state()
        state["graph_processed"]["point-wait"] = {
            "point_id": "point-wait",
            "episode_id": "episode-wait",
            "payload_digest": digest,
            "scope": scope,
            "group_id": GROUP_KEY,
        }
        state["projection_pending"][identity] = pending
        backfill.save_state_atomic(state_path, state)
        return state_path, map_path, ledger_path, identity

    async def test_young_edgeless_episode_waits_without_failing_the_tick(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, map_path, ledger_path, identity = self._processed_pending(
                Path(temp)
            )
            before = state_path.read_bytes()
            lane = projector.GraphRecallProjector(
                EmptySource(),
                Store(),
                scope_map_path=map_path,
                ledger_path=ledger_path,
            )

            first = await lane.drain(state_path)
            self.assertEqual(first, {"attempted": 1, "delivered": 0, "failed": 0})
            self.assertEqual(lane.last_drain_stats["waiting"], 1)
            self.assertIsNone(lane.last_fail_class)
            self.assertEqual(state_path.read_bytes(), before)
            if ledger_path.exists():
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertNotIn(identity, ledger["delivered"])
                self.assertNotIn(identity, ledger["resolved"])
                self.assertNotIn(identity, ledger["failures"])

            second = await lane.drain(state_path)
            self.assertEqual(second, {"attempted": 1, "delivered": 0, "failed": 0})
            self.assertEqual(lane.last_drain_stats["waiting"], 1)

    async def test_raised_no_edge_wait_is_not_a_tick_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path, map_path, ledger_path, identity = self.setup_files(Path(temp))
            lane = projector.GraphRecallProjector(
                EmptySource(),
                Store(),
                scope_map_path=map_path,
                ledger_path=ledger_path,
            )

            async def boom(identity, pending, processed=None):
                raise projector.ProjectionRejected(projector.NO_EDGE_YET)

            lane.project = boom
            result = await lane.drain(state_path)
            self.assertEqual(result, {"attempted": 1, "delivered": 0, "failed": 0})
            self.assertEqual(lane.last_drain_stats["waiting"], 1)
            if ledger_path.exists():
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertNotIn(identity, ledger["failures"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
