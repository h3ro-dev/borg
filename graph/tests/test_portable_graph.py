"""Portable configuration and real local transport tests; no private stores."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def install_fixture(home, owner='fixture-owner', port=26383):
    shutil.copytree(ROOT, home / 'graphiti', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT.parent / 'memory' / 'bin', home / 'mem0' / 'bin', ignore=shutil.ignore_patterns('__pycache__'))
    (home / 'config.json').write_text(json.dumps({'schema': 'borg-install/v1'}))
    settings = dict(BORG_HOME=str(home), BORG_OWNER_ID=owner, BORG_MEMORY_SCOPE='personal:'+owner,
        BORG_QDRANT_URL='http://127.0.0.1:26333', BORG_QDRANT_COLLECTION='fixture',
        BORG_HISTORY_DB=str(home/'mem0/data/history.db'), BORG_OLLAMA_URL='http://127.0.0.1:21434',
        BORG_EXTRACTION_MODEL='fixture-extract', BORG_EXTRACTION_MODEL_ID='ollama:sha256:'+'a'*64,
        BORG_EMBED_MODEL='fixture-embed', BORG_EMBED_MODEL_ID='ollama:sha256:'+'b'*64,
        BORG_EMBED_DIMS='16', BORG_FALKORDB_HOST='127.0.0.1', BORG_FALKORDB_PORT=str(port),
        BORG_FALKORDB_GRAPH='fixture_legacy', BORG_GRAPH_LLM_URL='http://127.0.0.1:21460/v1',
        BORG_GRAPH_MODEL='fixture-graph')
    env = {k:v for k,v in os.environ.items() if not k.startswith(('BORG_', 'MEM0_', 'GRAPH_', 'OLLAMA_', 'SHIM_'))}
    env.update(settings, PYTHONDONTWRITEBYTECODE='1')
    return env


def run_code(home, env, code):
    return subprocess.run([sys.executable, '-c', code], env=env, cwd=home,
                          text=True, capture_output=True, timeout=45)


class PortableGraphTests(unittest.TestCase):
    def test_adapter_uses_explicit_installer_config(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            env = install_fixture(home)
            env.update(MEM0_EMBED_OLLAMA_URL='http://192.0.2.1:1', GRAPH_LLM_MODEL='private-model')
            result = run_code(home, env, '''
import sys
sys.path.insert(0, 'mem0/bin')
import mem0_graph as m
a = m.GraphitiAdapter()
assert a.graph_port == 26383, a.graph_port
assert a.graph_host == '127.0.0.1'
assert a.llm_url == 'http://127.0.0.1:21460/v1', a.llm_url
assert a.llm_model == 'fixture-graph'
assert a.embed_ollama_url == 'http://127.0.0.1:21434'
''')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_feed_configuration_and_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            env = install_fixture(home)
            env.update(GRAPH_LLM_URLS='http://192.0.2.1:1', GRAPH_FEED_QDRANT_URL='http://192.0.2.2:1')
            result = run_code(home, env, '''
import sys
sys.path.insert(0, 'graphiti')
import backfill as b
assert b.DEFAULT_COLLECTION == 'fixture'
assert b.QDRANT == 'http://127.0.0.1:26333'
assert b.llm_url_pool() == ['http://127.0.0.1:21460/v1']
p = {'id':'one', 'payload':{'data':'A fixture fact.', 'user_id':'fixture-owner'}}
f, reason = b.classify_point(p)
assert reason == 'admitted', reason
assert f['scope'] == 'personal:fixture-owner'
p['payload']['user_id'] = 'different-owner'
assert b.classify_point(p)[0] is None
''')
            self.assertEqual(result.returncode, 0, result.stderr)


class InstalledEntrypointTests(unittest.TestCase):
    def test_native_clients_and_import_closure(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            env = install_fixture(home)
            result = run_code(home, env, '''
import sys, asyncio, importlib.machinery
sys.path[:0] = ['graphiti', 'mem0/bin']
import backfill as b
import mem0_graph as m
async def check():
    writer = b.GraphitiWriter()
    assert writer._graph_port == 26383
    assert writer._llm.config.model == 'fixture-graph'
    assert writer._embedder.config.embedding_model == 'fixture-embed'
    assert writer._embedder.config.embedding_dim == 16
    assert hasattr(writer._reranker.client, 'chat')
    await writer.close()
    # Full adapter client creation requires the configured Falkor service;
    # native_graph_probe.py covers it against coordinated disposable instances.
    adapter = m.GraphitiAdapter(m.ScopeGraphRegistry({}))
    assert adapter.graph_port == 26383
asyncio.run(check())
for name in ['graphiti-mcp-server', 'graph-recall-projector', 'lane-supervisor', 'ollama-schema-shim']:
    module = importlib.machinery.SourceFileLoader(name, 'graphiti/bin/'+name).load_module()
    if name == 'lane-supervisor':
        assert str(module.VENV_PY).endswith('/mem0/venv/bin/python')
        for lane in module.LANES:
            if lane['name'] == 'graph':
                assert 'GRAPH_LLM_URLS' not in module.lane_environ(lane)
    if name == 'ollama-schema-shim':
        assert module.PORT == 21460
        assert not module.TEE_ON
''')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_registry_reloads_and_door_authorization_precedes_network(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            env = install_fixture(home)
            result = run_code(home, env, '''
import sys, json, asyncio, importlib.machinery
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = ['graphiti', 'mem0/bin']
import mem0_graph as m
import graph_scope as scope
adapter = m.GraphitiAdapter()
try:
    adapter.registry
except m.GraphScopeError:
    pass
else:
    raise AssertionError('missing registry must remain unavailable')
registry = scope.ScopeRegistry(Path('graphiti/data/scope-graphs.json'))
registry.ensure_scope('team:a')
assert adapter.registry.keys_for(['team:a']) == [m.scope_graph_key('team:a')]
registry.ensure_scope('team:b')
assert adapter.registry.keys_for(['team:b']) == [m.scope_graph_key('team:b')]
assert 'fixture_legacy' not in adapter.registry.keys_for([], full_access=True)
door = importlib.machinery.SourceFileLoader('fixture_door', 'graphiti/bin/graphiti-mcp-server').load_module()
door.get_access_token = lambda: SimpleNamespace(client_id='fixture', scopes=['team:a'], claims={})
principal, other, keys = door._portable_context()
assert keys == [m.scope_graph_key('team:a')]
door.get_access_token = lambda: SimpleNamespace(client_id='fixture', scopes=['team:missing'], claims={})
try:
    door._portable_context()
except door.ToolError:
    pass
else:
    raise AssertionError('unknown scope permitted')
''')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_optional_supervisor_and_disabled_feed_do_not_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            env = install_fixture(home)
            env.update(GRAPH_LLM_URLS='http://192.0.2.1:1', GRAPH_FEED_LIVE='0')
            result = run_code(home, env, '''
import importlib.machinery, sys
s = importlib.machinery.SourceFileLoader('fixture_supervisor', 'graphiti/bin/lane-supervisor').load_module()
def forbidden(*args, **kwargs):
    raise AssertionError('unexpected process or service access')
s.subprocess.run = forbidden
s.subprocess.Popen = forbidden
s.http_ok = forbidden
sys.argv = ['lane-supervisor', '--dry-run']
assert s.main() == 0
''')
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ['ox', 'qwen', 'luna', 'terra']:
                self.assertIn(name, result.stdout)
            result = subprocess.run([sys.executable, str(home/'graphiti/backfill.py'), '--json'],
                                    cwd=home, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['outcome'], 'NOT_RUN')
            self.assertFalse((home/'graphiti/data/backfill-state.json').exists())

    def test_capture_requires_explicit_opt_in_and_explicit_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            capture = home/'private/shim-pairs.jsonl'
            env = install_fixture(home)
            env.update(BORG_GRAPH_CAPTURE_OPT_IN='1', SHIM_PAIRS_LOG=str(capture))
            result = run_code(home, env, '''
import importlib.machinery
m = importlib.machinery.SourceFileLoader('fixture_shim_opt_in', 'graphiti/bin/ollama-schema-shim').load_module()
assert m.TEE_ON
assert m.TEE_F == str(__import__('pathlib').Path(__import__('os').environ['SHIM_PAIRS_LOG']).resolve())
assert not __import__('pathlib').Path(m.TEE_F).exists()
''')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_supervisor_source_has_no_embedded_route_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            env = install_fixture(home)
            result = run_code(home, env, '''
import importlib.machinery
s = importlib.machinery.SourceFileLoader('fixture_supervisor_routes', 'graphiti/bin/lane-supervisor').load_module()
assert all('GRAPH_LLM_URLS' not in lane.get('env', {}) for lane in s.LANES)
assert all('HARVEST_URL' not in lane.get('env', {}) for lane in s.LANES)
''')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_homes_native_qdrant_http_transport_and_feed_state(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        with tempfile.TemporaryDirectory() as temp:
            servers = []
            threads = []
            requests = []
            try:
                for owner in ['alpha', 'beta']:
                    class Handler(BaseHTTPRequestHandler):
                        def do_POST(self):
                            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                            requests.append((self.server.owner, self.path, body))
                            payload = {'result': {'points': [{'id': self.server.owner, 'payload': {
                                'user_id': self.server.owner, 'data': 'Synthetic source fact',
                                'scope': 'team:same', 'run_id': 'fixture'}}], 'next_page_offset': None}}
                            output = json.dumps(payload).encode()
                            self.send_response(200)
                            self.send_header('Content-Length', str(len(output)))
                            self.end_headers()
                            self.wfile.write(output)
                        def log_message(self, *args):
                            pass
                    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                    server.owner = owner
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    servers.append(server)
                    threads.append(thread)
                homes = []
                for i, owner in enumerate(['alpha', 'beta']):
                    home = Path(temp)/owner
                    homes.append(home)
                    env = install_fixture(home, owner=owner, port=26383+i)
                    env['BORG_QDRANT_URL'] = 'http://127.0.0.1:'+str(servers[i].server_port)
                    result = run_code(home, env, '''
import asyncio, sys
sys.path.insert(0, 'graphiti')
import backfill as b
class Writer:
    async def add_episode(self, group):
        assert all(row['payload']['user_id'] == b.OWNER_ID for row in group['rows'])
        return {'nodes': 1, 'edges': 0}
async def run():
    source = b.QdrantSource()
    try:
        first = await b.run_once(source, Writer(), state_path=b.STATE_F, scope_map_path=b.SCOPE_MAP_F)
        second = await b.run_once(source, Writer(), state_path=b.STATE_F, scope_map_path=b.SCOPE_MAP_F)
        assert first['new_episodes'] == 1, first
        assert second['new_episodes'] == 0, second
    finally:
        source.close()
asyncio.run(run())
''')
                    self.assertEqual(result.returncode, 0, result.stderr)
                for home, owner in zip(homes, ['alpha', 'beta']):
                    state = json.loads((home/'graphiti/data/backfill-state.json').read_text())
                    self.assertEqual(set(state['graph_processed']), {owner})
                maps = [json.loads((h/'graphiti/data/scope-graphs.json').read_text()) for h in homes]
                self.assertEqual(maps[0], maps[1])  # same scope key, separate per-instance state
                self.assertNotEqual(servers[0].server_port, servers[1].server_port)
                self.assertEqual(len(requests), 4)
                for owner, path, body in requests:
                    self.assertEqual(path, '/collections/fixture/points/scroll')
                    self.assertEqual(body['filter']['must'][0]['match']['value'], owner)
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=5)


class NativeModelTransportTests(unittest.TestCase):
    def test_native_embed_and_rerank_use_names_and_separate_ports(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.server.server_port, self.path, body))
                if self.path == '/v1/embeddings':
                    response = {'object':'list', 'model':body['model'], 'data':[
                        {'object':'embedding','index':0,'embedding':[0.25]*16}],
                        'usage':{'prompt_tokens':1,'total_tokens':1}}
                elif self.path == '/v1/chat/completions':
                    response = {'id':'fixture','object':'chat.completion','created':1,'model':body['model'],
                        'choices':[{'index':0,'message':{'role':'assistant','content':'True'},
                        'finish_reason':'stop','logprobs':{'content':[{'token':'True','logprob':-0.1,
                        'bytes':[84,114,117,101], 'top_logprobs':[{'token':'True','logprob':-0.1,'bytes':[84,114,117,101]}]}]}}]}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self,*args):
                pass
        servers = [ThreadingHTTPServer(('127.0.0.1',0),Handler) for _ in range(2)]
        threads = [threading.Thread(target=s.serve_forever,daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                home = Path(temp)/'one'
                env = install_fixture(home)
                env['BORG_OLLAMA_URL'] = 'http://127.0.0.1:'+str(servers[0].server_port)
                env['BORG_GRAPH_LLM_URL'] = 'http://127.0.0.1:'+str(servers[1].server_port)+'/v1'
                result = run_code(home, env, '''
import sys, asyncio
sys.path.insert(0,'graphiti')
import backfill
async def main():
    writer=backfill.GraphitiWriter()
    try:
        vector=await writer._embedder.create('synthetic fixture')
        assert vector == [0.25]*16, vector
        ranked=await writer._reranker.rank('synthetic query',['synthetic passage'])
        assert len(ranked)==1 and ranked[0][0]=='synthetic passage', ranked
    finally:
        await writer.close()
asyncio.run(main())
''')
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(requests),2)
            self.assertEqual(requests[0][0],servers[0].server_port)
            self.assertEqual(requests[0][2]['model'],'fixture-embed')
            self.assertEqual(requests[1][0],servers[1].server_port)
            self.assertEqual(requests[1][2]['model'],'fixture-graph')
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)



class CanaryExitTests(unittest.TestCase):
    def test_native_failed_canary_is_nonzero_without_running_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            env = install_fixture(home)
            result = run_code(home, env, '''
import sys, asyncio
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, 'graphiti')
import backfill
with mock.patch.object(backfill, 'run_isolated_canary', new=mock.AsyncMock(return_value={'outcome':'FAIL'})):
    assert asyncio.run(backfill.async_main(SimpleNamespace(canary=True, fetch=1, json=True))) == 1
''')
            self.assertEqual(result.returncode, 0, result.stderr)



class EmptyGraphSemanticsTests(unittest.TestCase):
    def test_empty_registered_graph_does_not_call_model(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'one'
            env = install_fixture(home)
            result = run_code(home, env, '''
import sys, asyncio
sys.path.insert(0,'mem0/bin')
import mem0_graph as m
from redis.exceptions import ResponseError
class EmptyGraph:
    def ro_query(self,*args):
        raise ResponseError('Invalid graph operation on empty key')
adapter=m.GraphitiAdapter(m.ScopeGraphRegistry({}))
adapter._falkor_graph=lambda key: EmptyGraph()
def forbidden():
    raise AssertionError('empty graph must not invoke model client')
adapter._graphiti_client=forbidden
async def run():
    assert await adapter.search('query',['memscope_empty']) == []
    assert await adapter.entity_timeline('entity',['memscope_empty']) == []
    assert await adapter.recent_episodes(['memscope_empty']) == []
    stats=await adapter.stats(['memscope_empty'])
    assert stats['status']=='READY' and stats['empty'], stats
    assert stats['ingestion_watermark_state']=='unknown'
asyncio.run(run())
''')
            self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
