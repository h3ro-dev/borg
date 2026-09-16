"""Process lifecycle regressions, using only isolated native child commands."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import resource
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from computer_tools import BoundaryMiddleware, MAX_FILE_BYTES, NativeComputer


def command(code):
    return 'exec ' + shlex.join([sys.executable, '-c', code])


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('condition not true before deadline')
        time.sleep(.01)


def closed(proc):
    return all(s.closed for s in (proc.stdin, proc.stdout, proc.stderr))


def fd_count():
    count = 0
    for fd in range(256):
        try:
            os.fstat(fd)
            count += 1
        except OSError:
            pass
    return count


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeComputer()

    def tearDown(self):
        for pid in list(self.native._processes):
            self.native.force_terminate(pid)

    def start(self, cmd):
        return self.native.start_process(cmd, shell='/bin/sh', timeout_ms=0)['pid']

    def finish(self, pid):
        proc = self.native._processes[pid]
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.fail('command blocked without a caller reading output')
        wait_for(lambda: closed(proc))
        return proc

    def test_short_unread_commands_do_not_grow_fds_under_low_limit(self):
        code = '''
import resource, time
from computer_tools import NativeComputer
from test_process_lifecycle import fd_count
resource.setrlimit(resource.RLIMIT_NOFILE, (96, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
native = NativeComputer()
before = fd_count()
try:
    for i in range(125):
        pid = native.start_process('false', shell='/bin/sh', timeout_ms=0)['pid']
        native._processes[pid].wait(timeout=3)
        time.sleep(.01)
    deadline = time.monotonic() + 3
    while fd_count() > before and time.monotonic() < deadline:
        time.sleep(.01)
    after = fd_count()
    assert after <= before, (before, after)
    assert all(p.returncode == 1 for p in native._processes.values())
    print('125 unread commands; RLIMIT_NOFILE=96; fd_before=%s fd_after=%s' % (before, after))
finally:
    for pid in list(native._processes):
        native.force_terminate(pid)
'''
        row = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
        self.assertEqual(row.returncode, 0, row.stderr)
        print(row.stdout.strip())

    def test_large_stdout_and_stderr_finish_without_reads(self):
        pid = self.start(command('import os; os.write(1,b"a"*400000); os.write(2,b"b"*400000)'))
        self.finish(pid)
        output = ''
        while len(output) < 800000:
            row = self.native.read_process_output(pid, timeout_ms=0)
            self.assertGreater(row['bytes'], 0)
            output += row['output']
        self.assertEqual(output.count('a'), 400000)
        self.assertEqual(output.count('b'), 400000)
        self.assertEqual(row['next_offset'], 800000)

    def test_delayed_tail_truncation_and_explicit_replay(self):
        count = MAX_FILE_BYTES + 12345
        pid = self.start(command(f'import os; os.write(1,b"x"*{count-4}+b"TAIL")'))
        self.finish(pid)
        with self.assertRaisesRegex(ToolError, r'outside retained output.*retained_from=12345'):
            self.native.read_process_output(pid, offset=0)
        first = self.native.read_process_output(pid, length=10, timeout_ms=0)
        self.assertEqual((first['offset'], first['next_offset'], first['retained_from']), (12345, 12355, 12345))
        self.assertTrue(first['truncated'])
        replay = self.native.read_process_output(pid, offset=12345, length=10, timeout_ms=0)
        self.assertEqual(replay['output'], first['output'])
        tail = self.native.read_process_output(pid, offset=count-4, length=10, timeout_ms=0)
        self.assertEqual(tail['output'], 'TAIL')
        self.assertEqual(tail['next_offset'], count)
        self.assertLessEqual(len(self.native._process_output[pid]['data']), MAX_FILE_BYTES)

    def test_interactive_stdin_survives_another_completion(self):
        pid = self.start('read value; printf "reply:%s" "$value"')
        self.finish(self.start('printf short'))
        self.assertIsNone(self.native._processes[pid].poll())
        self.assertFalse(self.native._processes[pid].stdin.closed)
        self.native.interact_with_process(pid, 'hello\n')
        self.finish(pid)
        self.assertEqual(self.native.read_process_output(pid)['output'], 'reply:hello')

    def test_output_eof_while_running_keeps_stdin(self):
        pid = self.start('exec 1>&- 2>&-; read value')
        wait_for(lambda: self.native._processes[pid].stdout.closed)
        self.assertFalse(self.native._processes[pid].stdin.closed)
        self.native.interact_with_process(pid, 'done\n')
        self.finish(pid)

    def test_long_read_does_not_block_short_command(self):
        pid = self.start('read value; printf "%s" "$value"')
        entered = threading.Event()
        def read():
            entered.set()
            return self.native.read_process_output(pid, timeout_ms=5000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            long = pool.submit(read)
            entered.wait(1)
            other = pool.submit(self.start, 'printf short').result(timeout=1)
            self.finish(other)
            self.assertEqual(self.native.read_process_output(other)['output'], 'short')
            self.assertFalse(long.done())
            self.native.interact_with_process(pid, 'done\n')
            self.assertEqual(long.result(timeout=3)['output'], 'done')

    def test_50_simultaneous_interactive_sessions(self):
        with ThreadPoolExecutor(max_workers=50) as pool:
            pids = list(pool.map(lambda _: self.start('read value; printf "%s" "$value"'), range(50)))
            self.assertEqual(len(set(pids)), 50)
            self.assertTrue(all(self.native._processes[p].poll() is None for p in pids))
            list(pool.map(lambda p: self.native.interact_with_process(p, f'{p}\n'), pids))
            for pid in pids:
                self.finish(pid)
            rows = list(pool.map(self.native.read_process_output, pids))
            self.assertEqual([r['output'] for r in rows], list(map(str, pids)))

    def test_spawn_failure_preserves_existing_session(self):
        pid = self.start('read value')
        with patch('computer_tools.subprocess.Popen', side_effect=OSError('spawn failure')):
            with self.assertRaises(OSError):
                self.start('false')
        self.assertEqual(list(self.native._processes), [pid])
        self.native.interact_with_process(pid, 'done\n')
        self.finish(pid)

    def test_setup_and_thread_start_failures_close_owned_resources(self):
        original = subprocess.Popen
        for target in ('computer_tools.os.set_blocking', 'computer_tools.selectors.DefaultSelector.register', 'computer_tools.threading.Thread.start'):
            created = []
            def spawn(*args, **kwargs):
                proc = original(*args, **kwargs)
                created.append(proc)
                return proc
            with self.subTest(target=target):
                with patch('computer_tools.subprocess.Popen', side_effect=spawn), patch(target, side_effect=RuntimeError('injected')):
                    with self.assertRaisesRegex(ToolError, 'may have run'):
                        self.start('read value')
                self.assertEqual(len(created), 1)
                self.assertIsNotNone(created[0].poll())
                self.assertTrue(closed(created[0]))
                self.assertNotIn(created[0].pid, self.native._processes)

    def test_registry_failure_closes_owned_child(self):
        class FailedRegistry(dict):
            def __setitem__(self, key, value):
                raise RuntimeError('registration failed')
        self.native._process_output = FailedRegistry()
        original = subprocess.Popen
        created = []
        def spawn(*args, **kwargs):
            proc = original(*args, **kwargs)
            created.append(proc)
            return proc
        with patch('computer_tools.subprocess.Popen', side_effect=spawn):
            with self.assertRaisesRegex(ToolError, 'may have run'):
                self.start('read value')
        self.assertTrue(closed(created[0]))
        self.assertIsNotNone(created[0].poll())
        self.assertEqual(self.native._processes, {})

    def test_resource_exhaustion_does_not_become_policy_refusal(self):
        from computer_tools import classify_failure
        self.assertEqual(classify_failure('[Errno 24] Too many open files'), 'resource_exhausted')
        self.assertEqual(classify_failure('opaque safety policy refusal'), 'policy_refused')
        self.assertEqual(classify_failure('credential withheld'), 'credential_withheld')

    def test_termination_cleanup_preserves_other_session(self):
        victim = self.start('read value')
        survivor = self.start('read value; printf alive')
        with ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(self.native.read_process_output, victim, 5000)
            self.native.force_terminate(victim)
            self.assertFalse(reader.result(timeout=2)['running'])
        self.assertTrue(closed(self.native._processes[victim]))
        self.assertIsNone(self.native._processes[survivor].poll())
        self.native.interact_with_process(survivor, 'go\n')
        self.finish(survivor)
        self.assertEqual(self.native.read_process_output(survivor)['output'], 'alive')

    def test_high_numbered_descriptors_support_output_and_input(self):
        import fcntl
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard < 2048:
            self.skipTest('hard descriptor limit below 2048')
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 2048), hard))
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = original(*args, **kwargs)
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(proc, name)
                fd = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD, 1100)
                stream.close()
                setattr(proc, name, os.fdopen(fd, 'wb' if name == 'stdin' else 'rb', buffering=0))
            return proc
        try:
            with patch('computer_tools.subprocess.Popen', side_effect=spawn):
                pid = self.start('read value; printf "%s" "$value"')
            self.native.interact_with_process(pid, 'high\n')
            self.assertEqual(self.native.read_process_output(pid, timeout_ms=2000)['output'], 'high')
            self.finish(pid)
        finally:
            for pid in list(self.native._processes):
                self.native.force_terminate(pid)
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


class CancelledClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_offset_reports_boundary_through_mcp(self):
        native = NativeComputer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(computer={}, inbound_authorization_file=root/'absent', mem0_token_file=root/'absent2')
            server = FastMCP('retained-output-boundary')
            server.add_middleware(BoundaryMiddleware(lambda: None, settings))
            server.tool(name='computer_read_process_output')(native.read_process_output)
            pid = native.start_process(command(f'import os; os.write(1,b"x"*{MAX_FILE_BYTES+12345})'), shell='/bin/sh', timeout_ms=0)['pid']
            try:
                await asyncio.to_thread(native._processes[pid].wait, 3)
                await asyncio.to_thread(wait_for, lambda: closed(native._processes[pid]))
                async with Client(server) as client:
                    result = await client.call_tool('computer_read_process_output', {'pid': pid, 'offset': 0}, raise_on_error=False)
                    self.assertTrue(result.is_error)
                    text = ' '.join(getattr(item, 'text', '') for item in result.content)
                    self.assertIn('retained_from=12345', text)
            finally:
                native.force_terminate(pid)

    async def test_cancelled_client_leaves_native_command_running_and_draining(self):
        native = NativeComputer()
        entered, release = threading.Event(), threading.Event()
        def start(command: str) -> dict:
            result = native.start_process(command, shell='/bin/sh', timeout_ms=0)
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test caller not released')
            return result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(computer={}, inbound_authorization_file=root/'absent', mem0_token_file=root/'absent2')
            server = FastMCP('cancelled-process-client')
            server.add_middleware(BoundaryMiddleware(lambda: None, settings))
            server.tool(name='computer_start_process')(start)
            try:
                async with Client(server) as client:
                    call = asyncio.create_task(client.call_tool('computer_start_process', {'command': command('import os,time; time.sleep(.1); os.write(1,b"x"*400000)')}))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    call.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await call
                    release.set()
                    proc = next(iter(native._processes.values()))
                    await asyncio.to_thread(proc.wait, 3)
                    await asyncio.to_thread(wait_for, lambda: closed(proc))
                    self.assertEqual(native.read_process_output(proc.pid, length=10)['output'], 'x'*10)
                    self.assertEqual(proc.returncode, 0)
            finally:
                release.set()
                for pid in list(native._processes):
                    native.force_terminate(pid)


if __name__ == '__main__':
    unittest.main()
