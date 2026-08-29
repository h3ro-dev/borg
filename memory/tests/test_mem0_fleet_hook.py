import importlib.machinery
import json
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "mem0-fleet-hook"


class FleetHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MEM0_FLEET_BASE"] = self.tmp.name
        os.environ["MEM0_MACHINE"] = "test-studio"
        os.environ["MEM0_HARNESS"] = "codex"
        name = "fleet_hook_" + os.urandom(4).hex()
        self.mod = importlib.machinery.SourceFileLoader(name, str(SCRIPT)).load_module()

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, **extra):
        value = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "/work/alpha-project",
            "prompt": "Verify the alpha release hook evidence",
        }
        value.update(extra)
        return value

    def rows(self):
        return [
            {
                "id": "relevant-1",
                "memory": "Alpha project uses a verified release receipt before completion.",
                "score": 0.86,
                "metadata": {"project": "alpha-project", "source": "project-ledger"},
            },
            {
                "id": "irrelevant-1",
                "memory": "Garden irrigation runs each Friday.",
                "score": 0.99,
                "metadata": {"project": "garden", "source": "other-project"},
            },
        ]

    def test_relevant_recall_and_irrelevant_suppression(self):
        out = self.mod.handle_start(self.payload(), lambda _tool, _args: self.rows())
        self.assertIn("relevant-1", out)
        self.assertNotIn("irrelevant-1", out)
        self.assertIn("Mem0 candidate context only", out)

    def test_clean_start_is_idempotent(self):
        self.mod.handle_prime(self.payload(source="startup"))
        calls = []

        def caller(tool, args):
            calls.append((tool, args))
            return self.rows()

        first = self.mod.handle_start(self.payload(), caller)
        second = self.mod.handle_start(self.payload(turn_id="turn-2"), caller)
        self.assertTrue(first)
        self.assertEqual("", second)
        self.assertEqual(1, len(calls))

    def test_service_unavailable_fails_open_and_does_not_mark_start(self):
        def down(_tool, _args):
            raise OSError("offline")

        self.assertEqual("", self.mod.handle_start(self.payload(), down))
        self.assertEqual("", self.mod.handle_start(self.payload(), down))
        start_dir = self.mod.STATE / "test-studio" / "codex" / "start"
        self.assertFalse(start_dir.exists())

    def test_kill_switch_disables_start(self):
        self.mod.HOOK_OFF.parent.mkdir(parents=True, exist_ok=True)
        self.mod.HOOK_OFF.touch()
        calls = []
        out = self.mod.handle_start(self.payload(), lambda *args: calls.append(args))
        self.assertEqual("", out)
        self.assertEqual([], calls)

    def test_secret_and_pii_lines_are_redacted(self):
        text = "api_key: sk-test-secret\nname@example.com\n303-555-1212\nSafe tool fact"
        clean = self.mod.redact_text(text)
        self.assertNotIn("sk-test-secret", clean)
        self.assertNotIn("name@example.com", clean)
        self.assertNotIn("303-555-1212", clean)
        self.assertIn("Safe tool fact", clean)
        self.assertEqual(3, clean.count("[REDACTED_SENSITIVE_LINE]"))

    def write_codex_transcript(self, completed=True):
        path = Path(self.tmp.name) / "transcript.jsonl"
        rows = [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Use the alpha release receipt."}]}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1", "name": "verify_release"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "release receipt status PASS"}},
        ]
        if completed:
            rows.extend([
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}},
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ])
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def test_interrupted_task_is_not_capture_ready(self):
        evidence = self.mod.parse_transcript(self.write_codex_transcript(False), "codex")
        self.assertFalse(evidence.completed)
        self.assertTrue(evidence.records)

    def test_completed_transcript_contains_user_and_tool_only(self):
        evidence = self.mod.parse_transcript(self.write_codex_transcript(True), "codex")
        self.assertTrue(evidence.completed)
        self.assertEqual({"user", "tool"}, {row["kind"] for row in evidence.records})
        self.assertNotIn("Done.", json.dumps(evidence.records))

    def test_duplicate_end_capture_calls_server_once(self):
        records = [{"ref": "U1", "kind": "user", "text": "Keep the release receipt rule."}]
        digest = self.mod.sha256_text(json.dumps(records, sort_keys=True, ensure_ascii=True))
        request = {
            "records_json": json.dumps(records),
            "session": "session-1",
            "cwd": "/work/alpha-project",
            "machine": "test-studio",
            "harness": "codex",
            "digest": digest,
            "completed": True,
        }
        calls = []

        def caller(tool, args):
            calls.append((tool, args))
            return {"status": "PASS", "stored": 1}

        self.assertEqual("PASS", self.mod.process_capture_request(request, caller))
        self.assertEqual("SKIP_DUP", self.mod.process_capture_request(request, caller))
        self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
