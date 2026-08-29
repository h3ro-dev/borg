"""Isolated proofs for Dream v2 wave 1 (never contacts the live store)."""

from __future__ import annotations

import copy
import contextlib
import importlib.machinery
import io
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
BIN = BASE / "bin"
SAFETY = importlib.machinery.SourceFileLoader(
    "mem0_dream_safety_test", str(BIN / "mem0_dream_safety.py")
).load_module()
CTL = importlib.machinery.SourceFileLoader("mem0ctl_wave1_test", str(BIN / "mem0ctl")).load_module()


class EmptyQdrant:
    """No-store fixture: scroll is empty and exposes no mutation methods."""

    def scroll(self, *_args, **_kwargs):
        return [], None


class DreamV2Wave1Test(unittest.TestCase):
    def test_token_gate_splits_and_quarantines(self):
        events = []
        rows = [{"id": f"row-{i}", "text": "x" * 160} for i in range(8)]
        old = os.environ.get("MEM0_TOKEN_GATE_FORCE_ESTIMATE")
        os.environ["MEM0_TOKEN_GATE_FORCE_ESTIMATE"] = "1"
        try:
            admissions = SAFETY.admit_serialized_prompt(
                phase="PROOF",
                model="qwen3.8:27b-mlx",
                rows=rows,
                render_prompt=lambda items: "INSTRUCTION\n" + "\n".join(row["text"] for row in items),
                log=events.append,
                envelope=80,
            )
            self.assertGreater(len(admissions), 1)
            self.assertTrue(all(item.measurement.tokens <= item.envelope for item in admissions))
            self.assertTrue(any(line.startswith("TOKEN-SPLIT") for line in events))
            self.assertTrue(any("tokens=" in line and "ESTIMATED" in line for line in events))
            quarantined = SAFETY.admit_serialized_prompt(
                phase="PROOF",
                model="qwen3.8:27b-mlx",
                rows=[{"id": "oversized-single", "text": "z" * 5000}],
                render_prompt=lambda items: "INSTRUCTION\n" + items[0]["text"],
                log=events.append,
                envelope=80,
            )
            self.assertEqual([], quarantined)
            self.assertTrue(any(line.startswith("TOKEN-QUARANTINE") and "oversized-single" in line for line in events))
        finally:
            if old is None:
                os.environ.pop("MEM0_TOKEN_GATE_FORCE_ESTIMATE", None)
            else:
                os.environ["MEM0_TOKEN_GATE_FORCE_ESTIMATE"] = old

    def test_newest_star_refuses_transitive_chain(self):
        import numpy as np

        vectors = np.asarray([[1.0, 0.0], [0.8, 0.6], [0.28, 0.96]], dtype=np.float32)
        pairs_i, pairs_j, weights = CTL.pair_index(vectors, 0.75)
        members, _capped, stats = CTL.cluster_star_anchored(
            vectors, pairs_i, pairs_j, weights, 0.75,
            anchor_order=CTL.newest_anchor_order(
                ["2026-08-28T03:00:00", "2026-08-27T03:00:00", "2026-08-26T03:00:00"], ["A", "B", "C"]
            ),
        )
        self.assertEqual([[0, 1], [2]], list(members.values()))
        self.assertGreaterEqual(stats["chain_links_refused"], 1)

    def test_snapshot_seed_idempotency_canary_failure_and_dry_dream(self):
        # This managed environment forbids listening on loopback, so the
        # isolated fixture emulates the two HTTP endpoints at the call seam.
        # The production code still uses the real 127.0.0.1 Qdrant API.
        dream = importlib.machinery.SourceFileLoader("mem0_dream_wave1_test", str(BIN / "mem0-dream")).load_module()
        snapshots = []

        def fake_qdrant_json(method, path, _body=None, **_kwargs):
            if method == "POST" and path == "/collections/studio0/snapshots":
                snapshots.append({"name": "dream-wave1-isolated.snapshot"})
                return {"result": snapshots[-1]}
            if method == "GET" and path == "/collections/studio0/snapshots":
                return {"result": snapshots}
            raise AssertionError((method, path))

        original_json, original_log = dream._qdrant_json, dream.LOG_F
        dream._qdrant_json = fake_qdrant_json
        dream.LOG_F = Path("/tmp/mem0-dream-wave1-snapshot.log")
        try:
            snapshot = dream.qdrant_snapshot_preflight()
            self.assertEqual("dream-wave1-isolated.snapshot", snapshot)
            listed = dream._qdrant_json("GET", "/collections/studio0/snapshots")["result"]
            self.assertIn(snapshot, [item["name"] for item in listed])
        finally:
            dream._qdrant_json, dream.LOG_F = original_json, original_log

        seed = importlib.machinery.SourceFileLoader("mem0_canary_seed_wave1_test", str(BIN / "mem0-canary-seed")).load_module()
        stored = {}
        original_seed = (seed.discover_scopes, seed.existing_rows, seed.embed, seed.request_json, sys.argv[:])

        def fake_upsert(_url, body=None, **_kwargs):
            for point in (body or {}).get("points") or []:
                stored[str(point["id"])] = point.get("payload") or {}
            return {"result": {"status": "completed"}}

        try:
            seed.discover_scopes = lambda *_args: ["ops", "team:project"]
            seed.existing_rows = lambda *_args: dict(stored)
            seed.embed = lambda _url, texts: [[0.0, 1.0] for _ in texts]
            seed.request_json = fake_upsert
            sys.argv = ["mem0-canary-seed", "--qdrant-url", "http://isolated", "--ollama-url", "http://isolated"]
            first_capture = io.StringIO()
            with contextlib.redirect_stdout(first_capture):
                seed.main()
            second_capture = io.StringIO()
            with contextlib.redirect_stdout(second_capture):
                seed.main()
            self.assertIn("inserted=20", first_capture.getvalue())
            self.assertIn("inserted=0", second_capture.getvalue())
        finally:
            seed.discover_scopes, seed.existing_rows, seed.embed, seed.request_json, sys.argv = original_seed

        canary_points = [{"id": pid, "payload": copy.deepcopy(payload)} for pid, payload in stored.items()]
        wrong = copy.deepcopy(canary_points[0])
        wrong["payload"]["status"] = "tombstoned"
        violations = SAFETY.check_canary_payloads([wrong, *canary_points[1:]])
        self.assertTrue(violations)
        self.assertIn("status=tombstoned,expected=live", " | ".join(violations))

        original_canary = (dream.qdrant_client, dream.CTL.scroll_all, dream.LOG_F)
        try:
            dream.qdrant_client = lambda: object()
            dream.CTL.scroll_all = lambda *_args, **_kwargs: [
                SimpleNamespace(id=point["id"], payload=point["payload"])
                for point in [wrong, *canary_points[1:]]
            ]
            dream.LOG_F = Path("/tmp/mem0-dream-wave1-canary-fail.log")
            marker = io.StringIO()
            with contextlib.redirect_stdout(marker):
                result = dream.check_canaries()
            self.assertFalse(result[0])
            self.assertIn("CANARY-FAIL", marker.getvalue())
        finally:
            dream.qdrant_client, dream.CTL.scroll_all, dream.LOG_F = original_canary

        # Exercise the real Dream dry/no-store control path with an empty
        # fixture.  run_step must never be reached and no Qdrant mutator exists.
        original = (dream.store_counts, dream.novelty_skip_count, dream.check_canaries,
                    dream.qdrant_client, dream.run_step, dream.LOG_F, sys.argv[:])
        ran = []
        try:
            dream.store_counts = lambda: (0, {}, 0, {CTL.TOMBSTONE_STATUS: 0, CTL.DECAY_STATUS: 0}, 0)
            dream.novelty_skip_count = lambda: 0
            dream.check_canaries = lambda: (True, 20, 0)
            dream.qdrant_client = lambda: EmptyQdrant()
            dream.run_step = lambda *args: ran.append(args)
            dream.LOG_F = Path("/tmp/mem0-dream-wave1-isolated.log")
            sys.argv = ["mem0-dream", "--dry-run", "--max-clusters", "0"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, dream.main())
            self.assertIn("DREAM-DRY-RUN", output.getvalue())
            self.assertIn("DREAM-END", output.getvalue())
            self.assertIn("canaries=PASS", output.getvalue())
            self.assertEqual([], ran)
        finally:
            (dream.store_counts, dream.novelty_skip_count, dream.check_canaries,
             dream.qdrant_client, dream.run_step, dream.LOG_F, sys.argv) = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
