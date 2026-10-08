import importlib.util
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
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
        destination = root / 'backups'
        destination.mkdir()
        stage = destination / 'checkpoint.stage'
        stage.mkdir()
        binary = root / 'bin'
        binary.mkdir()
        stub = binary / 'bd'
        stub.write_text('''#!/bin/sh
set -eu
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
  'init --non-interactive') test "${STUB_INIT_FAIL:-0}" = 0 || exit 9; mkdir -p "$BEADS_DIR" ;;
  'backup restore') test "$4" = --force; test -f "$3/database"; touch "$BEADS_DIR/restored" ;;
  *) exit 2 ;;
esac
''')
        stub.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {'PATH': str(binary) + os.pathsep + os.environ['PATH']}))
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


if __name__ == '__main__':
    unittest.main()
