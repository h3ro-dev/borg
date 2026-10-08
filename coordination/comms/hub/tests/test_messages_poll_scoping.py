"""Opt-in work boundaries and transactionally read-only previews."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from comms.hub.store import Store, HubError


class PollScopingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'hub.sqlite3'
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.store.bootstrap_owner('fixture-owner', 'test-approval')
        self.store.call('fixture-owner', 'agents.register', {'agent_id': 'seat-a', 'runtime': 'fixture', 'machine': 'fixture'})

    def send(self, work='X', kind='information', scope='/project'):
        return self.store.call('fixture-owner', 'messages.send', {'to': ['seat-a'], 'kind': kind,
            'work_id': work, 'scope': scope, 'subject': 'fixture', 'body': 'fixture'})['message']['id']

    def assign(self, work, scope='/project'):
        return self.store.call('fixture-owner', 'assignments.assign', {'work_id': work, 'assignee': 'seat-a',
            'scope': scope, 'summary': 'fixture assignment'})

    def poll(self, request_id=None, **params):
        return self.store.call('seat-a', 'messages.poll', params, request_id)

    def rows(self, table):
        with sqlite3.connect(self.db) as db:
            return db.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()

    def snapshot(self):
        return self.rows('deliveries'), self.rows('request_receipts')

    def update(self, sql, args=()):
        with sqlite3.connect(self.db) as db:
            db.execute(sql, args)

    def delivery(self, message):
        with sqlite3.connect(self.db) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM deliveries WHERE message_id=?', (message,)).fetchone())

    def assert_error(self, code, call):
        with self.assertRaises(HubError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)

    def test_work_filter_does_not_lease_other_work(self):
        x = self.send(); y = self.send('Y')
        result = self.poll(work_id='X')
        self.assertEqual([m['id'] for m in result['messages']], [x])
        self.assertEqual(result['poll_mode'], {'work_id': 'X', 'peek': False})
        row = self.delivery(y)
        self.assertEqual((row['state'], row['attempts'], row['last_attempt_at']), ('queued', 0, None))

    def test_filter_precedes_candidate_limit(self):
        for _ in range(105): self.send('Y')
        x = self.send()
        self.assertEqual([m['id'] for m in self.poll(work_id='X')['messages']], [x])

    def test_binding_notices_and_actor_wide_head(self):
        self.assign('X'); self.assign('Y')
        result = self.poll(work_id='X', limit=1)
        self.assertEqual([m['work_id'] for m in result['messages']], ['X'])
        self.assertTrue(result['messages'][0]['assignment_binding'])
        head = result['resume_reconciliation']['pending_instruction_head']
        self.assertNotEqual(head, result['messages'][0]['id'])
        self.assertEqual(self.delivery(head)['state'], 'queued')

    def test_default_poll_and_active_work_do_not_implicitly_filter(self):
        x = self.send(); y = self.send('Y')
        result = self.poll(active_work={'work_id': 'X'})
        self.assertEqual([m['id'] for m in result['messages']], [x, y])
        self.assertEqual(result['poll_mode'], {'work_id': None, 'peek': False})

    def test_reconcile_representations_are_scoped(self):
        self.send('X', 'instruction'); self.send('Y', 'instruction')
        self.poll()
        result = self.poll(work_id='X', reconcile=True)
        self.assertEqual([m['work_id'] for m in result['messages']], ['X'])
        self.assertTrue(result['messages'][0]['delivery_represented'])

    def test_peek_is_repeatable_without_leases_attempts_or_receipts(self):
        self.send(); self.assign('Y')
        before = self.snapshot()
        first = self.poll('peek-request', peek=True)
        second = self.poll('peek-request', peek=True)
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(all(m['peeked'] and m['delivery']['lease_id'] is None for m in first['messages']))

    def test_peek_expired_lease_then_real_poll(self):
        x = self.send(); self.poll()
        self.update('UPDATE deliveries SET lease_until=? WHERE message_id=?', (time.time()-10, x))
        before = self.snapshot()
        item = self.poll(peek=True)['messages'][0]
        self.assertEqual(item['delivery']['effective_state'], 'expired_lease')
        self.assertIsNone(item['delivery']['lease_id'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.poll()['messages'][0]['delivery']['attempts'], 2)

    def test_peek_live_instruction_requires_reconcile(self):
        self.send(kind='instruction'); self.poll()
        self.assertEqual(self.poll(peek=True)['messages'], [])
        item = self.poll(peek=True, reconcile=True)['messages'][0]
        self.assertEqual(item['delivery']['effective_state'], 'live_lease')
        self.assertIsNone(item['delivery']['lease_id'])

    def test_peek_skips_expired_message_without_marking_it(self):
        x = self.send()
        self.update("UPDATE messages SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (x,))
        before = self.snapshot()
        self.assertEqual(self.poll(peek=True)['messages'], [])
        self.assertEqual(before, self.snapshot())

    def test_peek_does_not_replay_or_conflict_with_mutation_receipt(self):
        self.send(); self.send('Y')
        first = self.poll('shared-request', work_id='X')
        before = self.snapshot()
        peek = self.poll('shared-request', work_id='Y', peek=True)
        self.assertNotEqual(first, peek)
        self.assertEqual(peek['messages'][0]['work_id'], 'Y')
        self.assertEqual(before, self.snapshot())

    def test_filtered_replay_and_conflict(self):
        self.send()
        first = self.poll('poll-request', work_id='X')
        self.assertEqual(first, self.poll('poll-request', work_id='X'))
        self.assert_error('request_conflict', lambda: self.poll('poll-request', work_id='Y'))

    def test_invalid_inputs(self):
        for value in ['', 123, 'x'*513]:
            self.assert_error('invalid_input', lambda: self.poll(work_id=value))
        self.assert_error('invalid_input', lambda: self.poll(peek='yes'))
        for value in [0, 101]:
            self.assert_error('invalid_limit', lambda: self.poll(limit=value))

    def ack(self, item, **params):
        return self.store.call('seat-a', 'messages.ack', {'message_id': item['id'],
            'lease_id': item['delivery']['lease_id'], 'state': 'acknowledged', **params})

    def test_scoped_ack_allows_foreign_binding_notice_but_default_still_blocks(self):
        self.send(kind='instruction'); item = self.poll(work_id='X')['messages'][0]
        self.assign('Y', scope='/')
        self.assert_error('instruction_reconciliation_required', lambda: self.ack(item))
        self.assertEqual(self.ack(item, work_id='X')['delivery']['state'], 'acknowledged')

    def test_scoped_ack_still_blocks_same_work(self):
        self.send(kind='instruction'); item = self.poll(work_id='X')['messages'][0]
        self.send(kind='instruction', scope='/')
        self.assert_error('instruction_reconciliation_required', lambda: self.ack(item, work_id='X'))

    def test_scoped_ack_still_blocks_unscoped_hold(self):
        self.send(kind='instruction'); item = self.poll(work_id='X')['messages'][0]
        self.send(None, 'instruction', '/')
        self.assert_error('instruction_reconciliation_required', lambda: self.ack(item, work_id='X'))

    def test_ack_work_must_match(self):
        self.send(kind='instruction'); item = self.poll(work_id='X')['messages'][0]
        self.assert_error('invalid_input', lambda: self.ack(item, work_id='Y'))


if __name__ == '__main__': unittest.main()
