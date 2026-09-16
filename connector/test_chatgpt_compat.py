"""Exercise the cached ChatGPT tool arguments at the real MCP boundary."""
import asyncio
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastmcp import Client, FastMCP
from computer_tools import BoundaryMiddleware, DESCRIPTIONS, MAX_FILE_BYTES, MAX_OUTPUT_BYTES, NativeComputer
from operation_receipts import OperationLedger


class ChatGPTCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.tmp.name)
        self.native = NativeComputer()
        self.authorized = True
        self.authorizations = 0
        def authorize():
            self.authorizations += 1
            if not self.authorized:
                raise PermissionError("Authentication required")
        self.server = FastMCP("cached-chatgpt-contract")
        settings = SimpleNamespace(computer={}, inbound_authorization_file=self.root / "absent",
                                   mem0_token_file=self.root / "also-absent")
        self.ledger = OperationLedger(self.root / "operations")
        self.server.add_middleware(BoundaryMiddleware(authorize, settings, self.ledger))
        for name in DESCRIPTIONS:
            self.server.tool(name="computer_" + name)(getattr(self.native, name))
        self.client = Client(self.server)
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)
        for row in self.native.list_sessions()["sessions"]:
            self.native.force_terminate(row["pid"])
        self.tmp.cleanup()

    async def call(self, name, **arguments):
        result = await self.client.call_tool("computer_" + name, arguments)
        return result.structured_content

    async def test_cached_write_edit_and_read_arguments(self):
        path = str(self.root / "file.txt")
        await self.call("write_file", path=path, content="alpha alpha", mode="rewrite", origin="llm")
        await self.call("edit_block", file_path=path, old_string="alpha", new_string="beta",
                        expected_replacements=2, origin="llm", options={})
        await self.call("write_file", path=path, content=" gamma", mode="append", origin="llm")
        read = await self.call("read_file", path=path, isUrl=False, origin="llm", options={})
        self.assertEqual(read["content"], "beta beta gamma")
        listing = await self.call("list_directory", path=str(self.root), depth=1, origin="llm")
        self.assertIn(path, [row["path"] for row in listing["entries"]])

    async def test_cached_shell_output_paging_and_replay(self):
        path = self.root / "executed.txt"
        command = shlex.join([sys.executable, "-c",
            f"from pathlib import Path; Path({str(path)!r}).write_text('executed'); print('abcdefghij', end='')"])
        row = await self.call("start_process", command=command, timeout_ms=1000,
                              shell="/bin/sh", origin="llm", verbose_timing=True)
        self.assertIn("elapsed_ms", row)
        await asyncio.to_thread(self.native._processes[row["pid"]].wait, 5)
        first = await self.call("read_process_output", pid=row["pid"], timeout_ms=1000,
                               offset=0, length=4, verbose_timing=True)
        self.assertEqual(first["output"], "abcd")
        replay = await self.call("read_process_output", pid=row["pid"], offset=0, length=4)
        self.assertEqual(replay["output"], "abcd")
        rest = await self.call("read_process_output", pid=row["pid"], length=1000)
        self.assertEqual(rest["output"], "efghij")
        self.assertEqual(rest["next_offset"], 10)
        self.assertEqual(rest["returncode"], 0)
        self.assertFalse(rest["running"])
        proc = self.native._processes[row["pid"]]
        self.assertTrue(all(stream.closed for stream in (proc.stdin, proc.stdout, proc.stderr)))
        self.assertEqual(path.read_text(), "executed")

    async def test_origin_metadata_never_grants_authority(self):
        self.authorized = False
        path = self.root / "not-written"
        for origin in ("llm", "ui"):
            result = await self.client.call_tool("computer_write_file",
                {"path": str(path), "content": "no", "origin": origin}, raise_on_error=False)
            self.assertTrue(result.is_error)
        self.assertFalse(path.exists())
        self.assertGreaterEqual(self.authorizations, 2)

    async def test_unsupported_document_edit_and_shell_do_not_mutate(self):
        path = self.root / "preserved.txt"
        path.write_text("before")
        result = await self.client.call_tool("computer_edit_block", {"file_path": str(path),
            "old_string": "before", "new_string": "after", "range": "A1"}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertEqual(path.read_text(), "before")
        result = await self.client.call_tool("computer_start_process", {
            "command": "printf must-not-run", "shell": "/missing/shell", "timeout_ms": 0,
            "origin": "llm"}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertEqual(self.native.list_sessions()["sessions"], [])


class OutputRetentionTests(unittest.TestCase):
    def test_bounded_history_preserves_forward_reads(self):
        native = NativeComputer()
        count = MAX_FILE_BYTES + MAX_OUTPUT_BYTES
        command = shlex.join([sys.executable, "-c", f"import sys; sys.stdout.write('x'*{count})"])
        row = native.start_process(command, timeout_ms=0, shell="/bin/sh")
        total = 0
        try:
            while total < count:
                output = native.read_process_output(row["pid"], timeout_ms=5000)
                total += output["bytes"]
                self.assertEqual(output["output"], "x" * output["bytes"])
                self.assertLessEqual(len(native._process_output[row["pid"]]["data"]), MAX_FILE_BYTES)
            self.assertEqual(total, count)
            with self.assertRaisesRegex(Exception, "outside retained output"):
                native.read_process_output(row["pid"], offset=0)
        finally:
            native.force_terminate(row["pid"])


if __name__ == "__main__":
    unittest.main()
