"""Focused resilience regressions for BORG connector internals."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import sys
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import borg_context_server as server
from computer_tools import BoundaryMiddleware, NativeComputer
from operation_receipts import OperationLedger
from job_tools import JobStore
from native_ui import NativeUI
from service_handles import CredentialStore


class ReceiptTests(unittest.TestCase):
    def test_interrupted_receipt_recovers_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = OperationLedger(root)
            row = first.start("computer_edit_block", "a" * 64)
            row["pid"] = 99999999
            first._write(row)
            second = OperationLedger(root)
            recovered = second.get(row["receipt_id"])
            self.assertEqual(recovered["state"], "outcome_unknown")
            self.assertEqual(recovered["failure_code"], "process_interrupted")

    def test_final_receipt_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = OperationLedger(root)
            row = first.start("desktop_click", "b" * 64)
            first.finish(row["receipt_id"], "succeeded")
            second = OperationLedger(root)
            self.assertEqual(second.get(row["receipt_id"])["state"], "succeeded")


class StatusIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_survives_memory_outage(self):
        settings = SimpleNamespace(project_roots=(Path("/example/projects"),), computer={}, desktop={})
        context = server.BorgContext(settings)

        @asynccontextmanager
        async def broken_upstream():
            raise server.ToolError("synthetic memory outage")
            yield

        context.upstream = broken_upstream
        with patch.object(server, "authorize", return_value=None):
            result = await context.borg_status()
        self.assertEqual(result["mem0"]["status"], "UNAVAILABLE")
        self.assertEqual(result["graph"]["status"], "UNKNOWN")
        self.assertIn("observed_at", result)


class LaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_receipt_write_does_not_freeze_unrelated_requests(self):
        entered = threading.Event()
        release = threading.Event()

        class Ledger:
            def start(self, *_):
                entered.set()
                release.wait(2)
                return {"receipt_id": "test"}

            def finish(self, *_):
                pass

        middleware = BoundaryMiddleware(lambda: None, SimpleNamespace(
            inbound_authorization_file=Path("/nonexistent"), mem0_token_file=Path("/nonexistent"),
            computer={}, desktop={}), ledger=Ledger())
        completed = asyncio.Event()
        result = SimpleNamespace(is_error=False, meta=None, model_dump=lambda: {"output": "ok"})

        async def call_next(_):
            completed.set()
            return result

        context = SimpleNamespace(message=SimpleNamespace(name="computer_write_file", arguments={}))
        task = asyncio.create_task(middleware.on_call_tool(context, call_next))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            async with asyncio.timeout(0.3):
                await asyncio.sleep(0)
                self.assertFalse(completed.is_set(), "side effect ran before its durable starting receipt")
                listed = await middleware.on_list_tools(None, lambda _: asyncio.sleep(0, result=[]))
                self.assertEqual(listed, [])
            self.assertFalse(release.is_set())
        finally:
            release.set()
            await task
        self.assertTrue(completed.is_set())

    async def test_desktop_lane_does_not_block_computer_lane(self):
        middleware = BoundaryMiddleware(lambda: None, SimpleNamespace(
            inbound_authorization_file=Path("/nonexistent"), mem0_token_file=Path("/nonexistent"),
            computer={}, desktop={}), ledger=None)
        desktop_entered = asyncio.Event()
        release = asyncio.Event()

        async def desktop():
            async with middleware.lane("desktop"):
                desktop_entered.set()
                await release.wait()

        task = asyncio.create_task(desktop())
        await desktop_entered.wait()
        async with asyncio.timeout(0.2):
            async with middleware.lane("computer"):
                pass
        release.set()
        await task

    async def test_lane_queue_is_bounded(self):
        middleware = BoundaryMiddleware(lambda: None, SimpleNamespace(
            inbound_authorization_file=Path("/nonexistent"), mem0_token_file=Path("/nonexistent"),
            computer={}, desktop={}), ledger=None)
        middleware.lane_queue_limit = 1
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with middleware.lane("desktop"):
                entered.set()
                await release.wait()

        first = asyncio.create_task(holder())
        await entered.wait()
        async def queued_waiter():
            async with middleware.lane("desktop"):
                await release.wait()
        waiter = asyncio.create_task(queued_waiter())
        await asyncio.sleep(0)
        with self.assertRaisesRegex(server.ToolError, "BORG_BUSY"):
            async with middleware.lane("desktop"):
                pass
        release.set()
        await first
        await waiter


class NativeComputerTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_process_session_survives_separate_calls(self):
        computer = NativeComputer()
        result = computer.start_process(
            "/usr/bin/python3 -u -c 'print(\"BORG_NATIVE_OK\", flush=True)'",
            timeout_ms=100,
        )
        output = computer.read_process_output(result["pid"], timeout_ms=1000)
        self.assertIn("BORG_NATIVE_OK", output["output"])
        computer.force_terminate(result["pid"])

    async def test_native_edit_requires_one_exact_block(self):
        computer = NativeComputer()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one.txt"
            computer.write_file(str(path), "alpha\n")
            edited = computer.edit_block(str(path), "alpha", "beta")
            self.assertEqual(edited["replacements"], 1)
            self.assertEqual(path.read_text(), "beta\n")


class DurableJobTests(unittest.TestCase):
    def test_job_output_and_artifact_survive_store_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = JobStore(root)
            second = reopened = None
            try:
                command = shlex.join([sys.executable, "-u", "-c", 'print("BORG_JOB_OK", flush=True)'])
                row = first.start(command, timeout_ms=50)
                import time
                deadline = time.monotonic() + 5
                status = first.status(row["job_id"])
                while status["state"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.02)
                    status = first.status(row["job_id"])
                self.assertEqual(status["state"], "succeeded")
                self.assertEqual(status["returncode"], 0)
                second = JobStore(root)
                status = second.status(row["job_id"])
                self.assertEqual(status["state"], "succeeded")
                self.assertEqual(second.read_output(row["job_id"], stream="stdout")["output"], "BORG_JOB_OK\n")
                self.assertEqual(second.read_output(row["job_id"], stream="stderr")["output"], "")
                source = root / "source.txt"
                source.write_text("artifact")
                artifact = second.put_artifact(str(source))
                reopened = JobStore(root)
                chunk = reopened.read_artifact(artifact["artifact_id"])
                self.assertEqual(base64_decode(chunk["content_base64"]), b"artifact")
                self.assertEqual(artifact["sha256"], hashlib.sha256(b"artifact").hexdigest())
            finally:
                first.close_all()
                if second is not None:
                    second.close_all()
                if reopened is not None:
                    reopened.close_all()


def base64_decode(value):
    import base64
    return base64.b64decode(value)


class NativeCapabilityTests(unittest.TestCase):
    def test_ui_permission_contract_is_native(self):
        result = NativeUI().permissions()
        self.assertEqual(result["backend"], "native_os")
        self.assertTrue(result["screencapture"])

    def test_service_handles_are_value_blind_and_require_native_login(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "credentials.json"
            registry.write_text(json.dumps({"services": [{
                "name": "example", "provider": "Example", "status": "reauth_required",
                "login_url": "https://example.test/login", "scopes": ["read"]
            }]}))
            registry.chmod(0o600)
            store = CredentialStore({"registry": str(registry)})
            status = store.status("example")
            self.assertEqual(status["status"], "reauth_required")
            self.assertFalse(status["credential_values_returned"])
            handoff = store.handoff("example")
            self.assertEqual(handoff["state"], "human_action_required")
            self.assertEqual(handoff["recommended_next_tool"], "browser_start_session")
            self.assertFalse(handoff["credential_values_returned"])


class MiddlewareReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_finalization_preserves_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OperationLedger(Path(directory))
            settings = SimpleNamespace(
                inbound_authorization_file=Path("/nonexistent-inbound"),
                mem0_token_file=Path("/nonexistent-upstream"), computer={}, desktop={})
            middleware = BoundaryMiddleware(lambda: None, settings, ledger=ledger)
            context = SimpleNamespace(message=SimpleNamespace(
                name="computer_write_file", arguments={}))
            entered = threading.Event()
            cancelled_finalizer = threading.Event()
            release = threading.Event()
            writes = []
            original_write, original_finish = ledger._write, ledger.finish

            def blocked_write(row):
                writes.append(row["state"])
                if row["state"] == "succeeded":
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("test finalizer was not released")
                original_write(row)

            def finish(receipt_id, state, failure_code=None):
                if state == "outcome_unknown":
                    cancelled_finalizer.set()
                return original_finish(receipt_id, state, failure_code)

            result = SimpleNamespace(is_error=False, meta=None, model_dump=lambda: {"output": "ok"})
            with patch.object(ledger, "_write", side_effect=blocked_write), patch.object(ledger, "finish", side_effect=finish):
                task = asyncio.create_task(middleware.on_call_tool(
                    context, lambda _: asyncio.sleep(0, result=result)))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    self.assertTrue(await asyncio.to_thread(cancelled_finalizer.wait, 1))
                    async with asyncio.timeout(0.3):
                        self.assertEqual(await middleware.on_list_tools(
                            None, lambda _: asyncio.sleep(0, result=[])), [])
                    self.assertEqual(writes, ["running", "succeeded"])
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            receipt = ledger.recent(1)[0]
            self.assertEqual(receipt["state"], "succeeded")
            self.assertIsNone(receipt["failure_code"])
            self.assertEqual(writes, ["running", "succeeded"])
            self.assertEqual(ledger._locks, {})

    async def test_cancelled_mutation_becomes_outcome_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OperationLedger(Path(directory))
            settings = SimpleNamespace(
                inbound_authorization_file=Path("/nonexistent-inbound"),
                mem0_token_file=Path("/nonexistent-upstream"), computer={}, desktop={})
            middleware = BoundaryMiddleware(lambda: None, settings, ledger=ledger)
            context = SimpleNamespace(message=SimpleNamespace(
                name="computer_start_process", arguments={"command": "synthetic"}))
            entered = asyncio.Event()

            async def call_next(_):
                entered.set()
                await asyncio.sleep(60)

            task = asyncio.create_task(middleware.on_call_tool(context, call_next))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            receipt = ledger.recent(1)[0]
            self.assertEqual(receipt["state"], "outcome_unknown")
            self.assertEqual(receipt["failure_code"], "request_cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
