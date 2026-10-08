"""Explicit operator checkpoint and offline graph restore proof.

Run with the installed memory Python. Stops only the brain, snapshots Qdrant,
uses SQLite's online backup, and archives stable staged files, never live stores.
Scheduled use is opt-in and bounded: a strftime stem names the run, retention
keeps the newest archives of that stem's own family only, scratch is removed
after the restore proof passes, a free-space guard refuses before the brain
stops, and one lock serialises runs. No credentials provisioning.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import glob
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import sqlite3
import subprocess
import time
import urllib.error
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


def expand_stem(pattern, now=None):
    """Expand strftime fields in a stem; the result must be one plain filename."""
    stem = time.strftime(pattern, time.localtime(now))
    if not stem or stem in ('.', '..') or Path(stem).name != stem or '%' in stem:
        raise RuntimeError('invalid_stem')
    return stem


def stem_family(pattern):
    """The literal text before the first strftime field names the archives one schedule owns."""
    if '%' not in pattern:
        return None
    return pattern.split('%', 1)[0] or None


def retry_gate(retries, expected):
    """An operator may assert the live retry count; a schedule records it instead."""
    if expected is not None and retries != expected:
        raise RuntimeError('unexpected_retry_count')


def free_space_guard(path, min_free_gb):
    if min_free_gb and shutil.disk_usage(path).free < min_free_gb * 1024 ** 3:
        raise RuntimeError('disk_guard')


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """One checkpoint at a time per destination; a dead holder's lock is replaced."""

    def __init__(self, destination):
        self.path = destination / '.checkpoint.lock'

    def acquire(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    holder = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    holder = 0
                if holder and pid_alive(holder):
                    raise RuntimeError('checkpoint_already_running')
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, 'w') as stream:
                stream.write(str(os.getpid()))
            return self
        raise RuntimeError('checkpoint_lock_contended')

    def release(self):
        self.path.unlink(missing_ok=True)


def feed_processes(home):
    processes = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True)
    return [line for line in processes.splitlines() if str(home / 'graphiti') in line
            and any(x in line for x in ('backfill.py', 'graph-recall-projector'))]


def brain_quiet(home):
    """True between brain cycles: no running cycle, no feed lock, no feed or projector process."""
    state_path = home / 'graphiti/data/brain-state.json'
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except ValueError:
            return False
        if state.get('state') == 'running' and pid_alive(int(state.get('pid') or 0)):
            return False
    if (home / 'graphiti/data/backfill.lock').exists():
        return False
    return not feed_processes(home)


def wait_for_quiet_brain(home, timeout, *, quiet=brain_quiet, clock=time.monotonic, sleep=time.sleep, poll=0.5):
    """Stopping the brain mid-cycle kills an extraction and leaves an unproven stub; wait for the gap."""
    started = clock()
    while True:
        if quiet(home):
            return clock() - started
        if clock() - started >= timeout:
            raise RuntimeError('brain_busy')
        sleep(poll)


SIDECARS = ('.tar.gz', '.sha256.json', '.restore-proof.json')


def run_scratch(destination, stem):
    """Staging, offline restore and isolated Beads restore directories of one run."""
    paths = [destination / (stem + '.stage'), destination / (stem + '.restore')]
    paths += sorted(destination.glob(glob.escape(stem + '.stage.beads-restore-') + '*'))
    return [p for p in paths if p.is_dir() and not p.is_symlink()]


def remove_scratch(destination, stem, proof):
    """Only after a verified restore: the archive, manifest and proof remain."""
    removed = []
    for path in run_scratch(destination, stem):
        shutil.rmtree(path)
        removed.append(path.name)
    proof['scratch_removed'] = removed


def prune_family(destination, family, keep, current, proof):
    """Keep the newest archives (by age) of one family; other families and the current run stay."""
    if not family or keep < 1:
        raise RuntimeError('retention_family_required')
    archives = sorted((p for p in destination.glob(glob.escape(family) + '*.tar.gz')
                       if p.is_file() and not p.is_symlink()),
                      key=lambda p: (p.stat().st_mtime, p.name))
    stems = [p.name[:-len('.tar.gz')] for p in archives]
    if current not in stems:
        raise RuntimeError('retention_current_missing')
    pruned = []
    for stem in stems[:-keep]:
        if stem == current:
            continue
        for suffix in SIDECARS:
            path = destination / (stem + suffix)
            if path.is_file() and not path.is_symlink():
                path.unlink()
        for path in run_scratch(destination, stem):
            shutil.rmtree(path)
        pruned.append(stem)
    proof['pruned'] = pruned
    proof['retained'] = [stem for stem in stems if stem not in pruned]


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


def native_bd(home):
    """Resolve the native executable, never the live-store rebinding wrapper."""
    binary = home / 'runtime/beads/bd'
    wrapper = home / 'bin/bd'
    try:
        if wrapper.exists():
            with wrapper.open('rb') as stream:
                scripted = stream.read(2) == b'#!'
            if scripted:
                contents = wrapper.read_text()
                if '--bd' in contents:
                    line = next(line for line in contents.splitlines() if line.strip().startswith('exec '))
                    tokens = shlex.split(line)
                    binary = Path(tokens[tokens.index('--bd') + 1])
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return None
        with binary.open('rb') as stream:
            if stream.read(2) == b'#!':
                return None
        return binary
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def isolated_beads_env(workspace):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('BEADS_', 'BD_', 'DOLT_'))}
    env.update(BEADS_DIR=str(workspace / '.beads'), BD_NON_INTERACTIVE='1',
               BD_DISABLE_METRICS='1', BD_DOLT_MODE='embedded', BD_DOLT_HOST='127.0.0.1',
               BD_DOLT_SHARED_SERVER='false', BD_DOLT_AUTO_PUSH='false')
    return env


def checkpoint_beads(home, destination, stage, proof):
    """Seal a native Dolt backup; a raw embedded database copy is advisory only."""
    workspace = home / 'beads'
    live_metadata = workspace / '.beads/metadata.json'
    live_meta = digest(live_metadata)
    live_project = json.loads(live_metadata.read_text())['project_id']
    target = stage / '.borg/beads'
    target.mkdir(parents=True, mode=0o700)
    backup = destination / 'beads-dolt-backup'
    executable = str(home / 'bin/bd') if (home / 'bin/bd').exists() else 'bd'

    def bd(cwd, *args, check=True, native=None):
        env = isolated_beads_env(cwd)
        result = subprocess.run([str(native) if native else executable, '--actor', 'codex-primary', *args],
                                cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=180)
        if check and result.returncode:
            raise RuntimeError('beads_' + '_'.join(args[:2]) + '_failed')
        return result

    def count(cwd, native=None):
        rows = json.loads(bd(cwd, 'list', '--all', '--limit', '0', '--json',
                             '--include-infra', '--include-templates', '--include-gates', native=native).stdout)
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
    isolated = restored / '.beads'
    isolated.mkdir(mode=0o700)
    config = isolated / 'config.yaml'
    config.write_text('dolt:\n  mode: embedded\n  shared-server: false\n  host: 127.0.0.1\n')
    config.chmod(0o600)
    native = native_bd(home)
    proof.update(beads_restore_proof='not_run', beads_restored_issue_count=None)
    if native is None:
        proof['beads_restore_reason'] = 'native_binary_missing'
    else:
        initialized = bd(restored, 'init', '--non-interactive', '--skip-hooks', '--skip-agents',
                         '--prefix', 'checkpoint', check=False, native=native)
        if initialized.returncode:
            proof.update(beads_restore_reason='noninteractive_init_exit_' + str(initialized.returncode),
                         beads_restore_stderr_tail=initialized.stderr[-200:])
        else:
            detail = None
            try:
                project = json.loads((isolated / 'metadata.json').read_text()).get('project_id')
                if not project or project == live_project:
                    detail = 'project_id_missing_or_matches_live'
                elif not (isolated / 'embeddeddolt').is_dir():
                    detail = 'embedded_directory_missing'
                elif digest(live_metadata) != live_meta:
                    detail = 'live_metadata_changed'
            except (OSError, ValueError, AttributeError):
                detail = 'isolated_metadata_invalid'
            if detail:
                proof.update(beads_restore_reason='isolation_unproven', beads_restore_detail=detail)
            else:
                try:
                    bd(restored, 'backup', 'restore', str(target / 'dolt-backup'), '--force', native=native)
                    restored_count = count(restored, native=native)
                finally:
                    if digest(live_metadata) != live_meta:
                        raise RuntimeError('beads_live_store_touched')
                proof['beads_restored_issue_count'] = restored_count
                if restored_count != live_count:
                    raise RuntimeError('beads_restore_count_mismatch')
                proof.update(beads_restore_proof='PASS', beads_restore_binary=str(native),
                             beads_restore_project_id=project)

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


def checkpoint(home, destination, stem, restore_port, expected_retries=None, *,
               leave_brain_stopped=False, retain=None, cleanup=False, min_free_gb=0, family=None,
               busy_timeout=900):
    import redis
    os.umask(0o077)
    config = json.loads((home / 'config.json').read_text())
    ports = config['ports']
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = destination / (stem + '.stage')
    archive = destination / (stem + '.tar.gz')
    proof_path = destination / (stem + '.restore-proof.json')
    proof = {'schema': 'borg-state-checkpoint/1', 'status': 'RUNNING', 'home': str(home),
             'stem': stem, 'archive': str(archive), 'started_at': time.time()}
    borg = [str(home / 'bin/borg')]
    lock = RunLock(destination)
    lock.acquire()
    stopped = owned = False
    counts = restored_retries = None
    try:
        if archive.exists() or stage.exists() or proof_path.exists():
            raise RuntimeError('archive_already_exists')
        owned = True
        write_json(proof_path, proof)
        # Refuse before the brain stops, so a full volume never costs a cycle.
        free_space_guard(destination, min_free_gb)
        stage.mkdir(mode=0o700)
        proof['brain_wait_s'] = round(wait_for_quiet_brain(home, busy_timeout), 1)
        subprocess.run(borg + ['stop', '--home', str(home), 'brain'], check=True,
                       capture_output=True, timeout=45)
        stopped = True
        data = home / 'graphiti/data'
        if (data / 'backfill.lock').exists():
            raise RuntimeError('feed_lock_present')
        # Never copy stores while an orphan feed/projector is still writing.
        if feed_processes(home):
            raise RuntimeError('feed_or_projector_still_running')
        state_sha = digest(data / 'backfill-state.json')
        retries = len(json.loads((data / 'backfill-state.json').read_text()).get('retry', {}))
        retry_gate(retries, expected_retries)
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
            # The staged copy is the backup; the server-side snapshot would otherwise accumulate.
            try:
                request(f'{base}/collections/{name}/snapshots/{snapshot}', 'DELETE').close()
                server_copy = 'deleted'
            except (urllib.error.URLError, OSError):
                server_copy = 'delete_failed'
            snapshots.append({'collection': name, 'snapshot': snapshot, 'server_copy': server_copy})
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
        if cleanup:
            remove_scratch(destination, stem, proof)
        if retain:
            prune_family(destination, family, retain, stem, proof)
    except Exception as exc:
        # Error class only: never echo request bodies or configuration values.
        proof.update(status='FAIL', error_type=type(exc).__name__,
                     error_code=str(exc) if isinstance(exc, RuntimeError) else 'inspect_local_failure')
        raise
    finally:
        try:
            if stopped and not leave_brain_stopped:
                result = subprocess.run(borg + ['start', '--home', str(home), 'brain'],
                                        capture_output=True, timeout=45)
                proof['brain_restart_exit'] = result.returncode
                if result.returncode:
                    proof['status'] = 'FAIL'
            elif stopped:
                proof['brain_left_stopped'] = True
            if owned:
                proof['finished_at'] = time.time()
                write_json(proof_path, proof)
        finally:
            lock.release()
    print(json.dumps({'status': proof['status'], 'stem': stem, 'proof': str(proof_path),
                      'graphs': len(counts), 'retry_entries': restored_retries,
                      'pruned': proof.get('pruned', []), 'scratch_removed': proof.get('scratch_removed', [])}))
    return 0 if proof['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--stem', required=True,
                        help='archive name; strftime fields such as %%Y%%m%%d-%%H%%M expand at run time')
    parser.add_argument('--restore-port', type=int, required=True)
    parser.add_argument('--expected-retries', type=int, default=None,
                        help='operator gate: refuse unless the live retry map holds exactly this many entries')
    parser.add_argument('--leave-brain-stopped', action='store_true', help='Keep explicit maintenance hold after checkpoint')
    parser.add_argument('--retain', type=int,
                        help='after a verified restore keep only the newest N archives whose names start with '
                             'the stem text before its first strftime field; other names are never touched')
    parser.add_argument('--cleanup', action='store_true',
                        help='after a verified restore remove this run staging and restore scratch directories')
    parser.add_argument('--min-free-gb', type=int, default=0,
                        help='refuse before stopping the brain when the destination volume has less free GiB')
    parser.add_argument('--busy-timeout', type=int, default=900,
                        help='seconds to wait for a gap between brain cycles before refusing with brain_busy')
    args = parser.parse_args()
    if Path(args.stem).name != args.stem or args.stem in ('.', '..'):
        parser.error('stem must be one filename')
    family = stem_family(args.stem)
    if args.retain is not None and (args.retain < 1 or not family):
        parser.error('--retain needs a count of at least 1 and a stem with a literal prefix before a strftime field')
    if args.min_free_gb < 0 or args.busy_timeout < 0:
        parser.error('--min-free-gb and --busy-timeout must not be negative')
    try:
        stem = expand_stem(args.stem)
        return checkpoint(args.home.resolve(), args.destination.resolve(), stem,
                          args.restore_port, args.expected_retries, leave_brain_stopped=args.leave_brain_stopped,
                          retain=args.retain, cleanup=args.cleanup, min_free_gb=args.min_free_gb, family=family,
                          busy_timeout=args.busy_timeout)
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'error_type': type(exc).__name__,
                          'error_code': str(exc) if isinstance(exc, RuntimeError) else 'inspect_local_failure'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
