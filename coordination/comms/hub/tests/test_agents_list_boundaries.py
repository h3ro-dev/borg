"""Regression tests for estate.read response bounds and bounded agents.list.

D1: the hub capped ``estate.read context`` at 65536 bytes while the Estate model
publishes ``bounds.context.max_response_bytes`` = 98304 and now emits ~85 KB, so
every context read failed with 503 ``estate_unavailable`` and no diagnosable cause.

D2: ``agents.list`` ignored ``limit``/``cursor`` and returned every registered
agent; the registry grew past the 4 MiB client limit, so no client could list or
resolve agents at all.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from comms.hub import service as service_module
from comms.hub.service import HubService, TransportConfigurationError
from comms.hub.store import MAX_LIMIT, Store


class _AuthorizingStore:
    """Minimal Store stand-in: estate reads never enter Store.call."""

    def authorize(self, actor, operation, scope):
        return {"allowed": actor == "caller"}

    def call(self, *args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("estate reads must not enter Store.call")


class _Fleet:
    def read(self, scope):
        return {"schema": "fleet-context/v1"}


def _reader_factory(*, context_bytes, published=None, capabilities_raises=False, fail=False):
    calls = []

    def factory(ownership):
        class Reader:
            def read(self, action, **params):
                calls.append(action)
                if fail:
                    raise RuntimeError("private reader failure text")
                if action == "capabilities":
                    if capabilities_raises:
                        raise RuntimeError("capabilities unavailable")
                    bounds = {} if published is None else {"context": {"max_response_bytes": published}}
                    return {"schema": "eco-estate/v1", "action": "capabilities", "bounds": bounds}
                payload = "x" * max(0, context_bytes - 60)
                return {"schema": "eco-estate/v1", "action": action, "value": payload}
        return Reader()

    return factory, calls


class EstateReadBoundTests(unittest.TestCase):
    def service(self, factory):
        return HubService(store=_AuthorizingStore(), credentials={"caller": "local-test-token-value-only"},
                          fleet_context_reader=_Fleet(), estate_reader_factory=factory)

    def test_context_between_old_cap_and_published_bound_is_served(self):
        # Exercise a response above the former 64 KiB internal cap.
        factory, calls = _reader_factory(context_bytes=84_850)
        result = self.service(factory).call("caller", "estate.read", {"action": "context"})
        self.assertEqual(result["schema"], "eco-estate/v1")
        self.assertGreater(len(service_module.compact_json(result).encode("utf-8")), 65536)
        # The default bound already fits; no extra capabilities read is spent.
        self.assertEqual(calls, ["context"])

    def test_context_above_default_uses_published_bound(self):
        factory, calls = _reader_factory(context_bytes=120_000, published=131072)
        result = self.service(factory).call("caller", "estate.read", {"action": "context"})
        self.assertEqual(result["schema"], "eco-estate/v1")
        self.assertEqual(calls, ["context", "capabilities"])

    def test_context_above_published_bound_is_typed_413(self):
        factory, _ = _reader_factory(context_bytes=120_000, published=98304)
        with redirect_stderr(io.StringIO()) as log:
            with self.assertRaises(TransportConfigurationError) as raised:
                self.service(factory).call("caller", "estate.read", {"action": "context"})
        self.assertEqual(raised.exception.code, "output_bound")
        self.assertEqual(raised.exception.status, 413)
        record = json.loads(log.getvalue().strip().splitlines()[-1])
        self.assertEqual(record["estate_read"], "output_bound")
        self.assertEqual(record["action"], "context")
        self.assertEqual(record["maximum"], 98304)

    def test_hub_ceiling_caps_a_larger_published_bound(self):
        factory, _ = _reader_factory(context_bytes=2 * 1024 * 1024, published=8 * 1024 * 1024)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(TransportConfigurationError) as raised:
                self.service(factory).call("caller", "estate.read", {"action": "context"})
        self.assertEqual(raised.exception.code, "output_bound")

    def test_failing_capabilities_keeps_default_bound(self):
        factory, _ = _reader_factory(context_bytes=120_000, capabilities_raises=True)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(TransportConfigurationError) as raised:
                self.service(factory).call("caller", "estate.read", {"action": "context"})
        self.assertEqual(raised.exception.code, "output_bound")

    def test_reader_failure_is_503_with_diagnosable_class_and_no_private_text(self):
        factory, _ = _reader_factory(context_bytes=1000, fail=True)
        with redirect_stderr(io.StringIO()) as log:
            with self.assertRaises(TransportConfigurationError) as raised:
                self.service(factory).call("caller", "estate.read", {"action": "context"})
        self.assertEqual(raised.exception.code, "estate_unavailable")
        self.assertEqual(raised.exception.status, 503)
        self.assertNotIn("private", str(raised.exception))
        record = json.loads(log.getvalue().strip().splitlines()[-1])
        self.assertEqual(record, {"estate_read": "unavailable", "action": "context", "error_type": "RuntimeError"})
        self.assertNotIn("private", log.getvalue())


class AgentsListPagingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = Store(Path(self.tempdir.name) / "hub.sqlite3")
        self.addCleanup(self.store.close)
        self.store.bootstrap_owner("fixture-owner", "test-approval")
        self.total = MAX_LIMIT + 50
        for index in range(self.total):
            runtime = "codex" if index % 2 else "claude-code"
            machine = "node%d" % (index % 3)
            self.store.call("fixture-owner", "agents.register",
                            {"agent_id": "agent-%03d" % index, "runtime": runtime, "machine": machine},
                            "register-%03d" % index)

    def test_default_page_is_bounded_and_newest_first(self):
        page = self.store.call("fixture-owner", "agents.list", {})
        self.assertEqual(len(page["agents"]), MAX_LIMIT)
        self.assertEqual(page["agents"][0]["agent_id"], "agent-%03d" % (self.total - 1))
        self.assertTrue(page["page"]["has_more"])
        self.assertIsInstance(page["page"]["next_cursor"], str)
        self.assertEqual(page["page"]["ordering"], "creation_sequence_desc")

    def test_cursor_pages_cover_the_registry_exactly_once(self):
        seen = []
        cursor = None
        for _ in range(10):
            params = {"limit": 60}
            if cursor:
                params["cursor"] = cursor
            page = self.store.call("fixture-owner", "agents.list", params)
            seen.extend(agent["agent_id"] for agent in page["agents"])
            if not page["page"]["has_more"]:
                break
            cursor = page["page"]["next_cursor"]
        self.assertEqual(len(seen), self.total)
        self.assertEqual(len(set(seen)), self.total)

    def test_reregistration_keeps_creation_sequence_stable(self):
        self.store.call("fixture-owner", "agents.register",
                        {"agent_id": "agent-000", "runtime": "codex", "machine": "node9"}, "register-again")
        page = self.store.call("fixture-owner", "agents.list", {"limit": 5})
        self.assertNotIn("agent-000", [agent["agent_id"] for agent in page["agents"]])
        exact = self.store.call("fixture-owner", "agents.list", {"agent_id": "agent-000"})
        self.assertEqual([agent["machine"] for agent in exact["agents"]], ["node9"])

    def test_exact_filters_and_limit_bounds(self):
        codex = self.store.call("fixture-owner", "agents.list", {"runtime": "codex", "limit": MAX_LIMIT})
        self.assertTrue(codex["agents"])
        self.assertTrue(all(agent["runtime"] == "codex" for agent in codex["agents"]))
        machine = self.store.call("fixture-owner", "agents.list", {"machine": "node2", "runtime": "codex"})
        self.assertTrue(all(agent["machine"] == "node2" and agent["runtime"] == "codex" for agent in machine["agents"]))
        self.assertEqual(self.store.call("fixture-owner", "agents.list", {"agent_id": "absent"})["agents"], [])
        from comms.hub.store import HubError
        with self.assertRaises(HubError):
            self.store.call("fixture-owner", "agents.list", {"limit": MAX_LIMIT + 1})
        with self.assertRaises(HubError):
            self.store.call("fixture-owner", "agents.list", {"cursor": "not-a-cursor"})

    def test_page_response_stays_inside_client_limit(self):
        page = self.store.call("fixture-owner", "agents.list", {"limit": MAX_LIMIT})
        self.assertLess(len(json.dumps(page).encode("utf-8")), 4 * 1024 * 1024)


class AgentsListLargeRegistryTests(unittest.TestCase):
    """Registry larger than the 1000-row candidate window."""

    TOTAL = 1250

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = Store(Path(self.tempdir.name) / "hub.sqlite3")
        self.addCleanup(self.store.close)
        self.store.bootstrap_owner("fixture-owner", "test-approval")
        for index in range(self.TOTAL):
            self.store.call("fixture-owner", "agents.register",
                            {"agent_id": "agent-%04d" % index, "runtime": "codex" if index % 4 else "claude-code",
                             "machine": "node%d" % (index % 5)}, "register-%04d" % index)

    def test_exact_agent_id_lookup_beyond_the_scan_window(self):
        oldest = self.store.call("fixture-owner", "agents.list", {"agent_id": "agent-0000"})
        self.assertEqual([agent["agent_id"] for agent in oldest["agents"]], ["agent-0000"])
        self.assertFalse(oldest["page"]["has_more"])
        self.assertEqual(self.store.call("fixture-owner", "agents.list", {"agent_id": "agent-9999"})["agents"], [])

    def test_filtered_cursor_walk_is_exact_once(self):
        seen = []
        cursor = None
        for _ in range(200):
            params = {"limit": 25, "runtime": "codex", "machine": "node3"}
            if cursor:
                params["cursor"] = cursor
            page = self.store.call("fixture-owner", "agents.list", params)
            seen.extend(agent["agent_id"] for agent in page["agents"])
            self.assertTrue(all(a["runtime"] == "codex" and a["machine"] == "node3" for a in page["agents"]))
            if not page["page"]["has_more"]:
                break
            cursor = page["page"]["next_cursor"]
        expected = ["agent-%04d" % i for i in range(self.TOTAL) if i % 4 and i % 5 == 3]
        self.assertEqual(sorted(seen), sorted(expected))
        self.assertEqual(len(seen), len(set(seen)))

    def test_cursor_is_bound_to_query_and_rejects_tampering(self):
        from comms.hub.store import HubError
        first = self.store.call("fixture-owner", "agents.list", {"limit": 10, "runtime": "codex"})
        cursor = first["page"]["next_cursor"]
        for params in ({"limit": 10, "runtime": "claude-code", "cursor": cursor},
                       {"limit": 11, "runtime": "codex", "cursor": cursor},
                       {"limit": 10, "runtime": "codex", "cursor": cursor[:-4] + "0000"}):
            with self.assertRaises(HubError) as raised:
                self.store.call("fixture-owner", "agents.list", params)
            self.assertEqual(raised.exception.code, "invalid_cursor")
        with self.assertRaises(HubError):
            self.store.call("other", "agents.list", {"limit": 10, "runtime": "codex", "cursor": cursor})

    def test_registration_during_paging_does_not_duplicate_or_skip(self):
        first = self.store.call("fixture-owner", "agents.list", {"limit": 100})
        self.store.call("fixture-owner", "agents.register", {"agent_id": "agent-late", "runtime": "codex", "machine": "node0"}, "register-late")
        seen = [a["agent_id"] for a in first["agents"]]
        cursor = first["page"]["next_cursor"]
        while cursor:
            page = self.store.call("fixture-owner", "agents.list", {"limit": 100, "cursor": cursor})
            seen.extend(a["agent_id"] for a in page["agents"])
            cursor = page["page"]["next_cursor"] if page["page"]["has_more"] else None
        self.assertEqual(len(seen), self.TOTAL)
        self.assertEqual(len(set(seen)), self.TOTAL)
        self.assertNotIn("agent-late", seen)
        self.assertEqual(self.store.call("fixture-owner", "agents.list", {"limit": 1})["agents"][0]["agent_id"], "agent-late")


if __name__ == "__main__":
    unittest.main()
