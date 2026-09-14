"""Readiness requires a completed native cycle and the correct graph shim."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from installer import brain_service as brain, health
from installer.config import initialize, write_private


class Child:
    def __init__(self, code, stopped=None):
        self.returncode = code
        self.stopped = stopped
        self.terminated = False
    def poll(self):
        if self.stopped:
            self.stopped.set()
        return self.returncode
    def terminate(self):
        self.terminated = True
        self.returncode = -15
    def wait(self, timeout=None):
        return self.returncode


class BrainHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.doc = initialize(Path(self.temp.name) / 'home', 'fixture-owner')
        self.root = Path(self.doc['home'])

    def cycle(self, codes, *, stopped=None):
        children = [Child(c) for c in codes]
        def launch(command, **kwargs):
            step = len(codes) - len(children)
            receipt = ({'schema': 'graph-feed/2', 'outcome': 'PASS', 'collection': self.doc['memory']['collection'],
                        'receipt_id': 'fixture-checkpoint', 'scan_epoch': 1, 'retry_count': 0, 'pending_graph_retries': 0}
                       if step == 0 else {'schema': 'graph-recall-projector/1', 'status': 'PASS'})
            kwargs['stdout'].write(json.dumps(receipt).encode() + b'\n')
            return children.pop(0)
        with patch.object(brain.subprocess, 'Popen', side_effect=launch), contextlib.redirect_stdout(io.StringIO()):
            return brain.run_cycle(self.root, self.doc, stopped or threading.Event())

    def test_successful_cycle_is_required(self):
        self.assertEqual(health.brain_health(self.doc, True)['state'], 'unverified')
        state = self.cycle([0, 0])
        self.assertEqual(state['exit_codes'], [0, 0])
        self.assertEqual(health.brain_health(self.doc, True)['state'], 'cycle_verified')
        self.assertEqual(health.brain_health(self.doc, False)['state'], 'stopped')

    def test_each_child_failure_cannot_be_ready(self):
        for codes in ([7], [0, 9]):
            self.cycle([0, 0])
            state = self.cycle(codes)
            self.assertEqual(state['state'], 'failed')
            self.assertNotEqual(health.brain_health(self.doc, True)['state'], 'cycle_verified')

    def test_interruption_records_failure_and_terminates_only_created_child(self):
        stopped = threading.Event()
        child = Child(None, stopped)
        with patch.object(brain.subprocess, 'Popen', return_value=child), contextlib.redirect_stdout(io.StringIO()):
            state = brain.run_cycle(self.root, self.doc, stopped)
        self.assertTrue(child.terminated)
        self.assertEqual(state['state'], 'interrupted')
        self.assertNotEqual(health.brain_health(self.doc, True)['state'], 'cycle_verified')

    def test_launch_failure_is_persisted(self):
        with patch.object(brain.subprocess, 'Popen', side_effect=OSError('fixture')), contextlib.redirect_stdout(io.StringIO()):
            state = brain.run_cycle(self.root, self.doc, threading.Event())
        self.assertEqual(state['exit_codes'], [127])
        self.assertNotEqual(health.brain_health(self.doc, True)['state'], 'cycle_verified')

    def test_stale_failed_foreign_and_interrupted_receipts_refuse(self):
        good = self.cycle([0, 0])
        path = self.root / 'graphiti/data/brain-state.json'
        for changed in [dict(good, state='failed'), dict(good, state='interrupted'),
                        dict(good, instance_id='different'), dict(good, home='/another')]:
            write_private(path, json.dumps(changed), replace=True)
            self.assertNotEqual(health.brain_health(self.doc, True)['state'], 'cycle_verified')
        write_private(path, json.dumps(good), replace=True)
        self.assertEqual(health.brain_health(self.doc, True, now=good['completed_at'] + 901)['state'], 'stale_or_failed')
        path.unlink()
        path.symlink_to(self.root / 'config.json')
        self.assertEqual(health.brain_health(self.doc, True)['state'], 'unverified')

    def test_running_cycle_keeps_only_recent_success(self):
        good = self.cycle([0, 0])
        state = dict(good, state='running', completed_at=None, exit_codes=[])
        path = self.root / 'graphiti/data/brain-state.json'
        write_private(path, json.dumps(state), replace=True)
        self.assertEqual(health.brain_health(self.doc, True)['state'], 'cycle_verified')
        self.assertEqual(health.brain_health(self.doc, True, now=state['started_at'] + brain.CYCLE_MAX_SECONDS + 1)['state'], 'stale_or_failed')

    def test_skipped_partial_failure_and_missing_native_receipts_refuse(self):
        for receipt in [{'outcome': 'NOT_RUN', 'reason': 'already-running'},
                        {'schema': 'graph-feed/2', 'outcome': 'PARTIAL', 'collection': self.doc['memory']['collection'],
                         'receipt_id': 'checkpoint', 'scan_epoch': 1, 'retry_count': 0, 'pending_graph_retries': 1},
                        {'schema': 'graph-recall-projector/1', 'status': 'NOT_RUN'}]:
            with self.assertRaises(ValueError):
                brain.step_receipt(io.BytesIO(json.dumps(receipt).encode()),
                                   'projector' if receipt.get('schema') == 'graph-recall-projector/1' else 'feed', self.doc)
        with self.assertRaises(ValueError):
            brain.step_receipt(io.BytesIO(b''), 'feed', self.doc)

    def test_graph_shim_requires_own_upstream(self):
        for response, expected in [({'ok': True, 'upstream': f"http://127.0.0.1:{self.doc['ports']['ollama']}"}, 'identity_verified'),
                                   ({'ok': True, 'upstream': 'http://foreign'}, 'identity_mismatch'),
                                   ({'ok': False}, 'identity_mismatch')]:
            with patch.object(health, 'get_json', return_value=response):
                self.assertEqual(health.graph_llm_health(self.doc)['state'], expected)
        with patch.object(health, 'get_json', side_effect=OSError('offline')):
            self.assertEqual(health.graph_llm_health(self.doc)['state'], 'unavailable')

    def test_local_wait_does_not_wait_for_provider_login(self):
        ready = {'local_services_ready': True, 'ready': False, 'state': 'provider_sign_in_required'}
        with patch.object(health, 'status', return_value=ready) as status:
            self.assertEqual(health.wait_for_local_ready(self.doc), ready)
            status.assert_called_once()
        pending = {'local_services_ready': False}
        with patch.object(health, 'status', return_value=pending) as status:
            self.assertEqual(health.wait_for_local_ready(self.doc, 0), pending)
            status.assert_called_once()


if __name__ == '__main__':
    unittest.main()
