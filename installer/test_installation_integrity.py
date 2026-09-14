"""Installer reruns preserve changed files and reject redirected code roots."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from installer import downloads, installation
from installer.config import initialize


class InstallationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)

    def test_runtime_rerun_refuses_modified_binary_even_with_original_marker(self):
        root = self.parent / 'home'
        root.mkdir()
        archive = self.parent / 'runtime.tar.gz'
        with tarfile.open(archive, 'w:gz') as package:
            member = tarfile.TarInfo('bin/tool')
            member.size = 8
            member.mode = 0o755
            package.addfile(member, io.BytesIO(b'original'))
        spec = {'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
        with patch.object(downloads, 'download', return_value=archive), patch.object(downloads, 'artifact', return_value=(archive.name, spec)):
            dest = downloads.unpack(root, 'fixture')
            self.assertEqual(downloads.unpack(root, 'fixture'), dest)
            extra = dest / 'owner-directory'
            extra.mkdir()
            with self.assertRaises(ValueError):
                downloads.unpack(root, 'fixture')
            self.assertTrue(extra.is_dir())
            extra.rmdir()
            (dest / 'bin/tool').write_bytes(b'owner-change')
            with self.assertRaises(ValueError):
                downloads.unpack(root, 'fixture')
            self.assertEqual((dest / 'bin/tool').read_bytes(), b'owner-change')

    def test_runtime_root_cannot_redirect_to_another_home(self):
        root = self.parent / 'home'
        (root / 'runtime').mkdir(parents=True)
        foreign = self.parent / 'foreign'
        foreign.mkdir()
        (root / 'runtime/fixture').symlink_to(foreign, target_is_directory=True)
        with patch.object(downloads, 'download', return_value=self.parent / 'unused.tar.gz'), patch.object(downloads, 'artifact', return_value=('unused', {'sha256': 'a' * 64})):
            with self.assertRaises(ValueError):
                downloads.unpack(root, 'fixture')
        self.assertEqual(list(foreign.iterdir()), [])

    def test_application_rerun_rejects_additions_and_preserves_them(self):
        doc = initialize(self.parent / 'home', 'fixture-owner')
        source = self.parent / 'source'
        source.mkdir()
        (source / 'borg.py').write_text('pass\n')
        files = [source / 'borg.py']
        with patch.object(installation, 'SOURCE', source), patch.object(installation, 'source_files', return_value=files):
            digest = installation.install_sources(doc)
            self.assertEqual(installation.install_sources(doc), digest)
            extra = Path(doc['home']) / 'app/owner-plugin.py'
            extra.write_text('owner work\n')
            with self.assertRaises(RuntimeError):
                installation.install_sources(doc)
            self.assertEqual(extra.read_text(), 'owner work\n')
            extra.unlink()
            directory = Path(doc['home']) / 'app/owner-directory'
            directory.mkdir()
            with self.assertRaises(RuntimeError):
                installation.install_sources(doc)
            self.assertTrue(directory.is_dir())

    def test_application_root_symlink_is_rejected_before_copying(self):
        doc = initialize(self.parent / 'home', 'fixture-owner')
        source = self.parent / 'source'
        source.mkdir()
        (source / 'borg.py').write_text('pass\n')
        source_files = [source / 'borg.py']
        manifest = {'borg.py': hashlib.sha256(source_files[0].read_bytes()).hexdigest()}
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        foreign = self.parent / 'foreign'
        foreign.mkdir()
        (foreign / '.borg-source-sha256').write_text(digest + '\n')
        (foreign / 'borg.py').write_text('pass\n')
        (Path(doc['home']) / 'app').symlink_to(foreign, target_is_directory=True)
        with patch.object(installation, 'SOURCE', source), patch.object(installation, 'source_files', return_value=source_files):
            with self.assertRaises(ValueError):
                installation.install_sources(doc)
        self.assertEqual(set(p.name for p in foreign.iterdir()), {'borg.py', '.borg-source-sha256'})

    def test_managed_root_redirects_fail_before_bootstrap_or_dependency_mutation(self):
        script = Path(installation.__file__).parents[1] / 'install.sh'
        for index, relative in enumerate(['bootstrap', 'cache/uv', 'runtime/npm', 'runtime/browsers',
                                          'mem0/venv', 'mem0/bin', 'graphiti', 'borg-context']):
            with self.subTest(relative=relative):
                doc = initialize(self.parent / f'home-{index}', 'fixture-owner')
                root = Path(doc['home'])
                target = root / relative
                foreign = self.parent / f'foreign-{index}'
                foreign.mkdir()
                if target.exists():
                    target.rename(self.parent / f'preserved-{index}')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(foreign, target_is_directory=True)
                with patch.object(installation.dependencies, 'prepare') as prepare:
                    with self.assertRaises(ValueError):
                        installation.install(doc)
                    prepare.assert_not_called()
                run = subprocess.run(['/bin/sh', str(script), '--home', str(root)], capture_output=True, text=True, timeout=8)
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertEqual(list(foreign.iterdir()), [])

    def test_wrong_release_and_changed_component_refuse_before_prepare(self):
        doc = initialize(self.parent / 'home', 'fixture-owner')
        root = Path(doc['home'])
        source = self.parent / 'source'
        native = source / 'memory/bin/module.py'
        native.parent.mkdir(parents=True)
        native.write_text('original\n')
        (root / 'app').mkdir()
        (root / 'app/.borg-source-sha256').write_text('another-release\n')
        with patch.object(installation, 'SOURCE', source), patch.object(installation, 'source_files', return_value=[native]), patch.object(installation.dependencies, 'prepare') as prepare, patch.object(installation.config, 'write_connector_config') as configure:
            with self.assertRaises(RuntimeError):
                installation.install(doc)
            prepare.assert_not_called()
            configure.assert_not_called()
            (root / 'app/.borg-source-sha256').unlink()
            (root / 'app').rmdir()
            (root / 'mem0/bin').mkdir()
            (root / 'mem0/bin/module.py').write_text('owner-edit\n')
            with self.assertRaises(RuntimeError):
                installation.install(doc)
            prepare.assert_not_called()
            configure.assert_not_called()
            self.assertEqual((root / 'mem0/bin/module.py').read_text(), 'owner-edit\n')

    def test_existing_python_cannot_redirect_to_another_runtime(self):
        doc = initialize(self.parent / 'home', 'fixture-owner')
        target = Path(doc['home']) / 'mem0/venv/bin/python'
        target.parent.mkdir(parents=True)
        target.symlink_to(sys.executable)
        script = Path(installation.__file__).parents[1] / 'install.sh'
        run = subprocess.run(['/bin/sh', str(script), '--home', doc['home']], capture_output=True, text=True, timeout=8)
        self.assertEqual(run.returncode, 2)
        self.assertIn('Python must belong to this BORG runtime', run.stderr)


if __name__ == '__main__':
    unittest.main()
