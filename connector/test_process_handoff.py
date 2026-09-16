import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch

from fastmcp import Client, FastMCP
from computer_tools import DESCRIPTIONS, NativeComputer
from process_handoff import ComputerHandoff, local_http_client


IDENTITY = {'instance_id': '11111111-1111-4111-8111-111111111111',
            'owner': 'test', 'home': '/home/test',
            'server_generation': '22222222-2222-4222-8222-222222222222'}
CONFIG = dict(IDENTITY, url='http://127.0.0.1:18775/mcp')


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old = NativeComputer()
        self.current = NativeComputer()
        self.old_server = FastMCP('predecessor')
        self.old_server.tool(name='borg_identity')(lambda: dict(IDENTITY))
        self.new_server = FastMCP('replacement')
        self.handoff = ComputerHandoff(self.current, CONFIG, IDENTITY, Path('/unused'),
                                      lambda: Client(self.old_server))
        for name in DESCRIPTIONS:
            self.old_server.tool(name='computer_' + name)(getattr(self.old, name))
            self.new_server.tool(name='computer_' + name)(getattr(self.handoff, name))

    async def asyncTearDown(self):
        for computer in (self.old, self.current):
            for row in computer.list_sessions()['sessions']:
                computer.force_terminate(row['pid'])

    async def test_old_interactive_session_survives_new_launch_and_output_remains_readable(self):
        old = self.old.start_process('/bin/cat', timeout_ms=0, shell='/bin/sh')
        async with Client(self.new_server) as client:
            started = (await client.call_tool('computer_start_process',
                {'command': "printf 'new-runtime'", 'timeout_ms': 0})).structured_content
            self.assertIn(started['pid'], self.current._processes)
            self.assertNotIn(started['pid'], self.old._processes)
            await client.call_tool('computer_interact_with_process', {'pid': old['pid'], 'input': 'old-session\n'})
            output = (await client.call_tool('computer_read_process_output',
                {'pid': old['pid'], 'length': 12, 'offset': 0, 'timeout_ms': 1000})).structured_content
            self.assertEqual(output['output'], 'old-session\n')
            self.assertTrue(output['running'])
            self.assertIsNone(self.old._processes[old['pid']].poll())
            listed = (await client.call_tool('computer_list_sessions', {})).structured_content
            self.assertEqual({s['pid'] for s in listed['sessions']}, {old['pid'], started['pid']})
            self.assertTrue(next(s for s in listed['sessions'] if s['pid'] == old['pid'])['predecessor'])

    async def test_tool_schemas_keep_cached_contract(self):
        async with Client(self.new_server) as replacement, Client(self.old_server) as original:
            old = {t.name: t.inputSchema for t in await original.list_tools()}
            new = {t.name: t.inputSchema for t in await replacement.list_tools()}
            for name in DESCRIPTIONS:
                self.assertEqual(old['computer_' + name], new['computer_' + name])

    async def test_identity_mismatch_never_dispatches_action(self):
        calls = []
        class Changed:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def call_tool(self, *args):
                return SimpleNamespace(structured_content=dict(IDENTITY, owner='different'))
            async def call_tool_mcp(self, *args, **kwargs): calls.append(args)
        bridge = ComputerHandoff(self.current, CONFIG, IDENTITY, Path('/unused'), Changed)
        result = await bridge.interact_with_process(999, 'unchanged')
        self.assertTrue(result.is_error)
        self.assertEqual(result.meta['borg_handoff']['state'], 'not_started')
        self.assertEqual(calls, [])

    async def test_uncertain_old_action_is_not_replayed_or_launched_locally(self):
        calls = []
        class Interrupted:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def call_tool(self, *args): return SimpleNamespace(structured_content=IDENTITY)
            async def call_tool_mcp(self, name, arguments, **kwargs):
                calls.append((name, arguments, kwargs))
                raise ConnectionError('diagnostics withheld')
        bridge = ComputerHandoff(self.current, CONFIG, IDENTITY, Path('/unused'), Interrupted)
        result = await bridge.interact_with_process(999, 'once')
        self.assertTrue(result.is_error)
        self.assertEqual(result.meta['borg_handoff']['state'], 'outcome_unknown')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]['meta']['borg_target_identity'], IDENTITY)
        self.assertEqual(self.current.list_sessions()['sessions'], [])

    async def test_search_handles_are_preserved(self):
        self.old._searches['old-search'] = {'rows': [{'path': '/a'}, {'path': '/b'}], 'offset': 0}
        result = await self.handoff.get_more_search_results('old-search', 1)
        self.assertEqual(result.structured_content['results'], [{'path': '/a'}])
        self.assertEqual(self.old._searches['old-search']['offset'], 1)

    def test_remote_or_other_owner_predecessor_is_rejected(self):
        for value in (dict(CONFIG, url='https://example.com/mcp'), dict(CONFIG, owner='other'),
                      dict(CONFIG, server_generation='invalid')):
            with self.assertRaises(ValueError):
                ComputerHandoff(self.current, value, IDENTITY, Path('/unused'))

    async def test_local_transport_ignores_proxies_and_redirects(self):
        import httpx
        with patch.dict('os.environ', {'HTTP_PROXY': 'http://127.0.0.1:19999', 'NO_PROXY': ''}):
            async with local_http_client(follow_redirects=True) as client:
                self.assertFalse(client.follow_redirects)
                transport = client._transport_for_url(httpx.URL(CONFIG['url']))
                self.assertEqual(type(transport._pool).__name__, 'AsyncConnectionPool')

    async def test_old_job_exit_status_and_new_job_custody(self):
        from job_tools import mount_jobs
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            config = {'jobs_root': directory}
            old = mount_jobs(self.old_server, config)
            current = mount_jobs(self.new_server, config, self.handoff)
            previous = old.start('exit 7', timeout_ms=0)
            await asyncio.to_thread(old.processes[previous['job_id']].wait, timeout=20)
            async with Client(self.new_server) as client:
                result = (await client.call_tool('job_status', {'job_id': previous['job_id']})).structured_content
                self.assertEqual((result['state'], result['returncode']), ('failed', 7))
                new = (await client.call_tool('job_start', {'command': 'exit 0', 'timeout_ms': 0})).structured_content
                await asyncio.to_thread(current.processes[new['job_id']].wait, timeout=20)
                result = (await client.call_tool('job_status', {'job_id': new['job_id'].upper()})).structured_content
                self.assertEqual((result['state'], result['returncode']), ('succeeded', 0))
                rows = (await client.call_tool('job_list', {})).structured_content['jobs']
                self.assertEqual({r['job_id']: r['returncode'] for r in rows},
                                 {previous['job_id']: 7, new['job_id']: 0})

    async def test_browser_and_remote_handles_keep_resident_owner(self):
        calls = []
        def snapshot(session_id: str) -> dict:
            calls.append(session_id)
            return {'session_id': session_id, 'owner': 'current'}
        self.old_server.tool(name='browser_snapshot')(lambda session_id: {'owner': 'previous'})
        current = SimpleNamespace(processes={'new': object()})
        wrapped = self.handoff.store_function(current, 'browser_snapshot', snapshot)
        self.assertEqual((await wrapped('old')).structured_content['owner'], 'previous')
        self.assertEqual((await wrapped('new'))['owner'], 'current')
        self.assertEqual(calls, ['new'])
        def cancel(job_id: str) -> dict: return {'owner': 'current'}
        self.old_server.tool(name='remote_cancel')(lambda job_id: {'owner': 'previous'})
        current = SimpleNamespace(jobs=SimpleNamespace(processes={'new': object()}))
        wrapped = self.handoff.store_function(current, 'remote_cancel', cancel)
        self.assertEqual((await wrapped('old')).structured_content['owner'], 'previous')
        self.assertEqual((await wrapped('new'))['owner'], 'current')


if __name__ == '__main__':
    unittest.main()
