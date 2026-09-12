"""Offline tests for the existing-Inbox reference bridge. Synthetic fixtures only."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("inbox_bridge", ROOT / "integrations/inbox_context.py")
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


def page(more=False):
    return {"complete": not more, "has_more": more,
            "next_cursor": "example-cursor" if more else None,
            "returned": 1, "limit": 5,
            "coverage": {"eligible_total": None}}


class NativeFixture:
    def __init__(self):
        self.calls = []
        self.rows = {
            "assignments.list": {"assignments": [{"work_id": "example-task", "scope": "/example/project",
                "assignee": "example-agent", "version": 3, "summary": "NOT_FOR_SUMMARY"}], "page": page()},
            "messages.list": {"messages": [{"id": "example-message", "work_id": "example-task",
                "scope": "/example/project", "kind": "information", "body": "NOT_FOR_SUMMARY",                "deliveries": [{"recipient": "example-agent", "state": "resolved"}]}], "page": page()}}

    def call_sync(self, operation, params):
        self.calls.append((operation, copy.deepcopy(params)))
        result = self.rows[operation]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


class InboxBridgeTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeFixture()
        self.reader = BRIDGE.InboxContextReader(self.native)

    def snapshot(self):
        return self.reader.snapshot(work_id="example-task", scope="/example/project")

    def read(self, **kwargs):
        return self.reader.read("messages", work_id="example-task", scope="/example/project", **kwargs)

    def test_native_assignment_and_message_reads(self):
        result = self.snapshot()
        self.assertEqual(result["status"], "OBSERVED")
        self.assertEqual([operation for operation, _ in self.native.calls], ["assignments.list", "messages.list"])

    def test_metadata_only(self):
        self.assertNotIn("NOT_FOR_SUMMARY", json.dumps(self.snapshot()))
    def test_resolved_delivery_does_not_assert_work_acceptance(self):
        result = self.snapshot()
        self.assertEqual(result["work_acceptance"], "not_asserted")
        self.assertEqual(result["messages"]["items"][0]["work_acceptance"], "not_asserted")
        self.assertFalse(result["memory_write_performed"])

    def test_current_owner_version_preserved(self):
        assignment = self.snapshot()["assignments"]["items"][0]
        self.assertEqual((assignment["assignee"], assignment["version"]), ("example-agent", 3))

    def test_partial_page_preserved(self):
        self.native.rows["messages.list"]["page"] = page(True)
        result = self.read(cursor="earlier-example-cursor")
        self.assertFalse(result["complete"])
        self.assertEqual(result["page"]["next_cursor"], "example-cursor")
        self.assertEqual(self.native.calls[0][1]["cursor"], "earlier-example-cursor")
        self.assertIsNone(result["page"]["coverage"]["eligible_total"])

    def test_unavailable_is_not_empty(self):
        self.native.rows["messages.list"] = TimeoutError("example transport failure")
        result = self.read()
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertIsNone(result["items"])
        self.assertFalse(result["retry_performed"])
        self.assertEqual(len(self.native.calls), 1)

    def test_one_failed_source_gives_partial_status(self):
        self.native.rows["messages.list"] = OSError()
        result = self.snapshot()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(result["assignments"]["items"]), 1)

    def test_no_polling_operation(self):
        with self.assertRaises(BRIDGE.ContractError):
            self.reader.read("messages.poll", work_id="example-task", scope="/example/project")
        self.assertEqual(self.native.calls, [])

    def test_cross_work_metadata_rejected(self):
        self.native.rows["messages.list"]["messages"][0]["work_id"] = "another-example"
        self.assertEqual(self.read()["status"], "INVALID_RESPONSE")

    def test_cross_scope_metadata_rejected(self):
        self.native.rows["messages.list"]["messages"][0]["scope"] = "/example/project-other"
        self.assertEqual(self.read()["status"], "INVALID_RESPONSE")

    def test_missing_pagination_is_not_complete(self):
        self.native.rows["messages.list"]["page"] = {}
        self.assertEqual(self.read()["status"], "INVALID_RESPONSE")

    def test_invalid_limit_stops_before_call(self):
        for value in (True, 0, 21, 2.5):
            with self.assertRaises(BRIDGE.ContractError):
                self.read(limit=value)
        self.assertEqual(self.native.calls, [])


if __name__ == "__main__":
    unittest.main()
