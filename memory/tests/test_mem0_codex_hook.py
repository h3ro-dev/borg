#!/usr/bin/env python3

import importlib.machinery
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path


SCRIPT = Path(os.path.expanduser("~/Library/Memory/mem0/bin/mem0-codex-hook"))

# FINDING A1 made recall default-closed: without a MEM0_HOOK_SCOPES grant the
# hook drops every row. Grant one deterministic scope before the module load
# captures the environment, then restore the real environment for any other
# test module in the same process.
TEST_SCOPE = "team:memory-stack"
_prior_scopes = os.environ.get("MEM0_HOOK_SCOPES")
os.environ["MEM0_HOOK_SCOPES"] = TEST_SCOPE
try:
    hook = importlib.machinery.SourceFileLoader("mem0_codex_hook_test", str(SCRIPT)).load_module()
finally:
    if _prior_scopes is None:
        del os.environ["MEM0_HOOK_SCOPES"]
    else:
        os.environ["MEM0_HOOK_SCOPES"] = _prior_scopes


class FakeMemory:
    def __init__(self, recall=None):
        self.recall = recall or {}
        self.added = []

    def search(self, query, top_k=5, filters=None, threshold=None):
        agent = (filters or {}).get("agent_id")
        if query in {row.get("memory") for row in self.added}:
            return {"results": []}
        return {"results": list(self.recall.get(agent, []))[:top_k]}

    def get_all(self, filters=None, top_k=100):
        filters = filters or {}
        rows = []
        for row in self.added:
            if all(row.get(key) == value for key, value in filters.items()):
                rows.append(row)
        return {"results": rows[:top_k]}

    def add(self, messages, user_id=None, agent_id=None, run_id=None, metadata=None, infer=None):
        row = {
            "id": f"fake-{len(self.added) + 1}",
            "memory": messages[0]["content"],
            "user_id": user_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "metadata": metadata or {},
        }
        self.added.append(row)
        return {"results": [{"event": "ADD", **row}]}


def message(role, text):
    key = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": key, "text": text}]},
    }


def tool_call(call_id="call-1", name="exec"):
    return {
        "type": "response_item",
        "payload": {"type": "custom_tool_call", "call_id": call_id, "name": name},
    }


def tool_output(text, call_id="call-1"):
    return {
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": text},
    }


def task_complete():
    return {"type": "event_msg", "payload": {"type": "task_complete"}}


class CodexHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        hook.BASE = root
        hook.DATA = root / "data"
        hook.STATE = hook.DATA / "hook-runs" / "codex"
        hook.LOG = hook.DATA / "codex-hook.log"
        hook.HOOK_OFF = root / "HOOK_OFF"
        hook._RECALL_FAST = None

    def tearDown(self):
        hook._RECALL_FAST = None
        self.temp.cleanup()

    def install_fast_recall(self, rows):
        """Stub the direct Qdrant transport, the recall path the live hook takes.

        Returns the recorded transport calls so a test can assert what the hook
        actually sent over the boundary (query, scope grant, agent filter).
        """
        calls = []

        class FakeFastRecall:
            @staticmethod
            def search(query, **kwargs):
                calls.append({"query": query, **kwargs})
                return [json.loads(json.dumps(row)) for row in rows]

        hook._RECALL_FAST = FakeFastRecall
        return calls

    def write_transcript(self, rows, name="rollout.jsonl"):
        path = Path(self.temp.name) / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def start_payload(self, prompt="Implement scoped Mem0 Codex lifecycle recall and capture"):
        return {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": os.path.expanduser("~/Library/Memory"),
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }

    def test_relevant_recall_and_irrelevant_suppression(self):
        # No memory_factory: handle_start takes the production fast path.
        # Both rows carry the granted scope, so the irrelevant row is
        # suppressed by relevance ranking, not by the scope boundary.
        calls = self.install_fast_recall(
            [
                {
                    "id": "relevant",
                    "memory": "The Mem0 lifecycle design keeps Graphiti unchanged and uses scoped Codex hook recall.",
                    "score": 0.86,
                    "agent_id": "seeder",
                    "metadata": {"source": os.path.expanduser("~/Library/Memory/mem0/README.md"), "scope": TEST_SCOPE},
                },
                {
                    "id": "irrelevant",
                    "memory": "The roofing site footer uses a blue background.",
                    "score": 0.99,
                    "agent_id": "seeder",
                    "metadata": {"source": "/tmp/roofing.md", "scope": TEST_SCOPE},
                },
            ]
        )
        payload = self.start_payload(
            "Implement Mem0 and Graphiti-safe Codex hooks under ~/Library/Memory/mem0/bin"
        )
        output = hook.handle_start(payload)
        self.assertIn("relevant", output)
        self.assertIn("source=~/Library/Memory/mem0/README.md", output)
        self.assertNotIn("roofing", output)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(hook.approx_tokens(context), hook.MAX_RECALL_TOKENS)
        # FINDING A1 contract at the transport boundary: the hook must hand the
        # exact scope grant to the fast search and must not filter by agent.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["allowed_scopes"], [TEST_SCOPE])
        self.assertIsNone(calls[0]["agent_id"])

    def test_only_irrelevant_rows_produce_no_context(self):
        memory = FakeMemory(
            {
                "seeder": [
                    {"id": "x", "memory": "A restaurant menu has three desserts.", "score": 0.99, "agent_id": "seeder"}
                ]
            }
        )
        output = hook.handle_start(self.start_payload(), memory_factory=lambda: memory)
        self.assertEqual(output, "")

    def test_sensitive_source_metadata_is_redacted(self):
        self.install_fast_recall(
            [
                {
                    "id": "safe-text",
                    "memory": "The Mem0 lifecycle design uses scoped Codex hook recall.",
                    "score": 0.91,
                    "agent_id": "seeder",
                    "metadata": {"source": "person@example.com", "scope": TEST_SCOPE},
                }
            ]
        )
        payload = self.start_payload("Implement Mem0 scoped Codex lifecycle recall")
        output = hook.handle_start(payload)
        self.assertIn("source=redacted-source", output)
        self.assertNotIn("person@example.com", output)

    def test_clean_empty_start_is_idempotent(self):
        memory = FakeMemory()
        payload = self.start_payload()
        self.assertEqual(hook.handle_start(payload, memory_factory=lambda: memory), "")
        self.assertEqual(hook.handle_start(payload, memory_factory=lambda: memory), "")
        receipts = list((hook.STATE / "start").glob("*.json"))
        self.assertEqual(len(receipts), 1)

    def test_start_fails_open_when_service_is_unavailable(self):
        def unavailable():
            raise ConnectionError("offline")

        payload = self.start_payload()
        self.assertEqual(hook.handle_start(payload, memory_factory=unavailable), "")
        self.assertFalse((hook.STATE / "start").exists())

    def test_kill_switch_disables_start_and_capture(self):
        hook.HOOK_OFF.touch()
        memory = FakeMemory()
        self.assertEqual(hook.handle_start(self.start_payload(), memory_factory=lambda: memory), "")
        transcript = self.write_transcript([message("user", "Remember this."), message("assistant", "Done."), task_complete()])
        request = {"session": "s", "turn": "t", "event_name": "Stop", "transcript_path": str(transcript), "cwd": str(transcript.parent), "assistant_completed": True}
        self.assertEqual(hook.capture_request(request, memory_factory=lambda: memory, extractor=lambda _: []), "KILL_SWITCH")
        self.assertEqual(memory.added, [])

    def test_sensitive_candidates_are_rejected_before_storage(self):
        transcript = self.write_transcript(
            [
                message("user", "Use the verified tool result."),
                tool_call(),
                tool_output("The local service check passed."),
                message("assistant", "The check is complete."),
                task_complete(),
            ]
        )
        candidates = [
            {"fact": "Contact the client at person@example.com.", "kind": "fact", "support": ["T1"]},
            {"fact": "The private amount is $4500.", "kind": "fact", "support": ["T1"]},
            {"fact": "The password is hunter2-secret-value.", "kind": "fact", "support": ["T1"]},
            {"fact": "The patient has a medical record.", "kind": "fact", "support": ["T1"]},
        ]
        memory = FakeMemory()
        request = {"session": "s", "turn": "t", "event_name": "Stop", "transcript_path": str(transcript), "cwd": str(transcript.parent), "assistant_completed": True}
        status = hook.capture_request(request, memory_factory=lambda: memory, extractor=lambda _: candidates)
        self.assertEqual(status, "PASS")
        self.assertEqual(memory.added, [])
        log = hook.LOG.read_text(encoding="utf-8")
        self.assertNotIn("example.com", log)
        self.assertNotIn("4500", log)
        self.assertNotIn("hunter2", log)

    def test_duplicate_end_capture_stores_once(self):
        transcript = self.write_transcript(
            [
                message("user", "Verify the installed Codex version."),
                tool_call(),
                tool_output("codex-cli 0.146.0 is installed."),
                message("assistant", "Verified."),
                task_complete(),
            ]
        )
        candidate = {"fact": "Codex CLI version 0.146.0 is installed.", "kind": "fact", "support": ["T1"]}
        memory = FakeMemory()
        request = {"session": "s", "turn": "t", "event_name": "Stop", "transcript_path": str(transcript), "cwd": str(transcript.parent), "assistant_completed": True}
        first = hook.capture_request(request, memory_factory=lambda: memory, extractor=lambda _: [candidate])
        second = hook.capture_request(request, memory_factory=lambda: memory, extractor=lambda _: [candidate])
        self.assertEqual(first, "PASS")
        self.assertEqual(second, "SKIP_DUP")
        self.assertEqual(len(memory.added), 1)
        self.assertEqual(memory.added[0]["agent_id"], "codex-hook")
        self.assertFalse(memory.added[0]["metadata"].get("support_text"))

    def test_concurrent_stop_and_session_end_capture_once(self):
        # Stop and SessionEnd enqueue different request files for the same
        # transcript, so only the per-digest lock serializes their workers.
        # The Stop worker is held mid-extraction while the SessionEnd worker
        # arrives; without the lock the SessionEnd worker would extract and
        # store immediately (the receipt does not exist yet).
        transcript = self.write_transcript(
            [
                message("user", "Verify the installed Codex version."),
                tool_call(),
                tool_output("codex-cli 0.146.0 is installed."),
                message("assistant", "Verified."),
                task_complete(),
            ]
        )
        candidate = {"fact": "Codex CLI version 0.146.0 is installed.", "kind": "fact", "support": ["T1"]}
        memory = FakeMemory()
        stop_in_extract = threading.Event()
        release_stop = threading.Event()
        results = {}

        def stop_extractor(records):
            stop_in_extract.set()
            release_stop.wait(timeout=30)
            return [candidate]

        def request(event_name):
            return {"session": "s", "turn": "t", "event_name": event_name, "transcript_path": str(transcript), "cwd": str(transcript.parent), "assistant_completed": True}

        def run_stop():
            results["Stop"] = hook.capture_request(request("Stop"), memory_factory=lambda: memory, extractor=stop_extractor)

        def run_session_end():
            results["SessionEnd"] = hook.capture_request(request("SessionEnd"), memory_factory=lambda: memory, extractor=lambda _: [candidate])

        stop_thread = threading.Thread(target=run_stop)
        stop_thread.start()
        self.assertTrue(stop_in_extract.wait(timeout=30))
        end_thread = threading.Thread(target=run_session_end)
        end_thread.start()
        # The SessionEnd worker must block on the digest lock, not finish.
        end_thread.join(timeout=1.0)
        self.assertTrue(end_thread.is_alive())
        release_stop.set()
        stop_thread.join(timeout=30)
        end_thread.join(timeout=30)
        self.assertEqual(results["Stop"], "PASS")
        self.assertEqual(results["SessionEnd"], "SKIP_DUP")
        self.assertEqual(len(memory.added), 1)
        self.assertTrue(hook.digest_lock_path(memory.added[0]["metadata"]["capture_digest"]).exists())

    def test_failed_first_worker_does_not_block_second_capture(self):
        # Fail-open requirement of the digest lock: a worker that dies without
        # writing a receipt releases the lock, and the waiting worker then
        # captures instead of skipping.
        transcript = self.write_transcript(
            [
                message("user", "Verify the installed Codex version."),
                tool_call(),
                tool_output("codex-cli 0.146.0 is installed."),
                message("assistant", "Verified."),
                task_complete(),
            ]
        )
        candidate = {"fact": "Codex CLI version 0.146.0 is installed.", "kind": "fact", "support": ["T1"]}
        memory = FakeMemory()
        stop_in_extract = threading.Event()
        release_stop = threading.Event()
        results = {}

        def crashing_extractor(records):
            stop_in_extract.set()
            release_stop.wait(timeout=30)
            raise ConnectionError("worker died mid-extraction")

        def request(event_name):
            return {"session": "s", "turn": "t", "event_name": event_name, "transcript_path": str(transcript), "cwd": str(transcript.parent), "assistant_completed": True}

        def run_stop():
            results["Stop"] = hook.capture_request(request("Stop"), memory_factory=lambda: memory, extractor=crashing_extractor)

        def run_session_end():
            results["SessionEnd"] = hook.capture_request(request("SessionEnd"), memory_factory=lambda: memory, extractor=lambda _: [candidate])

        stop_thread = threading.Thread(target=run_stop)
        stop_thread.start()
        self.assertTrue(stop_in_extract.wait(timeout=30))
        end_thread = threading.Thread(target=run_session_end)
        end_thread.start()
        release_stop.set()
        stop_thread.join(timeout=30)
        end_thread.join(timeout=30)
        self.assertEqual(results["Stop"], "FAIL_OPEN")
        self.assertEqual(results["SessionEnd"], "PASS")
        self.assertEqual(len(memory.added), 1)

    def test_end_capture_fails_open_when_memory_service_is_unavailable(self):
        transcript = self.write_transcript(
            [
                message("user", "Verify the installed Codex version."),
                tool_call(),
                tool_output("codex-cli 0.146.0 is installed."),
                message("assistant", "Verified."),
                task_complete(),
            ]
        )
        candidate = {"fact": "Codex CLI version 0.146.0 is installed.", "kind": "fact", "support": ["T1"]}

        def unavailable():
            raise ConnectionError("offline")

        request = {"session": "s", "turn": "t", "event_name": "Stop", "transcript_path": str(transcript), "cwd": str(transcript.parent), "assistant_completed": True}
        status = hook.capture_request(request, memory_factory=unavailable, extractor=lambda _: [candidate])
        self.assertEqual(status, "FAIL_OPEN")
        self.assertFalse((hook.STATE / "capture").exists())

    def test_unsupported_assistant_claim_is_not_capture_evidence(self):
        transcript = self.write_transcript(
            [
                message("user", "Inspect the deployment state."),
                message("assistant", "The service is deployed and live."),
                task_complete(),
            ]
        )
        evidence = hook.parse_transcript(transcript, assistant_completed=True)
        body = json.dumps(evidence.records)
        self.assertIn("Inspect the deployment state", body)
        self.assertNotIn("deployed and live", body)

        candidate = {"fact": "The service is deployed and live.", "kind": "fact", "support": ["U1"]}
        memory = FakeMemory()
        request = {"session": "s", "turn": "t", "event_name": "Stop", "transcript_path": str(transcript), "cwd": str(transcript.parent), "assistant_completed": True}
        self.assertEqual(hook.capture_request(request, memory_factory=lambda: memory, extractor=lambda _: [candidate]), "PASS")
        self.assertEqual(memory.added, [])

    def test_interrupted_task_is_not_captured(self):
        transcript = self.write_transcript(
            [
                message("user", "Start a long investigation."),
                message("assistant", "I am checking the first file now."),
                tool_call(),
                tool_output("partial output"),
            ]
        )
        memory = FakeMemory()
        request = {"session": "s", "turn": "t", "event_name": "SessionEnd", "transcript_path": str(transcript), "cwd": str(transcript.parent), "assistant_completed": False}
        status = hook.capture_request(request, memory_factory=lambda: memory, extractor=lambda _: [])
        self.assertEqual(status, "SKIP_INTERRUPTED")
        self.assertEqual(memory.added, [])

    def test_transcript_redacts_sensitive_tool_lines_before_extraction(self):
        transcript = self.write_transcript(
            [
                message("user", "Inspect the service."),
                tool_call(),
                tool_output("health=PASS\napi_key: top-secret-value-12345678901234567890\nowner@example.com"),
                message("assistant", "Inspection complete."),
                task_complete(),
            ]
        )
        evidence = hook.parse_transcript(transcript, assistant_completed=True)
        body = json.dumps(evidence.records)
        self.assertIn("health=PASS", body)
        self.assertNotIn("top-secret", body)
        self.assertNotIn("owner@example.com", body)


if __name__ == "__main__":
    unittest.main()
