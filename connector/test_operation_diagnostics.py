"""Effect-safety regressions with disposable files and synthetic fleet peers."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import CallToolResult, TextContent
from computer_tools import BoundaryMiddleware, classify_failure
from fleet_tools import Fleet, FleetPreflightError
from operation_diagnostics import for_call, sanitize
from operation_receipts import OperationLedger


class DiagnosticTests(unittest.TestCase):
    def test_closed_schema_drops_payloads_and_recomputes_retryability(self):
        source = {"phase": "dispatch", "cause": "permission_denied", "effect_state": "outcome_unknown",
                  "retryable": True, "error_text": "PRIVATE_ERROR", "arguments": {"command": "PRIVATE_COMMAND"},
                  "target": {"host": "alpha", "tool": "computer_write_file", "home": "PRIVATE_HOME",
                             "instance_id": str(uuid.uuid4()), "receipt_id": "PRIVATE_RECEIPT"}}
        result = sanitize(source)
        self.assertFalse(result["retryable"])
        self.assertFalse(result["retry_without_change"])
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertEqual(result["target"]["host"], "alpha")
        self.assertNotIn("home", result["target"])

    def test_invalid_field_types_fail_closed(self):
        result = sanitize({"phase": [], "effect_state": {}, "cause": [], "target": "bad"})
        self.assertEqual(result["effect_state"], "outcome_unknown")
        self.assertFalse(result["retryable"])
        self.assertNotIn("target", result)

    def test_only_known_predispatch_transient_failures_are_retryable(self):
        for code in ["busy", "timeout", "provider_unavailable", "rate_limited"]:
            self.assertTrue(sanitize({"effect_state": "not_started", "cause": code})["retryable"])
            self.assertFalse(sanitize({"effect_state": "outcome_unknown", "cause": code})["retryable"])
        for code in ["identity_mismatch", "bad_request", "permission_denied", "downstream_error", None]:
            self.assertFalse(sanitize({"effect_state": "not_started", "cause": code})["retryable"])

    def test_exception_type_classifies_empty_timeout_and_connection_errors(self):
        self.assertEqual(classify_failure(TimeoutError()), "timeout")
        self.assertEqual(classify_failure(ConnectionRefusedError()), "provider_unavailable")
        self.assertEqual(classify_failure(PermissionError()), "permission_denied")

    def test_nonfleet_calls_do_not_capture_arbitrary_target_arguments(self):
        result = for_call("computer_start_process", {"host": "alpha", "tool": "invented", "command": "PRIVATE_COMMAND"},
                          phase="dispatch", effect_state="outcome_unknown")
        self.assertNotIn("target", result)
        self.assertNotIn("PRIVATE_COMMAND", json.dumps(result))

    def test_legacy_receipt_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OperationLedger(Path(directory))
            row = ledger.start("computer_write_file", "a" * 64)
            ledger.finish(row["receipt_id"], "succeeded")
            old = ledger.get(row["receipt_id"])
            old["version"] = 1
            old.pop("diagnostics")
            ledger._write(old)
            reopened = OperationLedger(Path(directory))
            self.assertEqual(reopened.get(row["receipt_id"]), old)

    def test_restart_preserves_target_and_forces_unknown_retry_unsafe(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OperationLedger(Path(directory))
            detail = for_call("fleet_call", {"host": "alpha", "tool": "computer_write_file"},
                              phase="admission", effect_state="not_started", cause="busy")
            row = ledger.start("fleet_call", "a" * 64, detail)
            row["pid"] = 99999999
            ledger._write(row)
            reopened = OperationLedger(Path(directory))
            saved = reopened.get(row["receipt_id"])
            self.assertEqual(saved["state"], "outcome_unknown")
            self.assertEqual(saved["diagnostics"]["target"]["host"], "alpha")
            self.assertEqual(saved["diagnostics"]["cause"], "process_interrupted")
            self.assertFalse(saved["diagnostics"]["retryable"])

    def test_unknown_terminal_state_overrides_safe_retry_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OperationLedger(Path(directory))
            row = ledger.start("fleet_call", "a" * 64)
            result = ledger.finish(row["receipt_id"], "outcome_unknown", "timeout",
                                   {"effect_state": "not_started", "cause": "timeout", "retryable": True})
            self.assertFalse(result["diagnostics"]["retryable"])
            self.assertEqual(result["diagnostics"]["effect_state"], "outcome_unknown")

    def test_terminal_success_cannot_be_replaced_by_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OperationLedger(Path(directory))
            row = ledger.start("fleet_call", "a" * 64)
            success = ledger.finish(row["receipt_id"], "succeeded")
            after = ledger.finish(row["receipt_id"], "outcome_unknown", "request_cancelled")
            self.assertEqual(after, success)


class DiagnosticFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.ledger = OperationLedger(self.root / "operations")
        self.middleware = BoundaryMiddleware(lambda: None, SimpleNamespace(
            inbound_authorization_file=self.root / "absent-inbound", mem0_token_file=self.root / "absent-upstream",
            computer={}), self.ledger)
        self.context = SimpleNamespace(message=SimpleNamespace(name="computer_write_file", arguments={}))


class MiddlewareTests(DiagnosticFixture):
    async def test_exception_after_effect_is_not_safe_to_repeat(self):
        target = self.root / "effect"
        async def action(_):
            target.write_text("already changed")
            raise ToolError("permission denied after first stage")
        with self.assertRaises(ToolError):
            await self.middleware.on_call_tool(self.context, action)
        self.assertEqual(target.read_text(), "already changed")
        saved = self.ledger.recent(1)[0]
        self.assertEqual(saved["state"], "outcome_unknown")
        self.assertEqual(saved["diagnostics"]["cause"], "permission_denied")
        self.assertFalse(saved["diagnostics"]["retryable"])

    async def test_returned_error_after_dispatch_is_also_unknown(self):
        result = ToolResult(content="permission denied", is_error=True)
        returned = await self.middleware.on_call_tool(self.context, lambda _: asyncio.sleep(0, result=result))
        saved = self.ledger.get(returned.meta["borg_operation_receipt"])
        self.assertEqual(saved["state"], "outcome_unknown")
        self.assertEqual(returned.meta["borg_failure"]["effect_state"], "outcome_unknown")
        self.assertFalse(returned.meta["borg_failure"]["retryable"])

    async def test_admission_failure_has_no_effect_and_no_call(self):
        action = AsyncMock()
        with patch.object(self.middleware.scheduler, "run", AsyncMock(side_effect=ToolError("BORG_BUSY: queue is full"))):
            with self.assertRaises(ToolError):
                await self.middleware.on_call_tool(self.context, action)
        action.assert_not_called()
        saved = self.ledger.recent(1)[0]
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["diagnostics"]["effect_state"], "not_started")
        self.assertTrue(saved["diagnostics"]["retryable"])

    async def test_cancellation_during_starting_receipt_is_finalized_without_execution(self):
        entered, release = threading.Event(), threading.Event()
        original = self.ledger.start
        def blocked(*args):
            row = original(*args)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture not released")
            return row
        action = AsyncMock()
        with patch.object(self.ledger, "start", side_effect=blocked):
            pending = asyncio.create_task(self.middleware.on_call_tool(self.context, action))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                pending.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
        action.assert_not_called()
        saved = self.ledger.recent(1)[0]
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["diagnostics"]["effect_state"], "not_started")

    async def test_local_fleet_preflight_refusal_is_distinct_from_remote_error_words(self):
        self.context.message.name = "fleet_call"
        self.context.message.arguments = {"host": "alpha", "tool": "computer_write_file", "arguments": {}}
        action = AsyncMock(side_effect=FleetPreflightError("identity_mismatch"))
        with self.assertRaises(ToolError):
            await self.middleware.on_call_tool(self.context, action)
        saved = self.ledger.recent(1)[0]
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["diagnostics"]["cause"], "identity_mismatch")
        self.assertEqual(saved["diagnostics"]["target"], {"host": "alpha", "tool": "computer_write_file"})
        self.assertFalse(saved["diagnostics"]["retryable"])


class FleetDiagnosticTests(DiagnosticFixture):
    async def setup_fleet(self, mode):
        identity = {"instance_id": str(uuid.uuid4()), "server_generation": str(uuid.uuid4()),
                    "owner": "alpha", "home": str(self.root)}
        row = {"id": "alpha", "ssh_alias": "alpha", "enabled": True,
               **{k: identity[k] for k in ("instance_id", "owner", "home")}}
        registry = self.root / "registry.json"
        registry.write_text(json.dumps({"schema": "borg-fleet/v1", "hosts": [row]}))
        registry.chmod(0o600)
        calls = []
        target_receipt = str(uuid.uuid4())
        class Peer:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def close(self): pass
            async def call_tool(self, *args, **kwargs):
                if mode == "preflight_timeout": raise TimeoutError()
                if mode == "unreachable": raise ConnectionRefusedError()
                actual = dict(identity)
                if mode == "identity_mismatch": actual["instance_id"] = str(uuid.uuid4())
                return SimpleNamespace(structured_content=actual)
            async def list_tools(self, **kwargs):
                return [SimpleNamespace(name="computer_write_file", model_dump=lambda **kw: {"name": "computer_write_file"})]
            async def call_tool_mcp(self, *args, **kwargs):
                calls.append(args)
                if mode == "lost_response": raise TimeoutError()
                return CallToolResult(content=[TextContent(type="text", text="permission denied" if mode == "target_error" else "ok")],
                                      isError=mode == "target_error", _meta={"borg_operation_receipt": target_receipt})
        fleet = Fleet(registry, lambda _: Peer())
        self.addAsyncCleanup(fleet.close)
        self.context.message.name = "fleet_call"
        self.context.message.arguments = {"host": "alpha", "tool": "computer_write_file", "arguments": {}}
        async def action(_):
            return await fleet.fleet_call("alpha", "computer_write_file", {})
        return action, calls, identity, target_receipt

    async def test_timeout_before_dispatch_has_typed_durable_target(self):
        action, calls, _, _ = await self.setup_fleet("preflight_timeout")
        result = await self.middleware.on_call_tool(self.context, action)
        saved = self.ledger.get(result.meta["borg_operation_receipt"])
        self.assertEqual(calls, [])
        self.assertEqual(saved["failure_code"], "timeout")
        self.assertEqual(saved["diagnostics"]["effect_state"], "not_started")
        self.assertTrue(saved["diagnostics"]["retryable"])
        self.assertEqual(saved["diagnostics"]["target"]["host"], "alpha")

    async def test_lost_response_records_unknown_and_never_replays(self):
        action, calls, identity, _ = await self.setup_fleet("lost_response")
        result = await self.middleware.on_call_tool(self.context, action)
        saved = self.ledger.get(result.meta["borg_operation_receipt"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(saved["failure_code"], "timeout")
        self.assertEqual(saved["diagnostics"]["phase"], "dispatch")
        self.assertEqual(saved["state"], "outcome_unknown")
        self.assertFalse(saved["diagnostics"]["retryable"])
        self.assertEqual(saved["diagnostics"]["target"]["instance_id"], identity["instance_id"])

    async def test_identity_mismatch_cannot_dispatch_or_suggest_blind_retry(self):
        action, calls, _, _ = await self.setup_fleet("identity_mismatch")
        result = await self.middleware.on_call_tool(self.context, action)
        saved = self.ledger.get(result.meta["borg_operation_receipt"])
        self.assertEqual(calls, [])
        self.assertEqual(saved["failure_code"], "identity_mismatch")
        self.assertEqual(saved["diagnostics"]["effect_state"], "not_started")
        self.assertFalse(saved["diagnostics"]["retryable"])

    async def test_native_target_receipt_is_preserved_without_home_or_arguments(self):
        action, calls, identity, target_receipt = await self.setup_fleet("success")
        result = await self.middleware.on_call_tool(self.context, action)
        saved = self.ledger.get(result.meta["borg_operation_receipt"])
        self.assertEqual(saved["state"], "succeeded")
        self.assertEqual(saved["diagnostics"]["target"]["receipt_id"], target_receipt)
        self.assertEqual(saved["diagnostics"]["target"]["server_generation"], identity["server_generation"])
        self.assertNotIn(str(self.root), json.dumps(saved))

    async def test_target_permission_error_is_not_mistaken_for_preflight_refusal(self):
        action, calls, _, _ = await self.setup_fleet("target_error")
        result = await self.middleware.on_call_tool(self.context, action)
        saved = self.ledger.get(result.meta["borg_operation_receipt"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(saved["state"], "outcome_unknown")
        self.assertEqual(saved["failure_code"], "permission_denied")
        self.assertFalse(saved["diagnostics"]["retryable"])


if __name__ == "__main__":
    unittest.main()
