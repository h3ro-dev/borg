"""Run against reviewed site candidates: BORG_SITE_PATCH_FIXTURE=<directory>.

No native services are called. Fleet calls are mocked; brain readers run only
against a temporary synthetic installation. The fixture holds dot_inbox_bridge.py,
mailbox_guard.py and live.py after applying the accompanying versioned patch.
"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

FIXTURE = os.environ.get('BORG_SITE_PATCH_FIXTURE')


@unittest.skipUnless(FIXTURE, 'Set BORG_SITE_PATCH_FIXTURE to reviewed site candidates')
class SitePatchTests(unittest.TestCase):
    def module(self, name):
        spec = importlib.util.spec_from_file_location(name, Path(FIXTURE) / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_dot_native_call_accepts_only_current_worker_identity(self):
        module = self.module('dot_inbox_bridge')
        expected = dict(instance_id=os.environ.get('BORG_SITE_WORKER_INSTANCE', 'fixture-worker'),
                        owner=os.environ.get('BORG_SITE_OWNER', 'fixture-owner'),
                        home=os.environ.get('BORG_SITE_WORKER_HOME', '/fixture/worker'))
        with tempfile.TemporaryDirectory() as temp:
            box = module.FleetMailbox(Path(temp))
            for identity, accepted in [(expected, True),
                    ({**expected, 'instance_id': os.environ.get('BORG_SITE_RETIRED_INSTANCE', 'fixture-retired')}, False),
                    ({**expected, 'home': os.environ.get('BORG_SITE_RETIRED_HOME', '/fixture/retired')}, False),
                    ({**expected, 'owner': 'other'}, False)]:
                response = dict(_meta={'borg_fleet': {'identity': identity}}, structuredContent={'ok': True})
                with patch.object(module.subprocess, 'run', return_value=types.SimpleNamespace(
                        returncode=0, stdout=json.dumps(response))) as call:
                    if accepted:
                        self.assertEqual(box.call('computer_read_file', {'path': 'fixture'}), {'ok': True})
                    else:
                        with self.assertRaisesRegex(ValueError, 'fleet_identity_changed'):
                            box.call('computer_read_file', {'path': 'fixture'})
                    args = call.call_args.args[0]
                    self.assertEqual(json.loads(args[args.index('--arguments') + 1])['host'], 'laptop-worker')

    def test_mailbox_default_moves_without_relaxing_file_confinement(self):
        guard = self.module('mailbox_guard')
        self.assertEqual(guard.ROOT, os.environ.get('BORG_SITE_WORKER_HOME', '/fixture/worker') + '/projects/dot-mailbox')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / 'outside'
            outside.mkdir()
            (root / 'linked').symlink_to(outside)
            with self.assertRaises((ValueError, OSError)):
                guard.Mailbox(root / 'linked').directory()

    def test_both_brain_readers_exclude_only_dropped_and_preserve_state(self):
        tree = ast.parse((Path(FIXTURE) / 'live.py').read_text())
        scripts = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ('_brain_status', '_laptop_brain'):
                assignment = next(x for x in node.body if isinstance(x, ast.Assign)
                                  and any(isinstance(t, ast.Name) and t.id == 'script' for t in x.targets))
                value = assignment.value
                scripts[node.name] = value.func.value.value if isinstance(value, ast.Call) else value.value
        self.assertEqual(len(scripts), 2)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'app/installer').mkdir(parents=True)
            (root / 'graphiti/data').mkdir(parents=True)
            files = {
                '__init__.py': '',
                'config.py': 'import json\ndef load(root): return json.loads((root/"config.json").read_text())\ndef read_private(path): return json.loads(path.read_text())\n',
                'health.py': 'def brain_health(doc, run): return {"state":"cycle_verified", "started_at":1}\n',
                'services.py': 'def running(doc, name): return True\n',
                'brain_service.py': 'CYCLE_MAX_SECONDS=810\nSUCCESS_MAX_AGE_SECONDS=900\n',
                'blueprint.py': 'def full(doc): return True\n',
            }
            for name, content in files.items():
                (root / 'app/installer' / name).write_text(content)
            (root / 'config.json').write_text(json.dumps({'instance_id': 'fixture', 'home': str(root)}))
            (root / 'graphiti/data/brain-state.json').write_text(json.dumps({
                'schema': 'borg-brain-cycle/v1', 'instance_id': 'fixture', 'home': str(root),
                'state': 'succeeded', 'started_at': 1, 'completed_at': 2, 'exit_codes': [0, 0],
                'last_success': {'completed_at': 2, 'exit_codes': [0, 0], 'steps': [{}, {}]}}))
            state = root / 'graphiti/data/backfill-state.json'
            for retry, pending_count in [({'d': {'status': 'operator_dropped'}, 'p': {'status': 'needs_native_reconciliation'}}, 1),
                                         ({'d': {'status': 'operator_dropped'}}, 0),
                                         ({'bad': None}, 1)]:
                state.write_text(json.dumps({'retry': retry, 'projection_pending': {}}))
                state.chmod(0o600)
                before = state.read_bytes()
                for name, script in scripts.items():
                    script = script.replace('ROOT_VALUE', json.dumps(str(root)))
                    run = subprocess.run([sys.executable, '-B', '-c', script, str(root)],
                                         capture_output=True, text=True, check=True)
                    result = json.loads(run.stdout)
                    if name == '_laptop_brain':
                        result = result['brain']
                    self.assertEqual(result['retry_backlog'], pending_count)
                    self.assertEqual(result['operator_dropped_retries'], int('d' in retry))
                    self.assertEqual(state.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
