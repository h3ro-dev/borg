import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('state_checkpoint', Path(__file__).resolve().parents[2] / 'installer/state_checkpoint.py')
checkpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checkpoint)


class StateCheckpointTests(unittest.TestCase):
    def beads_fixture(self, root):
        root = root.resolve()
        home = root / 'home'
        (home / 'beads/.beads').mkdir(parents=True)
        (home / 'beads/.beads/journal.idx').write_text('original')
        (home / 'beads/.beads/metadata.json').write_text('{"project_id": "live-project"}')
        destination = root / 'backups'
        destination.mkdir()
        stage = destination / 'checkpoint.stage'
        stage.mkdir()
        binary = home / 'runtime/beads'
        binary.mkdir(parents=True)
        stub = binary / 'bd'
        stub.write_text('''set -eu
test "$1" = --actor && test "$2" = codex-primary
shift 2
test "$BEADS_DIR" = "$PWD/.beads"
case "$1 $2" in
  'backup init') printf '%s' "$3" > "$BEADS_DIR/backup-path" ;;
  'backup sync') dest=$(cat "$BEADS_DIR/backup-path"); mkdir -p "$dest"; printf 'native database' > "$dest/database" ;;
  'export --all') printf '{"id":"one"}\n{"id":"two"}\n' > "$4" ;;
  'list --all')
    if test -f "$BEADS_DIR/restored" && test -n "${STUB_RESTORED_LIST:-}"; then
      printf '%s' "$STUB_RESTORED_LIST"
    else printf '[{"id":"one"},{"id":"two"}]'; fi ;;
  'init --non-interactive')
    test "${STUB_INIT_FAIL:-0}" = 0 || exit 9
    test -f "$BEADS_DIR/config.yaml"
    test ! -f "$BEADS_DIR/metadata.json"
    printf '{"project_id":"%s"}' "${STUB_INIT_PROJECT_ID:-fresh-project}" > "$BEADS_DIR/metadata.json"
    mkdir "$BEADS_DIR/embeddeddolt" ;;
  'backup restore') test "$4" = --force; test -f "$3/database"; touch "$BEADS_DIR/restored" ;;
  *) exit 2 ;;
esac
''')
        stub.chmod(0o700)
        (home / 'bin').mkdir()
        wrapper = home / 'bin/bd'
        wrapper.write_text('#!/bin/sh\nexec /bin/sh ' + str(root / 'wrapper.sh') + ' --bd ' + str(stub) + ' "$@"\n')
        wrapper.chmod(0o700)
        (root / 'wrapper.sh').write_text('''set -eu
shift 2
export BEADS_DIR="''' + str(home / 'beads/.beads') + '''"
cd "''' + str(home / 'beads') + '''"
if test "$3" = init; then touch "$BEADS_DIR/wrapper-init"; exit 1; fi
exec /bin/sh "''' + str(stub) + '''" "$@"
''')
        # The fixture models a native executable without requiring a compiler:
        # only its OS invocation is adapted to the shell; resolution and all
        # workspace, argv, environment and disk guards are production code.
        run = subprocess.run
        def fixture_run(argv, **kwargs):
            if argv[0] == str(stub):
                argv = ['/bin/sh', *argv]
            return run(argv, **kwargs)
        self.enterContext(patch.object(checkpoint.subprocess, 'run', side_effect=fixture_run))
        return home, destination, stage

    def test_native_beads_backup_restore_and_private_export(self):
        with tempfile.TemporaryDirectory() as temp:
            home, destination, stage = self.beads_fixture(Path(temp))
            proof = {}
            checkpoint.checkpoint_beads(home, destination, stage, proof)
            self.assertEqual(proof['beads_restore_proof'], 'PASS')
            self.assertEqual([proof[key] for key in ('beads_live_issue_count',
                             'beads_export_issue_count', 'beads_restored_issue_count')], [2, 2, 2])
            self.assertEqual(proof['beads_raw_copy'], 'stable')
            self.assertEqual((stage / '.borg/beads/export.jsonl').stat().st_mode & 0o777, 0o600)
            self.assertFalse((home / 'beads/.beads/restored').exists())
            self.assertFalse((home / 'beads/.beads/wrapper-init').exists())
            self.assertEqual(proof['beads_restore_binary'], str(home / 'runtime/beads/bd'))

    def test_native_missing_never_restores_through_wrapper(self):
        with tempfile.TemporaryDirectory() as temp:
            home, destination, stage = self.beads_fixture(Path(temp))
            # Live wrapper remains usable; only native resolution is unavailable.
            with patch.object(checkpoint, 'native_bd', return_value=None):
                proof = {}
                checkpoint.checkpoint_beads(home, destination, stage, proof)
            self.assertEqual(proof['beads_restore_reason'], 'native_binary_missing')
            self.assertFalse((home / 'beads/.beads/wrapper-init').exists())
            self.assertFalse((Path(proof['beads_restore_directory']) / '.beads/restored').exists())

    def test_matching_project_id_refuses_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            home, destination, stage = self.beads_fixture(Path(temp))
            with patch.dict(os.environ, STUB_INIT_PROJECT_ID='live-project'):
                proof = {}
                checkpoint.checkpoint_beads(home, destination, stage, proof)
            self.assertEqual(proof['beads_restore_reason'], 'isolation_unproven')
            self.assertFalse((Path(proof['beads_restore_directory']) / '.beads/restored').exists())

    def test_native_resolution_rejects_scripts_missing_and_nonexecutable(self):
        with tempfile.TemporaryDirectory() as temp:
            home, _, _ = self.beads_fixture(Path(temp))
            native = home / 'runtime/beads/bd'
            self.assertEqual(checkpoint.native_bd(home), native)
            native.chmod(0o600)
            self.assertIsNone(checkpoint.native_bd(home))
            native.chmod(0o700)
            native.write_text('#!/bin/sh\nexit 0\n')
            self.assertIsNone(checkpoint.native_bd(home))
            native.unlink()
            self.assertIsNone(checkpoint.native_bd(home))

    def test_isolated_environment_discards_all_database_overrides(self):
        with patch.dict(os.environ, BEADS_DB='live', BD_OTHER='live', DOLT_SERVER='live', KEEP_ME='yes'):
            env = checkpoint.isolated_beads_env(Path('/fixture'))
        self.assertNotIn('BEADS_DB', env)
        self.assertNotIn('BD_OTHER', env)
        self.assertNotIn('DOLT_SERVER', env)
        self.assertEqual(env['KEEP_ME'], 'yes')
        self.assertEqual(env['BEADS_DIR'], '/fixture/.beads')

    def test_changed_raw_beads_is_label_not_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            home, destination, stage = self.beads_fixture(Path(temp))
            copy = checkpoint.copy_tree
            def drift(source, target):
                copy(source, target)
                if target.name == 'raw':
                    (source / 'journal.idx').write_text('rewritten by concurrent bd')
            proof = {}
            with patch.object(checkpoint, 'copy_tree', side_effect=drift):
                checkpoint.checkpoint_beads(home, destination, stage, proof)
            self.assertEqual(proof['beads_raw_copy'], 'changed_during_copy')
            self.assertEqual(proof['beads_restore_proof'], 'PASS')

    def test_noninteractive_init_failure_records_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            home, destination, stage = self.beads_fixture(Path(temp))
            proof = {}
            with patch.dict(os.environ, STUB_INIT_FAIL='1'):
                checkpoint.checkpoint_beads(home, destination, stage, proof)
            self.assertEqual(proof['beads_restore_proof'], 'not_run')
            self.assertEqual(proof['beads_restore_reason'], 'noninteractive_init_exit_9')
            self.assertEqual(proof['beads_export_issue_count'], proof['beads_live_issue_count'])

    def test_native_restore_count_mismatch_refuses_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            home, destination, stage = self.beads_fixture(Path(temp))
            with patch.dict(os.environ, STUB_RESTORED_LIST='[]'):
                with self.assertRaisesRegex(RuntimeError, 'beads_restore_count_mismatch'):
                    checkpoint.checkpoint_beads(home, destination, stage, {})

    def test_sqlite_wal_is_restorable_without_live_sidecars(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            db = sqlite3.connect(source / 'hub.sqlite3')
            self.addCleanup(db.close)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE history (id INTEGER, body TEXT)')
            db.execute("INSERT INTO history VALUES (1, 'preserved')")
            db.commit()
            checkpoint.copy_tree(source, root / 'stage')
            self.assertFalse((root / 'stage/hub.sqlite3-wal').exists())
            with sqlite3.connect(root / 'stage/hub.sqlite3') as restored:
                self.assertEqual(restored.execute('SELECT * FROM history').fetchall(), [(1, 'preserved')])
            self.assertEqual((root / 'stage/hub.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_archive_verification_detects_corruption_and_missing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage = root / 'stage'
            stage.mkdir()
            (stage / 'state.json').write_text('{"retry":{}}')
            manifest = checkpoint.file_map(stage)
            archive = root / 'backup.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(stage / 'state.json', arcname='state.json')
            checkpoint.verify_archive(archive, manifest)
            with self.assertRaisesRegex(RuntimeError, 'archive_manifest_mismatch'):
                checkpoint.verify_archive(archive, {**manifest, 'missing': '0' * 64})
            with self.assertRaisesRegex(RuntimeError, 'archive_manifest_mismatch'):
                checkpoint.verify_archive(archive, {'state.json': '0' * 64})

    def test_copy_preserves_symlinks_without_reading_their_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            (source / 'outside').symlink_to('/unavailable/credential')
            checkpoint.copy_tree(source, root / 'stage')
            self.assertTrue((root / 'stage/outside').is_symlink())
            self.assertEqual(checkpoint.file_map(root / 'stage'), {})

    def test_graph_counts_uses_readonly_query_for_each_graph(self):
        calls = []
        class Client:
            def execute_command(self, *args):
                calls.append(args)
                if args == ('GRAPH.LIST',):
                    return [b'b', b'a']
                return [[], [[3 if args[1] == 'a' else 5]], []]
        self.assertEqual(checkpoint.graph_counts(Client()), {'a': 3, 'b': 5})
        self.assertTrue(all(c[0] == 'GRAPH.RO_QUERY' for c in calls[1:]))

    def test_stem_pattern_expands_and_names_its_family(self):
        now = time.mktime((2026, 10, 9, 3, 30, 0, 0, 0, -1))
        self.assertEqual(checkpoint.expand_stem('borg-mini-nightly-%Y%m%d-%H%M', now),
                         'borg-mini-nightly-20261009-0330')
        self.assertEqual(checkpoint.expand_stem('borg-mini-20261008-pre-drop', now), 'borg-mini-20261008-pre-drop')
        self.assertEqual(checkpoint.stem_family('borg-mini-nightly-%Y%m%d-%H%M'), 'borg-mini-nightly-')
        self.assertIsNone(checkpoint.stem_family('borg-mini-20261008-pre-drop'))
        self.assertIsNone(checkpoint.stem_family('%Y%m%d'))
        for bad in ('nested/%Y', '', '.', '%%'):
            with self.assertRaisesRegex(RuntimeError, 'invalid_stem'):
                checkpoint.expand_stem(bad, now)

    def test_retry_gate_is_an_operator_assertion_only(self):
        checkpoint.retry_gate(3, None)
        checkpoint.retry_gate(2, 2)
        with self.assertRaisesRegex(RuntimeError, 'unexpected_retry_count'):
            checkpoint.retry_gate(2, 1)

    def fake_runs(self, destination, stems, scratch=True):
        """Each stem is one run; later stems are younger archives."""
        for age, stem in enumerate(stems):
            for suffix in checkpoint.SIDECARS:
                (destination / (stem + suffix)).write_text(stem)
            when = 1_700_000_000 + age * 86400
            os.utime(destination / (stem + '.tar.gz'), (when, when))
            if scratch:
                for suffix in ('.stage', '.restore', '.stage.beads-restore-abc123'):
                    (destination / (stem + suffix)).mkdir()
                    (destination / (stem + suffix) / 'file').write_text('scratch')

    def test_retention_prunes_only_older_archives_of_the_same_family(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            nightly = [f'borg-mini-nightly-202610{day:02d}-0330' for day in (1, 2, 3, 4)]
            # Age order: a hand-made archive sharing the prefix is the oldest; an operator
            # checkpoint with another prefix sits between the nightlies and is never a member.
            self.fake_runs(destination, ['borg-mini-nightly-manual', nightly[0], nightly[1],
                                         'borg-mini-20261008-pre-drop', nightly[2], nightly[3]])
            proof = {}
            checkpoint.prune_family(destination, 'borg-mini-nightly-', 2, nightly[-1], proof)
            self.assertEqual(proof['pruned'], ['borg-mini-nightly-manual', 'borg-mini-nightly-20261001-0330',
                                               'borg-mini-nightly-20261002-0330'])
            self.assertEqual(proof['retained'], ['borg-mini-nightly-20261003-0330', 'borg-mini-nightly-20261004-0330'])
            names = sorted(p.name for p in destination.iterdir())
            self.assertFalse(any(n.startswith(('borg-mini-nightly-20261001', 'borg-mini-nightly-20261002',
                                               'borg-mini-nightly-manual')) for n in names))
            for kept in ('borg-mini-nightly-20261003-0330.tar.gz', 'borg-mini-nightly-20261004-0330.stage',
                         'borg-mini-20261008-pre-drop.tar.gz', 'borg-mini-20261008-pre-drop.stage'):
                self.assertIn(kept, names)
            with self.assertRaisesRegex(RuntimeError, 'retention_current_missing'):
                checkpoint.prune_family(destination, 'borg-mini-nightly-', 1, 'borg-mini-nightly-20261005-0330', {})
            with self.assertRaisesRegex(RuntimeError, 'retention_family_required'):
                checkpoint.prune_family(destination, None, 1, nightly[-1], {})
            with self.assertRaisesRegex(RuntimeError, 'retention_family_required'):
                checkpoint.prune_family(destination, 'borg-mini-nightly-', 0, nightly[-1], {})

    def test_retention_never_removes_the_current_run(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            self.fake_runs(destination, ['nightly-0001', 'nightly-0002', 'nightly-0003'])
            proof = {}
            checkpoint.prune_family(destination, 'nightly-', 1, 'nightly-0001', proof)
            self.assertEqual(proof['pruned'], ['nightly-0002'])
            self.assertTrue((destination / 'nightly-0001.tar.gz').exists())
            self.assertTrue((destination / 'nightly-0003.tar.gz').exists())

    def test_cleanup_removes_only_this_run_scratch(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            self.fake_runs(destination, ['nightly-0001', 'nightly-0002'])
            proof = {}
            checkpoint.remove_scratch(destination, 'nightly-0001', proof)
            self.assertEqual(proof['scratch_removed'], ['nightly-0001.stage', 'nightly-0001.restore',
                                                        'nightly-0001.stage.beads-restore-abc123'])
            names = sorted(p.name for p in destination.iterdir())
            self.assertEqual([n for n in names if n.startswith('nightly-0001')],
                             ['nightly-0001.restore-proof.json', 'nightly-0001.sha256.json', 'nightly-0001.tar.gz'])
            self.assertEqual(len([n for n in names if n.startswith('nightly-0002')]), 6)

    def test_run_lock_refuses_a_live_holder_and_replaces_a_dead_one(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            lock = checkpoint.RunLock(destination).acquire()
            with self.assertRaisesRegex(RuntimeError, 'checkpoint_already_running'):
                checkpoint.RunLock(destination).acquire()
            lock.release()
            self.assertFalse((destination / '.checkpoint.lock').exists())
            gone = subprocess.Popen(['true'])
            gone.wait()
            (destination / '.checkpoint.lock').write_text(str(gone.pid))
            other = checkpoint.RunLock(destination).acquire()
            self.assertEqual(int((destination / '.checkpoint.lock').read_text()), os.getpid())
            other.release()
            (destination / '.checkpoint.lock').write_text('not a pid')
            checkpoint.RunLock(destination).acquire().release()

    def guarded_home(self, root):
        home = root / 'home'
        (home / 'bin').mkdir(parents=True)
        (home / 'config.json').write_text('{"ports": {"graph": 1, "qdrant": 2}}')
        stub = home / 'bin/borg'
        stub.write_text('#!/bin/sh\ntouch "$(dirname "$0")/called-$1"\n')
        stub.chmod(0o700)
        return home

    def test_disk_guard_refuses_before_the_brain_stops(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = self.guarded_home(root)
            destination = root / 'backups'
            with patch.dict(sys.modules, {'redis': types.ModuleType('redis')}):
                with self.assertRaisesRegex(RuntimeError, 'disk_guard'):
                    checkpoint.checkpoint(home, destination, 'nightly-0001', 19999, min_free_gb=2 ** 40)
            self.assertFalse((home / 'bin/called-stop').exists())
            self.assertFalse((home / 'bin/called-start').exists())
            proof = json.loads((destination / 'nightly-0001.restore-proof.json').read_text())
            self.assertEqual((proof['status'], proof['error_code'], proof['stem']), ('FAIL', 'disk_guard', 'nightly-0001'))
            self.assertIn('finished_at', proof)
            self.assertFalse((destination / 'nightly-0001.stage').exists())
            self.assertFalse((destination / '.checkpoint.lock').exists())

    def test_existing_stem_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = self.guarded_home(root)
            destination = root / 'backups'
            destination.mkdir()
            (destination / 'nightly-0001.restore-proof.json').write_text('{"status": "PASS", "keep": true}')
            with patch.dict(sys.modules, {'redis': types.ModuleType('redis')}):
                with self.assertRaisesRegex(RuntimeError, 'archive_already_exists'):
                    checkpoint.checkpoint(home, destination, 'nightly-0001', 19999)
            self.assertEqual(json.loads((destination / 'nightly-0001.restore-proof.json').read_text())['keep'], True)
            self.assertFalse((home / 'bin/called-stop').exists())
            self.assertFalse((destination / '.checkpoint.lock').exists())

    def test_cli_rejects_retention_without_a_dated_stem(self):
        base = ['state_checkpoint', '--home', '.', '--destination', '.', '--restore-port', '1']
        for argv in (base + ['--stem', 'literal', '--retain', '3'],
                     base + ['--stem', 'nightly-%Y', '--retain', '0'],
                     base + ['--stem', '%Y%m%d', '--retain', '3'],
                     base + ['--stem', 'nightly-%Y', '--min-free-gb', '-1'],
                     base + ['--stem', 'nested/%Y']):
            with patch.object(sys, 'argv', argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    checkpoint.main()
                self.assertEqual(raised.exception.code, 2)

    def test_cli_expands_the_stem_and_passes_schedule_options(self):
        calls = []
        def fake(home, destination, stem, port, expected, **options):
            calls.append((stem, port, expected, options))
            return 0
        argv = ['state_checkpoint', '--home', '.', '--destination', '.', '--restore-port', '19862',
                '--stem', 'borg-mini-nightly-%Y%m%d-%H%M', '--retain', '7', '--cleanup', '--min-free-gb', '100']
        with patch.object(sys, 'argv', argv), patch.object(checkpoint, 'checkpoint', fake):
            self.assertEqual(checkpoint.main(), 0)
        stem, port, expected, options = calls[0]
        self.assertRegex(stem, r'^borg-mini-nightly-\d{8}-\d{4}$')
        self.assertEqual((port, expected), (19862, None))
        self.assertEqual(options, {'leave_brain_stopped': False, 'retain': 7, 'cleanup': True,
                                   'min_free_gb': 100, 'family': 'borg-mini-nightly-', 'busy_timeout': 900})
    def test_brain_quiet_reads_cycle_state_lock_and_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'home'
            data = home / 'graphiti/data'
            data.mkdir(parents=True)
            self.assertTrue(checkpoint.brain_quiet(home))
            state = data / 'brain-state.json'
            state.write_text(json.dumps({'state': 'running', 'pid': os.getpid()}))
            self.assertFalse(checkpoint.brain_quiet(home))
            gone = subprocess.Popen(['true'])
            gone.wait()
            state.write_text(json.dumps({'state': 'running', 'pid': gone.pid}))
            self.assertTrue(checkpoint.brain_quiet(home))
            state.write_text(json.dumps({'state': 'succeeded', 'pid': os.getpid()}))
            self.assertTrue(checkpoint.brain_quiet(home))
            (data / 'backfill.lock').write_text(str(os.getpid()))
            self.assertFalse(checkpoint.brain_quiet(home))
            (data / 'backfill.lock').unlink()
            state.write_text('not json')
            self.assertFalse(checkpoint.brain_quiet(home))
            state.write_text(json.dumps({'state': 'failed', 'pid': os.getpid()}))
            with patch.object(checkpoint, 'feed_processes', return_value=['backfill.py']):
                self.assertFalse(checkpoint.brain_quiet(home))

    def test_wait_for_quiet_brain_returns_in_the_gap_or_refuses(self):
        answers = iter([False, False, True])
        clock = iter(range(100))
        slept = []
        waited = checkpoint.wait_for_quiet_brain(Path('.'), 10, quiet=lambda home: next(answers),
                                                 clock=lambda: next(clock), sleep=slept.append, poll=0.5)
        self.assertEqual((waited, slept), (3, [0.5, 0.5]))
        clock = iter(range(100))
        with self.assertRaisesRegex(RuntimeError, 'brain_busy'):
            checkpoint.wait_for_quiet_brain(Path('.'), 3, quiet=lambda home: False,
                                            clock=lambda: next(clock), sleep=lambda s: None)

    def test_checkpoint_waits_for_the_gap_before_stopping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = self.guarded_home(root)
            (home / 'graphiti/data').mkdir(parents=True)
            (home / 'graphiti/data/brain-state.json').write_text(json.dumps({'state': 'running', 'pid': os.getpid()}))
            destination = root / 'backups'
            with patch.dict(sys.modules, {'redis': types.ModuleType('redis')}):
                with self.assertRaisesRegex(RuntimeError, 'brain_busy'):
                    checkpoint.checkpoint(home, destination, 'nightly-0001', 19999, busy_timeout=0)
            self.assertFalse((home / 'bin/called-stop').exists())
            proof = json.loads((destination / 'nightly-0001.restore-proof.json').read_text())
            self.assertEqual((proof['status'], proof['error_code']), ('FAIL', 'brain_busy'))
if __name__ == '__main__':
    unittest.main()
