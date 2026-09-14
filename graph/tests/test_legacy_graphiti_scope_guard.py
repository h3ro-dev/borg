#!/usr/bin/env python3
"""Focused tests for the non-flagged legacy backfill-v1 scope guard."""

from __future__ import annotations

import asyncio
import importlib.machinery
import json
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest import mock


SERVER = importlib.machinery.SourceFileLoader(
    "od01_legacy_graph_server",
    str(Path(__file__).resolve().parents[1] / "bin/graphiti-mcp-server"),
).load_module()


def access_token(name, scopes):
    return SimpleNamespace(
        client_id=name,
        scopes=list(scopes),
        claims={"principal": name},
    )


class LegacyGraphScopeGuardTests(unittest.TestCase):
    def test_legacy_8767_denies_restricted_principal_before_query(self):
        query_calls = []

        def graphiti_called():
            query_calls.append("graphiti")
            raise AssertionError("restricted principal reached graphiti")

        def falkor_called():
            query_calls.append("falkor")
            raise AssertionError("restricted principal reached FalkorDB")

        restricted = access_token("synthetic-restricted", ["team:project"])
        with mock.patch.object(SERVER, "get_access_token", return_value=restricted), mock.patch.object(
            SERVER, "graphiti", side_effect=graphiti_called
        ), mock.patch.object(SERVER, "falkor_graph", side_effect=falkor_called):
            with self.assertRaises(SERVER.ToolError):
                asyncio.run(SERVER.graph_search("legacy canary"))
            with self.assertRaises(SERVER.ToolError):
                SERVER.entity_timeline("legacy")
            with self.assertRaises(SERVER.ToolError):
                SERVER.recent_episodes()
            with self.assertRaises(SERVER.ToolError):
                SERVER.graph_stats()
        self.assertEqual(query_calls, [])

    def test_legacy_8767_allows_synthetic_full_access_principal(self):
        query_calls = []

        class FakeGraphiti:
            async def search(self, query, group_ids, num_results):
                query_calls.append((query, list(group_ids), num_results))
                return [
                    SimpleNamespace(
                        fact="legacy canary",
                        valid_at=None,
                        invalid_at=None,
                    )
                ]

        full_access = access_token("synthetic-full", ["*"])
        with mock.patch.object(SERVER, "get_access_token", return_value=full_access), mock.patch.object(
            SERVER, "graphiti", return_value=FakeGraphiti()
        ), mock.patch.object(SERVER, "_log"):
            payload = json.loads(asyncio.run(SERVER.graph_search("legacy canary")))
        self.assertEqual(payload["graph"], "backfill-v1")
        self.assertEqual(payload["results"][0]["fact"], "legacy canary")
        self.assertEqual(query_calls, [("legacy canary", ["backfill-v1"], 10)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
