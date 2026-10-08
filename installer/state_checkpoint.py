"""Explicit operator checkpoint and offline graph restore proof.

Run with the installed memory Python. Stops only the brain, snapshots Qdrant,
uses SQLite's online backup, and archives stable staged files, never live stores.
No retention, deletion, credentials provisioning, or scheduled operation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.request
import tarfile
import tempfile


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    path.chmod(0o600)


def file_map(root):
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('*'))
            if p.is_file() and not p.is_symlink()}


def copy_tree(source, target):
    """Preserve files privately, and use SQLite's transaction-aware backup."""
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    for p in sorted(source.iterdir()):
        dest = target / p.name
        if p.is_symlink():
            dest.symlink_to(os.readlink(p))
        elif p.is_dir():
            copy_tree(p, dest)
        elif p.is_file():
            # These belong to SQLite's live connection, not its restored image.
            if p.name.endswith(('-wal', '-shm')):
                main = p.with_name(p.name.rsplit('-', 1)[0])
                if main.exists():
                    with main.open('rb') as f:
                        if f.read(16) == b'SQLite format 3\0':
                            continue
            with p.open('rb') as f:
                sqlite = f.read(16) == b'SQLite format 3\0'
            if sqlite:
                with closing(sqlite3.connect(p.as_uri() + '?mode=ro', uri=True)) as src:
                    with closing(sqlite3.connect(dest)) as dst:
                        src.backup(dst)
                        dst.execute('PRAGMA journal_mode=DELETE')
                        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise RuntimeError('sqlite_integrity_failed')
            else:
                shutil.copy2(p, dest)
            dest.chmod(0o600)


def graph_counts(client):
    names = client.execute_command('GRAPH.LIST')
    result = {}
    for name in sorted(names):
        key = name.decode() if isinstance(name, bytes) else name
        rows = client.execute_command('GRAPH.RO_QUERY', key, 'MATCH (n) RETURN count(n)')
        result[key] = int(rows[1][0][0])
    return result


def checkpoint_beads(home, destination, stage, proof):
    """Seal a native Dolt backup; a raw embedded database copy is advisory only."""
    workspace = home / 'beads'
    target = stage / '.borg/beads'
    target.mkdir(parents=True, mode=0o700)
    backup = destination / 'beads-dolt-backup'
    executable = str(home / 'bin/bd') if (home / 'bin/bd').exists() else 'bd'

    def bd(cwd, *args, check=True):
        env = dict(os.environ, BEADS_DIR=str(cwd / '.beads'), BD_NON_INTERACTIVE='1')
        # An inherited database override must not redirect the isolated restore.
        env.pop('BEADS_DB', None)
        result = subprocess.run([executable, '--actor', 'codex-primary', *args],
                                cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=180)
        if check and result.returncode:
            raise RuntimeError('beads_' + '_'.join(args[:2]) + '_failed')
        return result

    def count(cwd):
        rows = json.loads(bd(cwd, 'list', '--all', '--limit', '0', '--json',
                             '--include-infra', '--include-templates', '--include-gates').stdout)
        if not isinstance(rows, list):
            raise RuntimeError('beads_invalid_issue_list')
        return len(rows)

    bd(workspace, 'backup', 'init', str(backup))
    proof['beads_backup_destination'] = str(backup)
    bd(workspace, 'backup', 'sync', '--quiet')
    before = file_map(backup)
    if not before:
        raise RuntimeError('beads_native_backup_empty')
    copy_tree(backup, target / 'dolt-backup')
    if before != file_map(backup) or before != file_map(target / 'dolt-backup'):
        raise RuntimeError('beads_native_backup_changed_during_copy')
    proof['beads_native_backup_sha256'] = before
    exported = target / 'export.jsonl'
    bd(workspace, 'export', '--all', '-o', str(exported))
    exported.chmod(0o600)
    with exported.open() as stream:
        export_count = sum(1 for line in stream if line.strip() and json.loads(line))
    live_count = count(workspace)
    proof.update(beads_live_issue_count=live_count, beads_export_issue_count=export_count)
    if export_count != live_count:
        raise RuntimeError('beads_export_count_mismatch')

    # Keep the isolated restored database as evidence; never overwrite live Beads.
    restored = Path(tempfile.mkdtemp(prefix=stage.name + '.beads-restore-', dir=destination))
    proof['beads_restore_directory'] = str(restored)
    initialized = bd(restored, 'init', '--non-interactive', '--prefix', 'checkpoint',
                     '--skip-hooks', '--skip-agents', check=False)
    if initialized.returncode:
        proof.update(beads_restore_proof='not_run',
                     beads_restore_reason='noninteractive_init_exit_' + str(initialized.returncode),
                     beads_restored_issue_count=None)
    else:
        bd(restored, 'backup', 'restore', str(target / 'dolt-backup'), '--force')
        restored_count = count(restored)
        proof['beads_restored_issue_count'] = restored_count
        if restored_count != live_count:
            raise RuntimeError('beads_restore_count_mismatch')
        proof['beads_restore_proof'] = 'PASS'

    raw = workspace / '.beads'
    proof['beads_raw_copy'] = 'changed_during_copy'
    try:
        before = file_map(raw)
        copy_tree(raw, target / 'raw')
        if before == file_map(raw) == file_map(target / 'raw'):
            proof['beads_raw_copy'] = 'stable'
    except (OSError, shutil.Error) as exc:
        # Raw files may rotate/disappear while embedded Dolt is in use.
        proof['beads_raw_copy_error_type'] = type(exc).__name__


def verify_archive(archive, manifest):
    """Read every archived regular file and compare against the sealed stage."""
    observed = {}
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar:
            if member.isfile():
                stream = tar.extractfile(member)
                observed[member.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if observed != manifest:
        raise RuntimeError('archive_manifest_mismatch')


def restore_graph(archive, root, port):
    """Restore RDB from the archive into an isolated, owned redislite process."""
    import redislite
    import redis
    root.mkdir(mode=0o700)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
    with tarfile.open(archive, 'r:gz') as tar:
        with tar.extractfile('.borg/graphiti/data/graph.rdb') as src:
            with (root / 'restore.rdb').open('wb') as dst:
                shutil.copyfileobj(src, dst)
        state = json.load(tar.extractfile('.borg/graphiti/data/backfill-state.json'))
    command = [redislite.__redis_executable__, '--bind', '127.0.0.1',
               '--port', str(port), '--dir', str(root), '--dbfilename', 'restore.rdb',
               '--save', '', '--appendonly', 'no', '--daemonize', 'no',
               '--loadmodule', redislite.__falkordb_module__, 'THREAD_COUNT', '2']
    with (root / 'restore-server.log').open('wb') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            client = redis.Redis(host='127.0.0.1', port=port, socket_timeout=10)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('restore_server_exited')
                try:
                    if client.ping():
                        break
                except redis.ConnectionError:
                    time.sleep(.1)
            else:
                raise RuntimeError('restore_server_not_ready')
            return graph_counts(client), len(state.get('retry', {}))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def checkpoint(home, destination, stem, restore_port, expected_retries, *, leave_brain_stopped=False):
    import redis
    os.umask(0o077)
    config = json.loads((home / 'config.json').read_text())
    ports = config['ports']
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = destination / (stem + '.stage')
    stage.mkdir(mode=0o700)
    archive = destination / (stem + '.tar.gz')
    if archive.exists():
        raise RuntimeError('archive_already_exists')
    proof_path = destination / (stem + '.restore-proof.json')
    proof = {'schema': 'borg-state-checkpoint/1', 'status': 'RUNNING', 'home': str(home),
             'archive': str(archive), 'started_at': time.time()}
    write_json(proof_path, proof)
    borg = [str(home / 'bin/borg')]
    stopped = False
    try:
        subprocess.run(borg + ['stop', '--home', str(home), 'brain'], check=True,
                       capture_output=True, timeout=45)
        stopped = True
        data = home / 'graphiti/data'
        if (data / 'backfill.lock').exists():
            raise RuntimeError('feed_lock_present')
        # Never copy stores while an orphan feed/projector is still writing.
        processes = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True)
        for line in processes.splitlines():
            if str(home / 'graphiti') in line and any(x in line for x in
                    ('backfill.py', 'graph-recall-projector')):
                raise RuntimeError('feed_or_projector_still_running')
        state_sha = digest(data / 'backfill-state.json')
        retries = len(json.loads((data / 'backfill-state.json').read_text()).get('retry', {}))
        if retries != expected_retries:
            raise RuntimeError('unexpected_retry_count')
        client = redis.Redis(host='127.0.0.1', port=int(ports['graph']), socket_timeout=60)
        client.save()
        counts = graph_counts(client)
        matched = file_map(data)
        copy_tree(data, stage / '.borg/graphiti/data')
        if matched != file_map(data) or matched != file_map(stage / '.borg/graphiti/data'):
            raise RuntimeError('graph_store_changed_during_copy')
        checkpoint_beads(home, destination, stage, proof)
        for relative in ('coordination/data', 'policies', 'ops',
                         'projects/shared-memory-repair/finish-20261008'):
            source = home / relative
            copy_tree(source, stage / '.borg' / relative)
        for profile in sorted((home / 'conductors').glob('*/profile')):
            target = stage / '.borg' / profile.relative_to(home)
            target.mkdir(parents=True, mode=0o700)
            for name in ('config.toml', 'AGENTS.md', 'auth.json'):
                source = profile / name
                if source.exists():
                    shutil.copy2(source, target / name)
                    (target / name).chmod(0o600)
        for name in ('config.json', 'blueprint.json'):
            if (home / name).exists():
                shutil.copy2(home / name, stage / '.borg' / name)
        base = f'http://127.0.0.1:{ports["qdrant"]}'
        def request(url, method='GET'):
            return urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=600)
        collections = json.load(request(base + '/collections'))['result']['collections']
        snapshots = []
        (stage / 'qdrant-snapshots').mkdir(mode=0o700)
        for row in collections:
            name = row['name']
            snapshot = json.load(request(f'{base}/collections/{name}/snapshots?wait=true', 'POST'))['result']['name']
            with request(f'{base}/collections/{name}/snapshots/{snapshot}') as src:
                with (stage / 'qdrant-snapshots' / (name + '.snapshot')).open('wb') as dst:
                    shutil.copyfileobj(src, dst)
            snapshots.append({'collection': name, 'snapshot': snapshot})
        if state_sha != digest(data / 'backfill-state.json') or counts != graph_counts(client):
            raise RuntimeError('graph_state_changed_during_checkpoint')
        manifest = file_map(stage)
        manifest_path = destination / (stem + '.sha256.json')
        write_json(manifest_path, manifest)
        with tarfile.open(archive, 'x:gz') as tar:
            for child in sorted(stage.iterdir()):
                tar.add(child, arcname=child.name)
        archive.chmod(0o600)
        verify_archive(archive, manifest)
        restored, restored_retries = restore_graph(archive, destination / (stem + '.restore'), restore_port)
        if restored != counts or restored_retries != retries:
            raise RuntimeError('restore_counts_mismatch')
        proof.update(status='PASS', archive_sha256=digest(archive), manifest=str(manifest_path),
                     manifest_sha256=digest(manifest_path), live_graph_counts=counts,
                     restored_graph_counts=restored, retry_entries=restored_retries,
                     backfill_state_sha256=state_sha, qdrant_snapshots=snapshots,
                     restored_from_archive=True, completed_at=time.time())
    except Exception as exc:
        # Error class only: never echo request bodies or configuration values.
        proof.update(status='FAIL', error_type=type(exc).__name__,
                     error_code=str(exc) if isinstance(exc, RuntimeError) else 'inspect_local_failure')
        raise
    finally:
        if stopped and not leave_brain_stopped:
            result = subprocess.run(borg + ['start', '--home', str(home), 'brain'],
                                    capture_output=True, timeout=45)
            proof['brain_restart_exit'] = result.returncode
            if result.returncode:
                proof['status'] = 'FAIL'
        elif stopped:
            proof['brain_left_stopped'] = True
        write_json(proof_path, proof)
    print(json.dumps({'status': proof['status'], 'proof': str(proof_path),
                      'graphs': len(counts), 'retry_entries': restored_retries}))
    return 0 if proof['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--stem', required=True)
    parser.add_argument('--restore-port', type=int, required=True)
    parser.add_argument('--expected-retries', type=int, required=True)
    parser.add_argument('--leave-brain-stopped', action='store_true', help='Keep explicit maintenance hold after checkpoint')
    args = parser.parse_args()
    if Path(args.stem).name != args.stem or args.stem in ('.', '..'):
        parser.error('stem must be one filename')
    try:
        return checkpoint(args.home.resolve(), args.destination.resolve(), args.stem,
                          args.restore_port, args.expected_retries, leave_brain_stopped=args.leave_brain_stopped)
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
