import importlib.util
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('state_checkpoint', Path(__file__).resolve().parents[2] / 'installer/state_checkpoint.py')
checkpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checkpoint)


class StateCheckpointTests(unittest.TestCase):
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
