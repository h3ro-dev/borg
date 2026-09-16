"""Regressions for independent clients, shared resources and blocked pipes."""
import asyncio
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.exceptions import ToolError
from computer_tools import NativeComputer, MAX_OUTPUT_BYTES
from concurrency import CallScheduler, resource_keys


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ["one", "two", *map(str, range(64))]:
            (self.root / name).write_text("original")

    def tearDown(self):
        self.tmp.cleanup()

    async def test_64_independent_callers_overlap(self):
        scheduler = CallScheduler()
        entered = 0
        ready, release = asyncio.Event(), asyncio.Event()
        async def call():
            nonlocal entered
            entered += 1
            if entered == 64:
                ready.set()
            await release.wait()
            return "ok"
        tasks = [asyncio.create_task(scheduler.run("computer_read_file", {"path": str(self.root / str(i))}, call)) for i in range(64)]
        try:
            async with asyncio.timeout(3):
                await ready.wait()
            self.assertEqual(scheduler.status()["active"], 64)
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(results, ["ok"] * 64)
        self.assertEqual(scheduler.pending, 0)
        self.assertEqual(scheduler._locks, {})

    async def test_cancelled_running_call_keeps_its_resource_until_done(self):
        scheduler = CallScheduler()
        entered, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def held():
            entered.set()
            await release.wait()
        first = asyncio.create_task(scheduler.run("computer_write_file", {"path": str(self.root / "one")}, held))
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        async def other():
            second.set()
        waiting = asyncio.create_task(scheduler.run("computer_edit_block", {"file_path": str(self.root / "one")}, other))
        try:
            async with asyncio.timeout(0.5):
                await scheduler.run("computer_read_file", {"path": str(self.root / "two")}, lambda: asyncio.sleep(0))
            self.assertFalse(second.is_set())
        finally:
            release.set()
            await waiting
        self.assertTrue(second.is_set())
        self.assertEqual(scheduler._locks, {})

    async def test_cancelled_queue_entry_never_executes(self):
        scheduler = CallScheduler()
        called = False
        async def call():
            nonlocal called
            called = True
        async with scheduler.slot(("process:1",)):
            queued = asyncio.create_task(scheduler.run("computer_interact_with_process", {"pid": 1}, call))
            for _ in range(100):
                if scheduler.pending == 2:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(scheduler.pending, 2)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            await asyncio.sleep(0)
        self.assertFalse(called)
        self.assertEqual(scheduler.pending, 0)
        self.assertEqual(scheduler._locks, {})

    async def test_deadline_releases_all_resources_without_running(self):
        scheduler = CallScheduler({"wait_seconds": 0.02})
        async with scheduler.slot(("file:b",)):
            with self.assertRaisesRegex(ToolError, "BORG_BUSY"):
                async with scheduler.slot(("file:a", "file:b")):
                    self.fail("blocked operation executed")
            self.assertNotIn("file:a", scheduler._locks)
        self.assertEqual(scheduler.pending, 0)
        self.assertEqual(scheduler.rejected, 1)

    async def test_same_process_serializes_but_other_process_does_not(self):
        scheduler = CallScheduler({"wait_seconds": 0.02})
        async with scheduler.slot(resource_keys("computer_read_process_output", {"pid": 5})):
            await scheduler.run("computer_read_process_output", {"pid": 6}, lambda: asyncio.sleep(0))
            with self.assertRaisesRegex(ToolError, "BORG_BUSY"):
                await scheduler.run("computer_force_terminate", {"pid": 5}, lambda: asyncio.sleep(0))

    def test_aliases_and_moves_share_ordered_file_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").write_text("x")
            (root / "alias").symlink_to(root / "real")
            self.assertTrue(set(resource_keys("computer_read_file", {"path": str(root / "alias")})) & set(resource_keys("computer_edit_block", {"file_path": str(root / "real")})))
            self.assertEqual(resource_keys("computer_move_file", {"source": "/tmp/a", "destination": "/tmp/b"}), resource_keys("computer_move_file", {"source": "/tmp/b", "destination": "/tmp/a"}))

    def test_desktop_is_shared_and_browser_sessions_are_independent(self):
        self.assertEqual(resource_keys("ui_click", {}), resource_keys("desktop_click", {}))
        self.assertNotEqual(resource_keys("browser_click", {"session_id": "12345678123456781234567812345678"}), resource_keys("browser_click", {"session_id": "22345678123456781234567812345678"}))

    def test_browser_uuid_aliases_share_one_key(self):
        aliases = ["ABCDEF78123456781234567812345678", "abcdef78-1234-5678-1234-567812345678", "{abcdef78-1234-5678-1234-567812345678}"]
        self.assertEqual(len({resource_keys("browser_click", {"session_id": alias}) for alias in aliases}), 1)

    async def test_case_alias_and_hard_link_wait_on_same_file(self):
        import os
        real = self.root / "Example.txt"
        real.write_text("value")
        hard = self.root / "hard-link"
        os.link(real, hard)
        scheduler = CallScheduler({"wait_seconds": 0.02})
        aliases = [hard]
        case = self.root / "example.txt"
        if case.exists() and os.path.samefile(real, case):
            aliases.append(case)
        else:
            case.write_text("distinct file")
        async with scheduler.slot(resource_keys("computer_write_file", {"path": str(real)})):
            for alias in aliases:
                with self.assertRaisesRegex(ToolError, "BORG_BUSY"):
                    await scheduler.run("computer_write_file", {"path": str(alias)}, lambda: asyncio.sleep(0))
            if case not in aliases:
                await scheduler.run("computer_write_file", {"path": str(case)}, lambda: asyncio.sleep(0))

    async def test_queued_symlink_replacement_revalidates_identity(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root / "one")
        scheduler = CallScheduler()
        entered, release = asyncio.Event(), asyncio.Event()
        async def write():
            entered.set()
            await release.wait()
        async with scheduler.slot(resource_keys("computer_write_file", {"path": str(alias)})):
            waiter = asyncio.create_task(scheduler.run("computer_write_file", {"path": str(alias)}, write))
            for _ in range(100):
                if scheduler.pending == 2: break
                await asyncio.sleep(0.001)
            self.assertEqual(scheduler.pending, 2)
            NativeComputer().write_file(str(alias), "replacement")
        await entered.wait()
        competing = asyncio.create_task(scheduler.run("computer_write_file", {"path": str(alias)}, lambda: asyncio.sleep(0)))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(competing.done())
        finally:
            release.set()
            await asyncio.gather(waiter, competing)

    async def test_moves_exclude_effective_destination_and_descendant_edits(self):
        scheduler = CallScheduler({"wait_seconds": 0.02})
        source = self.root / "one"
        dest = self.root / "destination"
        dest.mkdir()
        (dest / source.name).write_text("old")
        async with scheduler.slot(resource_keys("computer_move_file", {"source": str(source), "destination": str(dest)})):
            with self.assertRaisesRegex(ToolError, "BORG_BUSY"):
                await scheduler.run("computer_write_file", {"path": str(dest / source.name)}, lambda: asyncio.sleep(0))
        async with scheduler.slot(resource_keys("computer_edit_block", {"file_path": str(dest / source.name)})):
            with self.assertRaisesRegex(ToolError, "BORG_BUSY"):
                await scheduler.run("computer_move_file", {"source": str(dest), "destination": str(self.root / "moved")}, lambda: asyncio.sleep(0))

    async def test_same_job_waiters_do_not_consume_execution_slots(self):
        scheduler = CallScheduler({"max_in_flight": 2, "wait_seconds": 0.05})
        job_id = "12345678-1234-5678-1234-567812345678"
        async with scheduler.slot(resource_keys("job_status", {"job_id": job_id})):
            tasks = [asyncio.create_task(scheduler.run("job_status", {"job_id": job_id}, lambda: asyncio.sleep(0))) for _ in range(3)]
            await asyncio.sleep(0.01)
            await scheduler.run("computer_read_file", {"path": str(self.root / "one")}, lambda: asyncio.sleep(0))
            self.assertEqual(scheduler.active, 1)
        await asyncio.gather(*tasks)

    async def test_directory_symlink_rewrite_waits_for_descendant(self):
        directory = self.root / "directory"
        directory.mkdir()
        (directory / "child").write_text("original")
        alias = self.root / "directory-alias"
        alias.symlink_to(directory, target_is_directory=True)
        scheduler = CallScheduler({"wait_seconds": 0.02})
        async with scheduler.slot(resource_keys("computer_edit_block", {"file_path": str(alias / "child")})):
            with self.assertRaisesRegex(ToolError, "BORG_BUSY"):
                await scheduler.run("computer_write_file", {"path": str(alias)}, lambda: asyncio.sleep(0))

    def test_invalid_capacity_fails_at_startup(self):
        for config in ({"max_in_flight": 0}, {"queue_limit": -1}, {"wait_seconds": 0}, {"wait_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                CallScheduler(config)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.computer = NativeComputer()

    def tearDown(self):
        for row in self.computer.list_sessions()["sessions"]:
            self.computer.force_terminate(row["pid"])

    def test_process_that_does_not_read_input_has_bounded_write(self):
        row = self.computer.start_process("/usr/bin/python3 -c 'import time; time.sleep(30)'", timeout_ms=0)
        started = time.monotonic()
        with self.assertRaisesRegex(ToolError, "timed out after [0-9]+ bytes"):
            self.computer.interact_with_process(row["pid"], "x" * MAX_OUTPUT_BYTES)
        self.assertLess(time.monotonic() - started, 2)

    def test_finished_process_does_not_spin_on_eof(self):
        row = self.computer.start_process("/usr/bin/printf 'complete'", timeout_ms=0)
        self.computer._processes[row["pid"]].wait(timeout=3)
        started = time.monotonic()
        output = self.computer.read_process_output(row["pid"], timeout_ms=5000)
        self.assertEqual(output["output"], "complete")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_output_limit_does_not_discard_the_next_chunk(self):
        count = MAX_OUTPUT_BYTES + 10000
        row = self.computer.start_process(f"/usr/bin/python3 -c 'import sys; sys.stdout.write(\"x\" * {count})'", timeout_ms=0)
        first = self.computer.read_process_output(row["pid"], timeout_ms=5000)
        second = self.computer.read_process_output(row["pid"], timeout_ms=5000)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["output"] + second["output"], "x" * count)


class TraversalTests(unittest.TestCase):
    def test_shallow_listing_never_scans_descendants(self):
        import os
        computer = NativeComputer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "child").mkdir()
            (root / "child" / "nested").mkdir()
            original = os.scandir
            with patch("computer_tools.os.scandir", wraps=original) as scan:
                rows = computer.list_directory(str(root), depth=1)["entries"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(scan.call_count, 1)

    def test_no_match_search_stops_at_scan_budget(self):
        computer = NativeComputer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(20):
                (root / str(index)).touch()
            with patch("computer_tools.MAX_SCAN_ENTRIES", 5):
                result = computer.start_search(str(root), pattern="no-match")
            self.assertEqual(result["results"], [])
            self.assertTrue(result["scan_truncated"])
            self.assertEqual(result["scanned_entries"], 5)

    def test_directory_symlink_cannot_expand_scan(self):
        computer = NativeComputer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "loop").symlink_to(root, target_is_directory=True)
            result = computer.list_directory(str(root), depth=5)
            self.assertEqual(len(result["entries"]), 1)
            self.assertFalse(result["truncated"])


class MountedCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mounted_job_waiters_and_list_do_not_block_unrelated_file(self):
        from fastmcp import Client, FastMCP
        from computer_tools import BoundaryMiddleware
        from job_tools import JobStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "file"
            target.write_text("independent")
            store = JobStore(root / "jobs")
            job = store.start("/usr/bin/true", timeout_ms=0)
            entered, release = threading.Event(), threading.Event()
            first_read = True
            original_read = store._read
            def read(path):
                nonlocal first_read
                if first_read:
                    first_read = False
                    entered.set()
                    if not release.wait(5): raise RuntimeError("job test was not released")
                return original_read(path)
            store._read = read
            settings = SimpleNamespace(computer={"concurrency": {"max_in_flight": 2}},
                inbound_authorization_file=root / "absent-inbound", mem0_token_file=root / "absent-upstream")
            boundary = BoundaryMiddleware(lambda: None, settings)
            server = FastMCP("job-contention-test")
            server.add_middleware(boundary)
            server.tool(name="job_status")(store.status)
            server.tool(name="job_list")(store.list)
            server.tool(name="computer_read_file")(NativeComputer().read_file)
            async with Client(server) as client:
                first = asyncio.create_task(client.call_tool("job_status", {"job_id": job["job_id"]}))
                queued = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    queued = asyncio.create_task(client.call_tool("job_status", {"job_id": job["job_id"]}))
                    async with asyncio.timeout(2):
                        await client.call_tool("job_list", {})
                        await client.call_tool("computer_read_file", {"path": str(target)})
                    self.assertEqual(boundary.scheduler.active, 1)
                    queued.cancel()
                    with self.assertRaises(asyncio.CancelledError): await queued
                finally:
                    release.set()
                    await first
                    if queued is not None: await asyncio.gather(queued, return_exceptions=True)
                    store.close_all()

    async def test_native_worker_keeps_lock_after_client_cancellation(self):
        from fastmcp import Client, FastMCP
        from computer_tools import BoundaryMiddleware
        from operation_receipts import OperationLedger
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path, other_path = root / "one", root / "two"
            first_path.write_text("before")
            other_path.write_text("before")
            entered, release = threading.Event(), threading.Event()
            native = NativeComputer()
            def write(path: str, content: str, mode: str = "rewrite") -> dict:
                if content == "held":
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("native test writer was not released")
                return native.write_file(path, content, mode)
            settings = SimpleNamespace(computer={}, inbound_authorization_file=root / "absent-inbound",
                                       mem0_token_file=root / "absent-upstream")
            ledger = OperationLedger(root / "operations")
            boundary = BoundaryMiddleware(lambda: None, settings, ledger)
            server = FastMCP("native-cancellation-test")
            server.add_middleware(boundary)
            server.tool(name="computer_write_file")(write)
            async with Client(server) as client:
                first = asyncio.create_task(client.call_tool("computer_write_file", {"path": str(first_path), "content": "held"}))
                second = None
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    first.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await first
                    second = asyncio.create_task(client.call_tool("computer_write_file", {"path": str(first_path), "content": "second"}))
                    async with asyncio.timeout(2):
                        await client.call_tool("computer_write_file", {"path": str(other_path), "content": "independent"})
                    self.assertEqual(other_path.read_text(), "independent")
                    self.assertFalse(second.done())
                finally:
                    release.set()
                    if second is not None:
                        await second
                    await asyncio.gather(first, return_exceptions=True)
                    for _ in range(100):
                        if boundary.scheduler.pending == 0: break
                        await asyncio.sleep(0.01)
                self.assertEqual(first_path.read_text(), "second")
                self.assertEqual(boundary.scheduler.pending, 0)
                self.assertEqual(boundary.scheduler._locks, {})


if __name__ == "__main__":
    unittest.main()
