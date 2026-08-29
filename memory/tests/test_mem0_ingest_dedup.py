#!/usr/bin/env python3
"""Fact-level ingest dedup (novelty_prune_events) + boilerplate skip in thread ingest."""

import os
import importlib.machinery
import json
import tempfile
import unittest
from pathlib import Path

CTL = importlib.machinery.SourceFileLoader(
    "mem0ctl_dedup_test", os.path.expanduser("~/Library/Memory/mem0/bin/mem0ctl")
).load_module()
INGEST = importlib.machinery.SourceFileLoader(
    "mem0_ingest_threads_test", os.path.expanduser("~/Library/Memory/mem0/bin/mem0-ingest-threads")
).load_module()


class FakeCount:
    def __init__(self, count):
        self.count = count


class FakeQdrant:
    """Returns a fixed duplicate count; raises when told to."""

    def __init__(self, dup_count=0, raise_on_count=False):
        self.dup_count = dup_count
        self.raise_on_count = raise_on_count
        self.calls = []

    def count(self, collection, count_filter=None, exact=True):
        if self.raise_on_count:
            raise ConnectionError("qdrant offline")
        self.calls.append(count_filter)
        return FakeCount(self.dup_count)


class FakeMemory:
    def __init__(self, raise_on_delete=False):
        self.deleted = []
        self.raise_on_delete = raise_on_delete

    def delete(self, memory_id):
        if self.raise_on_delete:
            raise RuntimeError("delete failed")
        self.deleted.append(memory_id)


class NoveltyPruneEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name) / "novelty-skipped.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    EV1 = "11111111-1111-4111-8111-111111111111"
    EV2 = "22222222-2222-4222-8222-222222222222"

    def events(self):
        return [
            {"id": self.EV1, "memory": "The Grok peer conductor at 127.0.0.1:4770 is a shared long-work bus."},
            {"id": self.EV2, "memory": "A genuinely new fact about the estate."},
        ]

    def test_exact_duplicate_event_is_deleted_and_receipted(self):
        m = FakeMemory()
        q = FakeQdrant(dup_count=3)
        kept, deduped = CTL.novelty_prune_events(
            m, self.events(), user_id="james", agent_id="thread-ingest", q=q, log_path=self.log
        )
        # every event matched the fake's duplicate count, so all are deleted
        self.assertEqual(deduped, 2)
        self.assertEqual(kept, [])
        self.assertEqual(m.deleted, [self.EV1, self.EV2])
        receipts = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0]["action"], "novelty-fact-skip")
        self.assertEqual(receipts[0]["duplicates_live"], 3)
        # privacy: receipt carries hashes and sizes, never the text
        self.assertNotIn("Grok peer conductor", self.log.read_text())

    def test_unique_event_is_kept(self):
        m = FakeMemory()
        q = FakeQdrant(dup_count=0)
        kept, deduped = CTL.novelty_prune_events(
            m, self.events(), user_id="james", agent_id="thread-ingest", q=q, log_path=self.log
        )
        self.assertEqual(deduped, 0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(m.deleted, [])
        self.assertFalse(self.log.exists())

    def test_lookup_failure_fails_open_and_keeps_rows(self):
        m = FakeMemory()
        q = FakeQdrant(raise_on_count=True)
        kept, deduped = CTL.novelty_prune_events(
            m, self.events(), user_id="james", agent_id="thread-ingest", q=q, log_path=self.log
        )
        self.assertEqual(deduped, 0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(m.deleted, [])

    def test_delete_failure_fails_open_and_keeps_row(self):
        m = FakeMemory(raise_on_delete=True)
        q = FakeQdrant(dup_count=1)
        kept, deduped = CTL.novelty_prune_events(
            m, self.events(), user_id="james", agent_id="thread-ingest", q=q, log_path=self.log
        )
        self.assertEqual(deduped, 0)
        self.assertEqual(len(kept), 2)

    def test_event_without_id_or_text_is_kept_untouched(self):
        m = FakeMemory()
        q = FakeQdrant(dup_count=5)
        odd = [{"id": None, "memory": "text but no id"}, {"id": self.EV1, "memory": ""}]
        kept, deduped = CTL.novelty_prune_events(
            m, odd, user_id="james", agent_id="thread-ingest", q=q, log_path=self.log
        )
        self.assertEqual(deduped, 0)
        self.assertEqual(len(kept), 2)

    def test_non_uuid_id_is_kept_never_self_deleted(self):
        # qdrant 400s HasIdCondition on non-point-id shapes; the guard must keep
        # the row rather than let the fail-open catch mask a broken filter.
        m = FakeMemory()
        q = FakeQdrant(dup_count=5)
        kept, deduped = CTL.novelty_prune_events(
            m, [{"id": "not-a-uuid", "memory": "some fact"}],
            user_id="james", agent_id="thread-ingest", q=q, log_path=self.log
        )
        self.assertEqual(deduped, 0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(m.deleted, [])
        self.assertEqual(q.calls, [])  # never even queried


class ThreadBoilerplateSkipTests(unittest.TestCase):
    def codex_msg(self, role, text):
        return {"type": "response_item", "payload": {"type": "message", "role": role,
                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}

    def write(self, rows):
        path = Path(self.temp.name) / "rollout-test.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_injected_policy_prefixes_are_skipped(self):
        rows = [
            self.codex_msg("user", "# AGENTS.md instructions\nTalk to me in ASD-STE100. My name is James."),
            self.codex_msg("user", "<recommended_plugins>stuff</recommended_plugins>"),
            self.codex_msg("user", "<skills_instructions>x</skills_instructions>"),
            self.codex_msg("user", "Please fix the parity gate on issue 954."),
            self.codex_msg("assistant", "Working on the parity gate now."),
        ]
        texts = [t for _, _, t in INGEST.extract_texts(self.write(rows))]
        self.assertEqual(len(texts), 2)
        self.assertIn("Please fix the parity gate on issue 954.", texts)
        self.assertNotIn("ASD-STE100", " ".join(texts))


if __name__ == "__main__":
    unittest.main()
