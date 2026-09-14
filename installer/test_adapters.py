import json
from pathlib import Path
import tempfile
import unittest

from installer.adapters import fetch, model_files


class AdapterTests(unittest.TestCase):
    def test_release_pins_cover_only_the_exact_model_revision(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'adapters/MANIFEST.json').read_text())
        for row in manifest['adapters']:
            files = model_files(row['release_base_pin'])
            self.assertIn('tokenizer.json', {item['path'] for item in files})
            self.assertIn('model.safetensors', {item['path'] for item in files})
            self.assertFalse(row['active'])
            pin = json.loads(json.dumps(row['release_base_pin']))
            pin['config']['url'] = 'https://other.example/config.json'
            with self.assertRaises(ValueError):
                model_files(pin)

    def test_changed_existing_weight_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'model.safetensors'
            target.write_bytes(b'owner-modified')
            with self.assertRaises(ValueError):
                fetch(target, 'https://example.test/unreachable', 'a' * 64, 3)
            self.assertEqual(target.read_bytes(), b'owner-modified')


if __name__ == '__main__':
    unittest.main()
