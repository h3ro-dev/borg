#!/usr/bin/env python3
"""Focused fake-backed proof for bounded Graphiti feed recovery."""

import asyncio
import contextlib
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backfill  # noqa: E402


def point(point_id, *, scope, run_id, text=None):
    return {
        "id": point_id,
        "payload": {
            "data": text or f"recovery fact {point_id}",
            "run_id": run_id,
            "scope": scope,
            "user_id": "fixture-owner",
            "thread_date": "2026-09-05",
            "kind": "recovery-test",
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


class RecordingWriter:
    def __init__(self, *, hang_runs=()):
        self.hang_runs = set(hang_runs)
        self.calls = []
        self.closed = False

    async def add_episode(self, group):
        self.calls.append(group)
        if group["run_id"] in self.hang_runs:
            await asyncio.Event().wait()
        return {"nodes": len(group["rows"]), "edges": 0}

    async def close(self):
        self.closed = True


class SlowCancellationWriter:
    def __init__(self):
        self.cancelled = False
        self.late_write = False

    async def add_episode(self, _group):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            await asyncio.sleep(0.03)
            raise
        self.late_write = True


class AlwaysUnprovenWriter:
    def __init__(self):
        self.attempts = 0

    async def add_episode(self, _group):
        self.attempts += 1
        raise backfill.EpisodeCompletionUnproven("ambiguous physical episode")


class SelectiveWriter:
    def __init__(self):
        self.calls = []

    async def add_episode(self, group):
        self.calls.append(group)
        if group["run_id"].startswith("retry-"):
            raise RuntimeError("retry remains unavailable")
        return {"nodes": len(group["rows"]), "edges": 0}


def distinct_worker_scopes(scope_map, worker_count=2):
    registry = backfill.ScopeRegistry(scope_map)
    found = {}
    for index in range(100):
        scope = f"team:recovery-shard-{index}"
        graph_key = registry.ensure_scope(scope)
        worker = backfill.shard_index_for_graph_key(graph_key, worker_count)
        found.setdefault(worker, scope)
        if len(found) == worker_count:
            return found
    raise AssertionError("could not find one scope for each worker")


class FeedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "backfill-state.json"
        self.scope_map = self.root / "scope-graphs.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_never_returning_add_times_out_retryably_and_later_group_runs(self):
        writer = RecordingWriter(hang_runs={"run-hung"})
        points = [
            point("hung", scope="team:timeout", run_id="run-hung"),
            point("later", scope="team:timeout", run_id="run-later"),
        ]

        receipt = asyncio.run(
            backfill.run_parallel_once(
                Source(points),
                writer,
                state_path=self.state,
                scope_map_path=self.scope_map,
                worker_count=1,
                emit_progress=False,
                episode_timeout_s=0.02,
            )
        )

        state = backfill.load_state(self.state)
        self.assertEqual([call["run_id"] for call in writer.calls], ["run-hung", "run-later"])
        self.assertEqual(receipt["retry_count"], 1)
        self.assertEqual(set(state["graph_processed"]), {"later"})
        retry = next(iter(state["retry"].values()))
        self.assertEqual(retry["last_error"], "EpisodeTimeout")

    def test_timeout_waits_for_cancellation_and_never_leaves_a_late_writer(self):
        writer = SlowCancellationWriter()
        started = time.monotonic()

        with self.assertRaises(backfill.EpisodeTimeout):
            asyncio.run(
                backfill._add_graph_episode(
                    writer,
                    {"episode_id": "cancel-safe"},
                    timeout_s=0.01,
                )
            )

        self.assertGreaterEqual(time.monotonic() - started, 0.03)
        self.assertTrue(writer.cancelled)
        self.assertFalse(writer.late_write)

    def test_other_worker_is_durable_while_one_worker_is_still_hung(self):
        async def scenario():
            scopes = distinct_worker_scopes(self.scope_map)
            writers = {
                0: RecordingWriter(hang_runs={"run-hung"}),
                1: RecordingWriter(),
            }
            points = [
                point("hung", scope=scopes[0], run_id="run-hung"),
                point("saved", scope=scopes[1], run_id="run-saved"),
            ]
            task = asyncio.create_task(
                backfill.run_parallel_once(
                    Source(points, "next-page"),
                    lambda worker: writers[worker],
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    worker_count=2,
                    emit_progress=False,
                    episode_timeout_s=0.5,
                )
            )
            for _ in range(100):
                await asyncio.sleep(0.005)
                if self.state.exists() and "saved" in backfill.load_state(self.state)["graph_processed"]:
                    break
            state = backfill.load_state(self.state)
            self.assertIn("saved", state["graph_processed"])
            self.assertNotIn("hung", state["graph_processed"])
            self.assertIsNone(state["scan_offset"])
            self.assertFalse(task.done())
            return await task

        receipt = asyncio.run(scenario())
        self.assertEqual(receipt["retry_count"], 1)
        self.assertEqual(receipt["checkpoint_batch_size"], 1)
        self.assertEqual(receipt["max_uncheckpointed_successes"], 2)
        self.assertEqual(backfill.load_state(self.state)["scan_offset"], "next-page")

    def test_interruption_keeps_cursor_skips_saved_work_and_closes_writers(self):
        async def scenario():
            scopes = distinct_worker_scopes(self.scope_map)
            writers = {
                0: RecordingWriter(hang_runs={"run-hung"}),
                1: RecordingWriter(),
            }
            points = [
                point("hung", scope=scopes[0], run_id="run-hung"),
                point("saved", scope=scopes[1], run_id="run-saved"),
            ]
            task = asyncio.create_task(
                backfill.run_parallel_once(
                    Source(points, "next-page"),
                    lambda worker: writers[worker],
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    worker_count=2,
                    emit_progress=False,
                    episode_timeout_s=10,
                )
            )
            for _ in range(100):
                await asyncio.sleep(0.005)
                if self.state.exists() and "saved" in backfill.load_state(self.state)["graph_processed"]:
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return points, writers

        points, writers = asyncio.run(scenario())
        interrupted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIsNone(interrupted["scan_offset"])
        self.assertEqual(set(interrupted["graph_processed"]), {"saved"})
        self.assertTrue(all(writer.closed for writer in writers.values()))

        rerun_writers = []

        def factory(_worker):
            writer = RecordingWriter()
            rerun_writers.append(writer)
            return writer

        asyncio.run(
            backfill.run_parallel_once(
                Source(points, "next-page"),
                factory,
                state_path=self.state,
                scope_map_path=self.scope_map,
                worker_count=2,
                emit_progress=False,
                episode_timeout_s=0.1,
            )
        )
        rerun_calls = [call for writer in rerun_writers for call in writer.calls]
        self.assertEqual([call["run_id"] for call in rerun_calls], ["run-hung"])
        self.assertEqual(backfill.load_state(self.state)["scan_offset"], "next-page")

    def test_cursor_and_complete_scan_advance_only_in_final_commit(self):
        initial = backfill.new_state()
        initial["scan_offset"] = "old-cursor"
        backfill.save_state_atomic(self.state, initial)
        points = [
            point("one", scope="team:cursor", run_id="run-one"),
            point("two", scope="team:cursor", run_id="run-two"),
        ]
        snapshots = []
        real_save = backfill.save_state_atomic

        def capture(path, state):
            snapshots.append(json.loads(json.dumps(state)))
            real_save(path, state)

        with mock.patch.object(backfill, "save_state_atomic", side_effect=capture):
            asyncio.run(
                backfill.run_parallel_once(
                    Source(points, None),
                    RecordingWriter(),
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    worker_count=1,
                    emit_progress=False,
                    episode_timeout_s=0.1,
                )
            )

        self.assertEqual(len(snapshots), 3)
        self.assertTrue(all(item["scan_offset"] == "old-cursor" for item in snapshots[:-1]))
        self.assertTrue(all(item["last_complete_scan_mdt"] is None for item in snapshots[:-1]))
        self.assertIsNone(snapshots[-1]["scan_offset"])
        self.assertIsNotNone(snapshots[-1]["last_complete_scan_mdt"])
        self.assertEqual(snapshots[-1]["scan_epoch"], 1)

    def test_incremental_commit_preserves_source_and_projection_identity(self):
        source_point = point(
            "identity-point",
            scope="team:identity",
            run_id="identity-run",
            text="identity fact",
        )
        writer = RecordingWriter()
        snapshots = []
        real_save = backfill.save_state_atomic

        def capture(path, state):
            snapshots.append(json.loads(json.dumps(state)))
            real_save(path, state)

        with mock.patch.object(backfill, "save_state_atomic", side_effect=capture):
            asyncio.run(
                backfill.run_parallel_once(
                    Source([source_point], "next-page"),
                    writer,
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    worker_count=1,
                    emit_progress=False,
                    episode_timeout_s=0.1,
                )
            )

        group = writer.calls[0]
        row = group["rows"][0]
        accepted = snapshots[0]["graph_processed"]["identity-point"]
        identity = backfill.projection_identity(group["episode_id"], row)
        pending = snapshots[0]["projection_pending"][identity]
        expected = {
            "point_id": "identity-point",
            "payload_digest": row["payload_digest"],
            "scope": "team:identity",
            "run_id": "identity-run",
            "group_id": group["group_id"],
            "episode_id": group["episode_id"],
        }
        self.assertEqual({key: accepted[key] for key in expected}, expected)
        self.assertEqual({key: pending[key] for key in expected}, expected)
        self.assertEqual(pending["identity"], identity)
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(accepted["processed_at_mdt"])
        self.assertTrue(pending["created_at_mdt"])

    def test_log_receipt_replaces_source_cursor_values_with_presence_flags(self):
        safe = backfill.safe_receipt_for_log(
            {
                "outcome": "PASS",
                "cursor_before": "source-point-a",
                "cursor_after": "source-point-b",
            }
        )

        encoded = json.dumps(safe)
        self.assertNotIn("source-point-a", encoded)
        self.assertNotIn("source-point-b", encoded)
        self.assertTrue(safe["cursor_before_present"])
        self.assertTrue(safe["cursor_after_present"])

    def test_schema_two_dictionary_rows_regain_explicit_point_id(self):
        state = backfill.new_state()
        state["graph_processed"]["legacy-point"] = {
            "payload_digest": "digest",
            "scope": "team:legacy",
            "run_id": "legacy-run",
            "group_id": "memscope_000000000000000000000000",
            "episode_id": "episode",
            "processed_at_mdt": "2026-09-04T01:02:03-06:00",
        }
        self.state.write_text(json.dumps(state), encoding="utf-8")

        loaded = backfill.load_state(self.state)

        self.assertEqual(
            loaded["graph_processed"]["legacy-point"]["point_id"],
            "legacy-point",
        )

    def test_ambiguous_unmarked_episode_is_quarantined_after_bounded_checks(self):
        source_point = point(
            "ambiguous-point",
            scope="team:ambiguous",
            run_id="ambiguous-run",
        )
        writer = AlwaysUnprovenWriter()

        receipts = [
            asyncio.run(
                backfill.run_once(
                    Source([source_point]),
                    writer,
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    episode_timeout_s=0.1,
                )
            )
            for _ in range(backfill.MAX_RECONCILIATION_ATTEMPTS + 2)
        ]

        retry = next(iter(backfill.load_state(self.state)["retry"].values()))
        self.assertEqual(writer.attempts, backfill.MAX_RECONCILIATION_ATTEMPTS + 1)
        self.assertEqual(retry["recovery_attempts"], 1)
        self.assertEqual(retry["status"], "recovery_completion_unproven")
        self.assertFalse(retry["retryable"])
        self.assertEqual(receipts[-1]["retry_count"], 0)
        self.assertEqual(receipts[-1]["outcome"], "PARTIAL")
        self.assertEqual(receipts[-1]["pending_graph_retries"], 1)
        self.assertEqual(receipts[-1]["unresolved_reconciliation"], 1)

    def test_retry_selection_rotates_and_keeps_current_page_progressing(self):
        state = backfill.new_state()
        retry_points = [
            point("retry-a", scope="team:a-retry", run_id="retry-a"),
            point("retry-b", scope="team:b-retry", run_id="retry-b"),
        ]
        registry = backfill.ScopeRegistry(self.scope_map)
        retry_order = []
        for source_point in retry_points:
            fact = backfill.point_to_fact(source_point)
            group = backfill.build_pending_groups([fact], state, registry)[0]
            retry_order.append((group["episode_id"], group["run_id"]))
            state["retry"][group["episode_id"]] = {
                "kind": "graph",
                "episode_id": group["episode_id"],
                "rows": [backfill._row_for_retry(fact)],
                "attempts": 0,
                "last_attempt_mdt": "2026-09-05T00:00:00-06:00",
            }
        expected_retry_runs = [run_id for _episode_id, run_id in sorted(retry_order)]
        backfill.save_state_atomic(self.state, state)
        writer = SelectiveWriter()
        current_a = point("current-a", scope="team:z-current", run_id="current-a")
        current_b = point("current-b", scope="team:z-current", run_id="current-b")

        asyncio.run(
            backfill.run_once(
                Source([current_a], "next-page"),
                writer,
                state_path=self.state,
                scope_map_path=self.scope_map,
                fact_limit=2,
                episode_timeout_s=0.1,
            )
        )
        first_runs = [group["run_id"] for group in writer.calls]
        asyncio.run(
            backfill.run_once(
                Source([current_a, current_b], None),
                writer,
                state_path=self.state,
                scope_map_path=self.scope_map,
                fact_limit=2,
                episode_timeout_s=0.1,
            )
        )
        second_runs = [group["run_id"] for group in writer.calls[len(first_runs) :]]

        self.assertEqual(set(first_runs), {expected_retry_runs[0], "current-a"})
        self.assertEqual(set(second_runs), {expected_retry_runs[1], "current-b"})
        self.assertEqual(
            [name for name in first_runs + second_runs if name.startswith("retry-")],
            expected_retry_runs,
        )
        accepted = backfill.load_state(self.state)["graph_processed"]
        self.assertEqual(set(accepted), {"current-a", "current-b"})

    def test_retry_episode_identity_is_not_regrouped_with_current_source_rows(self):
        state = backfill.new_state()
        registry = backfill.ScopeRegistry(self.scope_map)
        retry_source = [
            point("retry-one", scope="team:stable", run_id="stable-run"),
            point("retry-two", scope="team:stable", run_id="stable-run"),
        ]
        retry_facts = [backfill.point_to_fact(item) for item in retry_source]
        retry_group = backfill.build_pending_groups(retry_facts, state, registry)[0]
        state["retry"][retry_group["episode_id"]] = {
            "kind": "graph",
            "episode_id": retry_group["episode_id"],
            "rows": [backfill._row_for_retry(row) for row in retry_facts],
            "attempts": 1,
            "last_attempt_mdt": "2026-09-05T00:00:00-06:00",
        }
        backfill.save_state_atomic(self.state, state)
        current = point("current", scope="team:stable", run_id="stable-run")
        writer = RecordingWriter()

        asyncio.run(
            backfill.run_once(
                Source(retry_source + [current]),
                writer,
                state_path=self.state,
                scope_map_path=self.scope_map,
                episode_timeout_s=0.1,
            )
        )

        self.assertEqual(writer.calls[0]["episode_id"], retry_group["episode_id"])
        self.assertEqual(
            [row["point_id"] for row in writer.calls[0]["rows"]],
            ["retry-one", "retry-two"],
        )
        self.assertEqual(
            [row["point_id"] for row in writer.calls[1]["rows"]],
            ["current"],
        )

    def test_new_source_digest_quarantines_stale_retry_group_without_replaying_it(self):
        state = backfill.new_state()
        registry = backfill.ScopeRegistry(self.scope_map)
        old_source = [
            point("changed", scope="team:changed", run_id="changed-run", text="old"),
            point("unchanged", scope="team:changed", run_id="changed-run"),
        ]
        old_facts = [backfill.point_to_fact(item) for item in old_source]
        old_group = backfill.build_pending_groups(old_facts, state, registry)[0]
        state["retry"][old_group["episode_id"]] = {
            "kind": "graph",
            "episode_id": old_group["episode_id"],
            "rows": [backfill._row_for_retry(row) for row in old_facts],
            "attempts": 1,
            "last_attempt_mdt": "2026-09-05T00:00:00-06:00",
        }
        backfill.save_state_atomic(self.state, state)
        changed = point(
            "changed",
            scope="team:changed",
            run_id="changed-run",
            text="new",
        )
        writer = RecordingWriter()

        asyncio.run(
            backfill.run_once(
                Source([changed]),
                writer,
                state_path=self.state,
                scope_map_path=self.scope_map,
                episode_timeout_s=0.1,
            )
        )
        first_call_count = len(writer.calls)
        final_receipt = asyncio.run(
            backfill.run_once(
                Source([]),
                writer,
                state_path=self.state,
                scope_map_path=self.scope_map,
                episode_timeout_s=0.1,
            )
        )

        retry = backfill.load_state(self.state)["retry"][old_group["episode_id"]]
        self.assertEqual(first_call_count, 1)
        self.assertEqual([row["point_id"] for row in writer.calls[0]["rows"]], ["changed"])
        self.assertEqual(len(writer.calls), first_call_count)
        self.assertEqual(retry["status"], "superseded_by_source")
        self.assertFalse(retry["retryable"])
        self.assertEqual(final_receipt["outcome"], "PARTIAL")


class FakeNodeNotFoundError(Exception):
    pass


class FakeEpisodeType:
    text = "text"


class FakeEpisodicNode:
    def __init__(self, **values):
        self.__dict__.update(values)

    @classmethod
    async def get_by_uuid(cls, driver, _episode_uuid):
        if driver.node is None:
            raise FakeNodeNotFoundError()
        return driver.node

    async def save(self, driver):
        driver.node = self


class FakeDriver:
    def __init__(self):
        self.node = None
        self.completion_token = None
        self.mentions = 0
        self.relationships = {}

    async def execute_query(self, query, **params):
        if " SET " in query:
            if self.node is None:
                return ([], None, None)
            self.completion_token = params["completion_token"]
        if "count(m)" in query:
            return ([{"mention_count": self.mentions}], None, None)
        if "r.uuid IN" in query:
            records = [
                {"uuid": edge_uuid, **self.relationships[edge_uuid]}
                for edge_uuid in params["entity_edge_uuids"]
                if edge_uuid in self.relationships
            ]
            return (records, None, None)
        record = {"completion_token": self.completion_token}
        return ([record] if self.node is not None else [], None, None)


class FakePhysicalGraph:
    def __init__(self):
        self.driver = FakeDriver()
        self.add_calls = 0

    async def add_episode(self, **_kwargs):
        self.add_calls += 1
        return types.SimpleNamespace(nodes=[object()], edges=[])


@contextlib.contextmanager
def fake_graphiti_modules():
    package = types.ModuleType("graphiti_core")
    package.__path__ = []
    nodes = types.ModuleType("graphiti_core.nodes")
    nodes.EpisodeType = FakeEpisodeType
    nodes.EpisodicNode = FakeEpisodicNode
    errors = types.ModuleType("graphiti_core.errors")
    errors.NodeNotFoundError = FakeNodeNotFoundError
    with mock.patch.dict(
        sys.modules,
        {
            "graphiti_core": package,
            "graphiti_core.nodes": nodes,
            "graphiti_core.errors": errors,
        },
    ):
        yield


class FakeAsyncOpenAI:
    instances = []

    def __init__(self, **options):
        self.options = options
        self.closed = False
        self.instances.append(self)

    async def close(self):
        self.closed = True


class FakeConfig:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeOpenAIGenericClient:
    def __init__(self, *, config, client=None):
        self.config = config
        self.client = client


class FakeOpenAIEmbedder:
    def __init__(self, *, config, client=None):
        self.config = config
        self.client = client


class FakeOpenAIRerankerClient:
    def __init__(self, *, config, client):
        self.config = config
        self.client = client


@contextlib.contextmanager
def fake_graphiti_constructor_modules():
    FakeAsyncOpenAI.instances = []
    package = types.ModuleType("graphiti_core")
    package.__path__ = []
    package.Graphiti = object
    cross_encoder = types.ModuleType("graphiti_core.cross_encoder.openai_reranker_client")
    cross_encoder.OpenAIRerankerClient = FakeOpenAIRerankerClient
    driver = types.ModuleType("graphiti_core.driver.falkordb_driver")
    driver.FalkorDriver = object
    embedder = types.ModuleType("graphiti_core.embedder.openai")
    embedder.OpenAIEmbedder = FakeOpenAIEmbedder
    embedder.OpenAIEmbedderConfig = FakeConfig
    llm_package = types.ModuleType("graphiti_core.llm_client")
    llm_package.__path__ = []
    llm_package.LLMConfig = FakeConfig
    llm_generic = types.ModuleType("graphiti_core.llm_client.openai_generic_client")
    llm_generic.OpenAIGenericClient = FakeOpenAIGenericClient
    openai_module = types.ModuleType("openai")
    openai_module.AsyncOpenAI = FakeAsyncOpenAI
    with mock.patch.dict(
        sys.modules,
        {
            "graphiti_core": package,
            "graphiti_core.cross_encoder.openai_reranker_client": cross_encoder,
            "graphiti_core.driver.falkordb_driver": driver,
            "graphiti_core.embedder.openai": embedder,
            "graphiti_core.llm_client": llm_package,
            "graphiti_core.llm_client.openai_generic_client": llm_generic,
            "openai": openai_module,
        },
    ):
        yield


class NativeClientBoundsTests(unittest.TestCase):
    def test_writer_bounds_llm_and_embedding_requests_and_closes_both_clients(self):
        with mock.patch.dict(
            backfill.os.environ,
            {
                "GRAPH_NATIVE_REQUEST_TIMEOUT_S": "17",
                "GRAPH_NATIVE_MAX_RETRIES": "0",
            },
        ), fake_graphiti_constructor_modules():
            writer = backfill.GraphitiWriter()
            clients = list(FakeAsyncOpenAI.instances)
            asyncio.run(writer.close())

        self.assertEqual(len(clients), 2)
        self.assertEqual([client.options["timeout"] for client in clients], [17.0, 17.0])
        self.assertEqual([client.options["max_retries"] for client in clients], [0, 0])
        self.assertTrue(all(client.closed for client in clients))


class PhysicalWriteReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "backfill-state.json"
        self.scope_map = self.root / "scope-graphs.json"
        self.source_point = point(
            "physical-point", scope="team:physical", run_id="physical-run"
        )
        fact = backfill.point_to_fact(self.source_point)
        registry = backfill.ScopeRegistry(self.scope_map)
        self.group = backfill.build_pending_groups(
            [fact], backfill.new_state(), registry
        )[0]
        self.graph = FakePhysicalGraph()
        self.writer = object.__new__(backfill.GraphitiWriter)
        self.writer._graphs = {self.group["group_id"]: self.graph}

    def tearDown(self):
        self.temp.cleanup()

    def test_completed_physical_episode_reconciles_once_without_reextracting(self):
        with fake_graphiti_modules():
            first = asyncio.run(self.writer.add_episode(self.group))
            self.assertFalse(first["already_present"])
            self.assertEqual(self.graph.add_calls, 1)

            receipt = asyncio.run(
                backfill.run_once(
                    Source([self.source_point]),
                    self.writer,
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    episode_timeout_s=0.1,
                )
            )

        self.assertEqual(receipt["duplicate_episodes"], 1)
        self.assertEqual(self.graph.add_calls, 1)
        self.assertEqual(set(backfill.load_state(self.state)["graph_processed"]), {"physical-point"})

    def test_same_content_stub_is_retryable_and_not_accepted(self):
        self.graph.driver.node = FakeEpisodicNode(content=self.group["episode_body"])
        with fake_graphiti_modules():
            receipt = asyncio.run(
                backfill.run_once(
                    Source([self.source_point]),
                    self.writer,
                    state_path=self.state,
                    scope_map_path=self.scope_map,
                    episode_timeout_s=0.1,
                )
            )

        state = backfill.load_state(self.state)
        self.assertEqual(receipt["retry_count"], 1)
        self.assertEqual(state["graph_processed"], {})
        self.assertEqual(len(state["retry"]), 1)
        self.assertEqual(self.graph.add_calls, 0)

    def test_unmarked_episode_with_mentions_but_no_entity_edges_is_unproven(self):
        self.graph.driver.node = FakeEpisodicNode(
            content=self.group["episode_body"],
            group_id=self.group["group_id"],
            entity_edges=[],
        )
        self.graph.driver.mentions = 1

        with fake_graphiti_modules():
            with self.assertRaises(backfill.EpisodeCompletionUnproven):
                asyncio.run(self.writer.add_episode(self.group))

        self.assertEqual(self.graph.add_calls, 0)
        self.assertIsNone(self.graph.driver.completion_token)

    def test_unmarked_episode_edges_do_not_replace_completion_marker(self):
        episode_id = self.group["episode_id"]
        self.graph.driver.node = FakeEpisodicNode(
            content=self.group["episode_body"],
            group_id=self.group["group_id"],
            entity_edges=["edge-a", "edge-b"],
        )
        self.graph.driver.mentions = 1
        self.graph.driver.relationships = {
            "edge-a": {"episodes": [episode_id]},
        }

        with fake_graphiti_modules():
            with self.assertRaises(backfill.EpisodeCompletionUnproven):
                asyncio.run(self.writer.add_episode(self.group))
            self.assertIsNone(self.graph.driver.completion_token)

            self.graph.driver.relationships["edge-b"] = {"episodes": [episode_id]}
            with self.assertRaises(backfill.EpisodeCompletionUnproven):
                asyncio.run(self.writer.add_episode(self.group))

        self.assertIsNone(self.graph.driver.completion_token)
        self.assertEqual(self.graph.add_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
