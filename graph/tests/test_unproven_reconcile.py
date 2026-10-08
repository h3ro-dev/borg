"""Zero-model destructive-operation admission and recovery regressions."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_feed_recovery import backfill as b, point, Source, RecordingWriter, fake_graphiti_constructor_modules


class PhysicalGraph:
    def __init__(self, group_id, targets):
        self.driver = self
        self.records = {uuid: dict(group_id=group_id, completion_token=None,
            entity_edges=[], mentions=0, content_size=25) for uuid in targets}
        self.removed = []
        self.extra_nodes = 8
        self.relationships = 0

    async def execute_query(self, query, **params):
        if 'count(n) AS nodes' in query:
            return ([{'nodes': self.extra_nodes + len(self.records)}], None, None)
        if 'AS relationships' in query:
            return ([{'relationships': self.relationships}], None, None)
        row = self.records.get(params['episode_uuid'])
        return ([dict(row)] if row else [], None, None)

    async def remove_episode(self, uuid):
        self.removed.append(uuid)
        del self.records[uuid]


class Writer:
    def __init__(self, graph):
        self.graph = graph
        self.closed = False

    def _graph(self, group_id):
        return self.graph

    async def close(self):
        self.closed = True


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.json'
        self.map = self.root / 'scope.json'
        self.brain = self.root / 'brain.json'
        self.source = point('one', scope='team:reconcile', run_id='one')
        group = b.build_pending_groups([b.point_to_fact(self.source)], b.new_state(), b.ScopeRegistry(self.map))[0]
        self.group = group
        self.key = group['episode_id']
        self.recovery = b.recovery_episode_id_for(self.key, group['rows'])
        state = b.new_state()
        state['retry'][self.key] = dict(kind='graph', status='recovery_completion_unproven',
            retryable=False, attempts=3, recovery_attempts=1, recovery_episode_id=self.recovery,
            rows=[b._row_for_retry(row) for row in group['rows']])
        b.save_state_atomic(self.state, state)
        self.original = self.state.read_bytes()
        self.sha = hashlib.sha256(self.original).hexdigest()
        self.graph = PhysicalGraph(group['group_id'], [self.recovery, self.key])
        self.writer = Writer(self.graph)
        self.log = mock.patch.object(b, 'LOG_F', self.root / 'feed.log')
        self.log.start()
        self.addCleanup(self.log.stop)

    def run_reconcile(self, **kwargs):
        options = dict(state_path=self.state, scope_map_path=self.map, brain_state_path=self.brain,
                       expected_sha256=self.sha, writer_factory=lambda: self.writer)
        options.update(kwargs)
        return asyncio.run(b.reconcile_unproven(**options))

    def test_requeue_then_native_recovery_clears_without_resetting_counters(self):
        receipt = self.run_reconcile()
        self.assertEqual(self.graph.removed, [self.recovery, self.key])
        row = b.load_state(self.state)['retry'][self.key]
        self.assertEqual(row['status'], 'needs_native_reconciliation')
        self.assertEqual((row['attempts'], row['recovery_attempts'], row['retryable']), (3, 1, False))
        self.assertEqual(b.load_state(self.state)['graph_processed'], {})
        self.assertEqual(Path(receipt['receipt_path']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(Path(receipt['receipt_path']).read_text()), receipt)
        writer = RecordingWriter()
        result = asyncio.run(b.run_once(Source([self.source]), writer, state_path=self.state, scope_map_path=self.map))
        self.assertEqual(writer.calls[0]['episode_id'], self.recovery)
        self.assertEqual(result['pending_graph_retries'], 0)
        self.assertTrue(self.writer.closed)

    def requeued_failure(self):
        receipt = self.run_reconcile()
        state = b.load_state(self.state)
        state['retry'][self.key].update(recovery_attempts=3,
            recovery_last_error_message='ValueError: exact_identifier_source_grounding_missing')
        b.save_state_atomic(self.state, state)
        self.sha = hashlib.sha256(self.state.read_bytes()).hexdigest()
        return receipt

    def test_drop_requeued_failure_after_cleanup_preserves_rows(self):
        self.requeued_failure()
        rows = b.load_state(self.state)['retry'][self.key]['rows']
        result = self.run_reconcile(drop=True, episodes=[self.key], reason='same content error twice')
        self.assertEqual(result['removed_uuids'], [])
        entry = b.load_state(self.state)['retry'][self.key]
        self.assertEqual(entry['status'], 'operator_dropped')
        self.assertEqual(entry['rows'], rows)
        self.assertEqual(entry['recovery_attempts'], 3)

    def test_requeued_drop_requires_completed_matching_operator_receipt(self):
        receipt = self.requeued_failure()
        path = Path(receipt['receipt_path'])
        original = path.read_text()
        for changes in ({'status': 'dry_run'}, {'episodes': []}, {'action': 'drop'}):
            path.write_text(json.dumps({**json.loads(original), **changes}))
            with self.assertRaisesRegex(b.ReconcileRefused, 'no_matching_terminal_entry'):
                self.run_reconcile(drop=True, episodes=[self.key], reason='content error twice')
        path.unlink()
        with self.assertRaisesRegex(b.ReconcileRefused, 'no_matching_terminal_entry'):
            self.run_reconcile(drop=True, episodes=[self.key], reason='content error twice')

    def test_requeued_drop_still_refuses_populated_graph_and_requeue_mode(self):
        self.requeued_failure()
        with self.assertRaisesRegex(b.ReconcileRefused, 'no_matching_terminal_entry'):
            self.run_reconcile()
        self.graph.records[self.recovery] = dict(group_id=self.group['group_id'],
            completion_token='proven', entity_edges=[], mentions=0, content_size=25)
        with self.assertRaises(b.ReconcileRefused):
            self.run_reconcile(drop=True, episodes=[self.key], reason='content error twice')

    def test_dry_run_writes_only_receipt(self):
        receipt = self.run_reconcile(dry_run=True)
        self.assertEqual(receipt['status'], 'dry_run')
        self.assertEqual(self.graph.removed, [])
        self.assertEqual(self.state.read_bytes(), self.original)

    def test_refuses_all_before_removing_populated_original(self):
        self.graph.records[self.key]['mentions'] = 1
        with self.assertRaises(b.ReconcileRefused):
            self.run_reconcile()
        self.assertEqual(self.graph.removed, [])
        self.assertEqual(self.state.read_bytes(), self.original)
        self.assertTrue(self.writer.closed)

    def test_refuses_other_relationships_and_malformed_responses(self):
        self.graph.relationships = 1
        with self.assertRaises(b.ReconcileRefused):
            self.run_reconcile()
        self.graph.relationships = 0
        del self.graph.records[self.key]['group_id']
        with self.assertRaises(b.ReconcileRefused):
            self.run_reconcile()
        self.assertEqual(self.graph.removed, [])

    def test_refuses_live_brain_hash_change_and_missing_selected_entry(self):
        self.brain.write_text(json.dumps({'pid': os.getpid()}))
        with self.assertRaisesRegex(b.ReconcileRefused, 'brain_running'):
            self.run_reconcile()
        self.brain.unlink()
        with self.assertRaisesRegex(b.ReconcileRefused, 'state_hash_mismatch'):
            self.run_reconcile(expected_sha256='0' * 64)
        with self.assertRaisesRegex(b.ReconcileRefused, 'no_matching'):
            self.run_reconcile(episodes=['missing'])
        self.assertEqual(self.graph.removed, [])

    def test_identity_tampering_refused_before_any_removal(self):
        state = b.load_state(self.state)
        state['retry'][self.key]['recovery_episode_id'] = self.key
        b.save_state_atomic(self.state, state)
        with self.assertRaisesRegex(b.ReconcileRefused, 'recovery_identity'):
            self.run_reconcile(expected_sha256=hashlib.sha256(self.state.read_bytes()).hexdigest())
        self.assertEqual(self.graph.removed, [])

    def test_drop_requires_one_reason_and_excludes_pending_but_keeps_rows(self):
        with self.assertRaises(b.ReconcileRefused):
            self.run_reconcile(drop=True, reason='content error')
        self.run_reconcile(drop=True, episodes=[self.key], reason='content error')
        row = b.load_state(self.state)['retry'][self.key]
        self.assertTrue(row['rows'])
        writer = RecordingWriter()
        result = asyncio.run(b.run_once(Source([self.source]), writer, state_path=self.state, scope_map_path=self.map))
        self.assertEqual(writer.calls, [])
        self.assertEqual(result['pending_graph_retries'], 0)
        self.assertEqual(result['unresolved_reconciliation'], 0)
        self.assertEqual(result['operator_dropped_retries'], 1)
        self.assertEqual(result['retry_status_counts']['operator_dropped'], 1)
        self.assertEqual(result['outcome'], 'PASS')
        changed = point('one', scope='team:reconcile', run_id='one', text='new source revision')
        asyncio.run(b.run_once(Source([changed]), writer, state_path=self.state, scope_map_path=self.map))
        self.assertEqual(b.load_state(self.state)['retry'][self.key]['status'], 'operator_dropped')

    def test_absent_targets_allow_idempotent_retry(self):
        del self.graph.records[self.recovery]
        receipt = self.run_reconcile()
        self.assertEqual(receipt['removed_uuids'], [self.key])

    def test_state_change_after_removal_preserves_foreign_state_and_receipt(self):
        remove = self.graph.remove_episode
        async def racing(uuid):
            await remove(uuid)
            self.state.write_bytes(self.original + b'\n')
        self.graph.remove_episode = racing
        with self.assertRaisesRegex(b.ReconcileRefused, 'state_changed_after_removal'):
            self.run_reconcile()
        self.assertEqual(self.state.read_bytes(), self.original + b'\n')
        receipt = json.loads(next((self.root/'reconcile').glob('*.json')).read_text())
        self.assertEqual(receipt['status'], 'refused')
        self.assertEqual(receipt['removed_uuids'], [self.recovery, self.key])

    def test_transient_failure_cleans_stub_and_next_pass_succeeds(self):
        self.state.unlink()
        self.graph.records.clear()
        graph = self.graph
        group = self.group
        class Transient(Writer):
            attempts = 0
            async def add_episode(self, incoming):
                self.attempts += 1
                if self.attempts == 1:
                    graph.records[group['episode_id']] = dict(group_id=group['group_id'], completion_token=None,
                        entity_edges=[], mentions=0, content_size=1)
                    raise RuntimeError('temporary upstream problem')
                if group['episode_id'] in graph.records:
                    raise b.EpisodeCompletionUnproven('stale stub')
                return {'nodes': 1, 'edges': 0}
            cleanup_empty_stub = b.GraphitiWriter.cleanup_empty_stub
        writer = Transient(graph)
        first = asyncio.run(b.run_once(Source([self.source]), writer, state_path=self.state, scope_map_path=self.map))
        self.assertEqual(first['retry_count'], 1)
        self.assertEqual(graph.removed, [self.key])
        second = asyncio.run(b.run_once(Source([self.source]), writer, state_path=self.state, scope_map_path=self.map))
        self.assertEqual(second['pending_graph_retries'], 0)

    def test_error_messages_keep_first_and_chain_with_bounded_length(self):
        first = RuntimeError('first source error')
        first.__cause__ = ValueError('root cause')
        self.assertIn('ValueError: root cause', b._error_text(first))
        self.assertEqual(len(b._error_text(RuntimeError('x' * 500))), 300)
        state = b.new_state()
        counters = {'retry_count': 0, 'recovery_retry_count': 0}
        for exc in (first, b.EpisodeCompletionUnproven('stub')):
            b._apply_worker_results(state, [{'worker': 0, 'outcomes': [{'group': self.group,
                'ok': False, 'error_type': type(exc).__name__, 'error_message': b._error_text(exc)}]}], counters)
        row = state['retry'][self.key]
        self.assertIn('first source error', row['first_error_message'])
        self.assertIn('stub', row['last_error_message'])

    def test_writer_defaults_to_single_ollama_slot_and_preserves_override(self):
        with mock.patch.dict(os.environ), fake_graphiti_constructor_modules():
            os.environ.pop('SEMAPHORE_LIMIT', None)
            os.environ.pop('GRAPH_LLM_CONCURRENCY', None)
            writer = b.GraphitiWriter()
            self.assertEqual(os.environ['SEMAPHORE_LIMIT'], '1')
            asyncio.run(writer.close())
            os.environ['SEMAPHORE_LIMIT'] = '2'
            writer = b.GraphitiWriter()
            self.assertEqual(os.environ['SEMAPHORE_LIMIT'], '2')
            asyncio.run(writer.close())


if __name__ == '__main__':
    unittest.main()
