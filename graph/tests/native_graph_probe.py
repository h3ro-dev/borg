#!/usr/bin/env python3
"""Explicit, coordinated native FalkorDB test; never part of unittest discovery.

Starts two owned disposable database processes, no model services or extraction.
Requires empty new roots and explicit ports. Native root coordinates ports first.
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

from test_portable_graph import install_fixture, run_code


def port_free(port):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root-a', type=Path, required=True)
    parser.add_argument('--root-b', type=Path, required=True)
    parser.add_argument('--port-a', type=int, required=True)
    parser.add_argument('--port-b', type=int, required=True)
    args = parser.parse_args()
    roots = [args.root_a.resolve(), args.root_b.resolve()]
    ports = [args.port_a, args.port_b]
    if roots[0] == roots[1] or ports[0] == ports[1]:
        parser.error('roots and ports must differ')
    if any(root.exists() for root in roots):
        parser.error('test roots must be new; never reuse a data directory')
    for port in ports:
        port_free(port)
    import redislite
    processes = []
    logs = []
    receipts = []
    try:
        for i, (root, port) in enumerate(zip(roots, ports)):
            env = install_fixture(root, owner='native-'+str(i), port=port)
            data = root/'graphiti/data'
            data.mkdir(parents=True)
            log = open(root/'native-server.log', 'w')
            logs.append(log)
            command = [redislite.__redis_executable__, '--bind', '127.0.0.1',
                       '--port', str(port), '--dir', str(data), '--dbfilename', 'fixture.rdb',
                       '--save', '', '--appendonly', 'no', '--daemonize', 'no',
                       '--loadmodule', redislite.__falkordb_module__, 'THREAD_COUNT', '2']
            process = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('owned native server exited; inspect native-server.log')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('native server not ready')
            env['FIXTURE_COUNT'] = str(i+1)
            result = run_code(root, env, r'''
import asyncio, importlib.machinery, json, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = ['graphiti', 'mem0/bin']
import mem0_graph as m
from graph_scope import ScopeRegistry
from falkordb import FalkorDB
registry = ScopeRegistry(Path('graphiti/data/scope-graphs.json'))
key = registry.ensure_scope('team:shared-scope')
other = registry.ensure_scope('team:empty')
db = FalkorDB(host=m.CONFIG.values['BORG_FALKORDB_HOST'], port=int(m.CONFIG.values['BORG_FALKORDB_PORT']))
count = int(os.environ['FIXTURE_COUNT'])
db.select_graph(key).query('UNWIND range(1,$count) AS i CREATE (:Entity {name: $owner, n:i})',
                           {'count':count, 'owner':m.CONFIG.values['BORG_OWNER_ID']})
keys_before = db.list_graphs()
async def check():
    adapter = m.GraphitiAdapter()
    stats = await adapter.stats([key, other])
    assert stats['status'] == 'READY', stats
    assert stats['graphs'][key]['nodes'] == count, stats
    assert stats['graphs'][other]['nodes'] == 0, stats
    assert await adapter.entity_timeline('absent', [other]) == []
    assert await adapter.search('absent', [other]) == []
    assert await adapter.recent_episodes([other]) == []
    client = adapter._graphiti_client()
    assert client.llm_client.config.model == 'fixture-graph'
    assert client.embedder.config.embedding_model == 'fixture-embed'
    assert client.embedder.config.embedding_dim == 16
    assert hasattr(client.cross_encoder.client, 'chat')
    await adapter.close()
    return stats
stats = asyncio.run(check())
assert db.list_graphs() == keys_before, (db.list_graphs(), keys_before)
p = importlib.machinery.SourceFileLoader('native_projector', 'graphiti/bin/graph-recall-projector').load_module()
async def projection():
    source = p.GraphEdgeSource()
    try:
        assert await source.edges(other) == []
    finally:
        await source.close()
asyncio.run(projection())
door = importlib.machinery.SourceFileLoader('native_door', 'graphiti/bin/graphiti-mcp-server').load_module()
door.get_access_token = lambda: SimpleNamespace(client_id='fixture', scopes=['team:shared-scope'], claims={})
read = json.loads(door.graph_stats())
assert read['results']['graphs'][key]['nodes'] == count, read
assert set(read['results']['graphs']) == {key}, read
assert json.loads(door.entity_timeline('absent'))['results'] == []
assert json.loads(door.recent_episodes())['results'] == []
assert db.list_graphs() == keys_before
print(json.dumps({'port':m.CONFIG.values['BORG_FALKORDB_PORT'], 'nodes':count,
                  'graph_key':key, 'empty_graph_not_created':True, 'status':'PASS'}))
db.connection.close()
''')
            (root/'probe.stdout').write_text(result.stdout)
            (root/'probe.stderr').write_text(result.stderr)
            if result.returncode:
                raise RuntimeError('native probe failed: '+result.stderr[-3000:])
            receipts.append(json.loads(result.stdout))
        assert receipts[0]['graph_key'] == receipts[1]['graph_key']
        assert receipts[0]['nodes'] != receipts[1]['nodes']
        print(json.dumps({'status':'PASS', 'native_instances':receipts}))
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()
        for port in ports:
            port_free(port)
        print(json.dumps({'owned_processes_stopped':len(processes), 'ports_released':ports}))


if __name__ == '__main__':
    main()
