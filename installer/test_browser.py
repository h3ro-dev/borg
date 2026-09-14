"""Browser archive tampering and redirected installation paths fail closed."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from installer import browser


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.doc = {'home': str(self.root)}
        self.cache = self.root / 'cache/downloads'
        self.cache.mkdir(mode=0o700, parents=True)
        self.archive = self.cache / 'chromium-test-darwin-aarch64.zip'
        self.make_archive([('pkg/chrome', b'original', stat.S_IFREG | 0o755),
                           ('pkg/lib/A/data', b'framework', stat.S_IFREG | 0o644),
                           ('pkg/lib/Current', b'A', stat.S_IFLNK | 0o777)])

    def make_archive(self, rows):
        with zipfile.ZipFile(self.archive, 'w') as package:
            for name, data, mode in rows:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                package.writestr(info, data)
        self.spec = {'url': 'https://cdn.playwright.dev/builds/fixture.zip',
                     'bytes': self.archive.stat().st_size,
                     'sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest(), 'executable': 'pkg/chrome'}

    def prepare(self):
        with patch.object(browser, 'specification', return_value=('darwin-aarch64', {'revision': 'test'}, self.spec)):
            return browser.prepare(self.doc)

    def test_verified_cache_framework_links_and_unchanged_rerun(self):
        with patch.object(browser.urllib.request, 'urlopen', side_effect=AssertionError('offline cache must suffice')):
            executable = self.prepare()
            self.assertTrue(os.access(executable, os.X_OK))
            self.assertEqual((executable.parent / 'lib/Current/data').read_bytes(), b'framework')
            self.assertEqual(self.prepare(), executable)
            self.assertEqual(list((self.root / 'runtime/browsers').glob('.chromium-*')), [])

    def test_changed_installed_file_is_preserved_and_refused(self):
        executable = self.prepare()
        executable.write_bytes(b'owner change')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(executable.read_bytes(), b'owner change')

    def test_wrong_archive_hash_and_size_refuse_before_extract(self):
        for field, value in [('sha256', '0' * 64), ('bytes', 1)]:
            with self.subTest(field=field), patch.dict(self.spec, {field: value}), patch.object(browser, 'extract') as extract:
                with self.assertRaises(ValueError):
                    self.prepare()
                extract.assert_not_called()

    def test_browser_root_redirect_refuses_without_touching_other_home(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        (self.root / 'runtime').mkdir()
        (self.root / 'runtime/browsers').symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(list(foreign.iterdir()), [])

    def test_unsafe_archive_paths_links_and_special_entries_refuse(self):
        for name, data, mode in [('../escape', b'x', stat.S_IFREG | 0o644),
                                 ('pkg/link', b'../../outside', stat.S_IFLNK | 0o777),
                                 ('pkg/fifo', b'', stat.S_IFIFO | 0o600)]:
            with self.subTest(name=name):
                self.make_archive([(name, data, mode)])
                with self.assertRaises(ValueError):
                    self.prepare()
        self.assertFalse((self.root / 'outside').exists())


if __name__ == '__main__':
    unittest.main()
