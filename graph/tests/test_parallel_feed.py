#!/usr/bin/env python3
"""Focused GF-03 tests for sharding, one state writer, and progress output."""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backfill  # noqa: E402


def point(point_id: str, *, scope: str, run_id: str = "run") -> dict:
    return {
        "id": point_id,
        "payload": {
            "data": f"parallel test fact {point_id}",
            "run_id": run_id,
            "scope": scope,
            "user_id": "fixture-owner",
            "thread_date": "2026-09-03",
            "kind": "gf-03-test",
        },
    }


class Source:
    def __init__(self, points, cursor_after=None):
        self.points = points
        self.cursor_after = cursor_after
        self.offsets = []

    def scroll(self, offset, _limit):
        self.offsets.append(offset)
        return self.points, self.cursor_after


class Graph:
    def __init__(self, *, delay_s=0.0, failures=0):
        self.delay_s = delay_s
        self.failures = failures
        self.calls = []

    async def add_episode(self, group):
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.calls.append(group)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("forced graph failure")
        return {"nodes": len(group["rows"]), "edges": 0}


class ConcurrencyProbe(Graph):
    def __init__(self, shared):
        super().__init__()
        self.shared = shared

    async def add_episode(self, group):
        self.shared["active"] += 1
        self.shared["max_active"] = max(
            self.shared["max_active"], self.shared["active"]
        )
        try:
            await asyncio.sleep(0.02)
            self.calls.append(group)
            return {"nodes": len(group["rows"]), "edges": 0}
        finally:
            self.shared["active"] -= 1


class ParallelFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "backfill-state.json"
        self.scope_map = self.root / "scope-graphs.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_shard_assignment_is_stable_and_disjoint(self):
        groups = [
            {"group_id": f"memscope_{index:024x}", "rows": []}
            for index in range(32)
        ]
        first = backfill.assign_groups_to_shards(groups, 4)
        second = backfill.assign_groups_to_shards(groups, 4)
        self.assertEqual(first, second)
        owners = {
            group["group_id"]: worker
            for worker, shard in enumerate(first)
            for group in shard
        }
        self.assertEqual(set(owners), {group["group_id"] for group in groups})
        for left in range(4):
            left_keys = {group["group_id"] for group in first[left]}
            for right in range(left + 1, 4):
                right_keys = {group["group_id"] for group in first[right]}
                self.assertTrue(left_keys.isdisjoint(right_keys))

    def test_parallel_pass_incrementally_saves_from_one_coordinator(self):
        points = [
            point(str(index), scope=f"team:gf03-{index}", run_id=f"run-{index}")
            for index in range(24)
        ]
        writers = []

        def factory(_worker_index):
            writer = Graph()
            writers.append(writer)
            return writer

        writer_tasks = []
        real_save = backfill.save_state_atomic

        def save_from_coordinator(path, state):
            writer_tasks.append(asyncio.current_task())
            real_save(path, state)

        with mock.patch.object(backfill, "save_state_atomic", side_effect=save_from_coordinator) as save:
            receipt = asyncio.run(
                backfill.run_parallel_once(
                    Source(points),
                    factory,
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    page_limit=100,
                    worker_count=4,
                    emit_progress=False,
                )
            )

        self.assertEqual(save.call_count, len(points) + 1)
        self.assertEqual(len({id(task) for task in writer_tasks}), 1)
        self.assertEqual(receipt["worker_count"], 4)
        self.assertEqual(receipt["new_episodes"], len(points))
        self.assertEqual(
            sum(len(writer.calls) for writer in writers), len(points)
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(len(state["graph_processed"]), len(points))
        self.assertEqual(len(state["projection_pending"]), len(points))

    def test_worker_shards_execute_graph_writes_concurrently(self):
        points = [
            point(str(index), scope=f"team:gf03-concurrent-{index}", run_id=f"run-{index}")
            for index in range(48)
        ]
        shared = {"active": 0, "max_active": 0}
        writers = []

        def factory(_worker_index):
            writer = ConcurrencyProbe(shared)
            writers.append(writer)
            return writer

        receipt = asyncio.run(
            backfill.run_parallel_once(
                Source(points),
                factory,
                state_path=self.state,
                scope_map_path=self.scope_map,
                page_limit=100,
                worker_count=4,
                emit_progress=False,
            )
        )

        active_shards = sum(
            1 for item in receipt["worker_stats"] if item["groups_total"]
        )
        self.assertGreaterEqual(active_shards, 2)
        self.assertGreater(shared["max_active"], 1)
        self.assertEqual(sum(len(writer.calls) for writer in writers), len(points))

    def test_progress_line_is_emitted_during_slow_worker(self):
        points = [point("slow", scope="team:gf03-slow")]
        lines = []
        with mock.patch.object(backfill, "log", side_effect=lines.append):
            asyncio.run(
                backfill.run_parallel_once(
                    Source(points),
                    lambda _worker: Graph(delay_s=0.06),
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    page_limit=10,
                    worker_count=1,
                    emit_progress=True,
                    progress_interval_s=0.01,
                )
            )

        progress = [line for line in lines if "WORKER-PROGRESS" in line]
        self.assertGreaterEqual(len(progress), 3)
        self.assertTrue(
            all(
                field in line
                for line in progress
                for field in (
                    "episodes_done=",
                    "facts_done=",
                    "rate_facts_per_hour=",
                )
            )
        )

    def test_llm_url_pool_normalizes_explicit_pool_and_uses_portable_config(self):
        self.assertEqual(
            backfill.llm_url_pool("http://a:11460,http://b:11461/v1/"),
            ["http://a:11460/v1", "http://b:11461/v1"],
        )
        with mock.patch.dict(
            backfill.os.environ,
            {"GRAPH_LLM_URLS": "http://one:1/v1,http://two:2/v1"},
            clear=False,
        ):
            self.assertEqual(
                backfill.llm_url_pool(),
                [str(backfill.CONFIG.values["BORG_GRAPH_LLM_URL"])],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
