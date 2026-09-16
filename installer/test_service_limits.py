from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from installer.services import install_definition


class ServiceLimitTests(unittest.TestCase):
    def test_platform_definitions_set_connector_capacity_only(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            (root / 'services').mkdir()
            spec = {'label': 'borg-test', 'args': ['/usr/bin/true'], 'env': {}, 'cwd': str(root)}
            for system in ('Darwin', 'Linux'):
                with self.subTest(system=system), patch('installer.services.platform.system', return_value=system), \
                     patch('installer.services.Path.home', return_value=root):
                    for component in ('connector', 'gateway', 'memory'):
                        target = install_definition({'home': str(root)}, component, spec)
                        if system == 'Darwin':
                            result = plistlib.loads(target.read_bytes())
                            if component == 'memory':
                                self.assertNotIn('SoftResourceLimits', result)
                            else:
                                self.assertEqual(result['SoftResourceLimits']['NumberOfFiles'], 8192)
                            self.assertNotIn('HardResourceLimits', result)
                        else:
                            self.assertNotIn('LimitNOFILE=', target.read_text())


if __name__ == '__main__':
    unittest.main()
