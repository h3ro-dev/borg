"""Desktop specialization of the pinned browser lifecycle; no second lease store."""
import hashlib
from fleet_browser.gateway import BrowserGateway, _keys, _fingerprint
from fleet_browser.store import ResourceError, _identifier

PUBLIC = {'open', 'act', 'renew', 'close', 'status', 'stop', 'resume'}
KEYS = {'enter', 'tab', 'esc', 'backspace', 'delete', 'up', 'down', 'left', 'right',
        'home', 'end', 'space', 'shift-tab'}


def validate_action(action, args):
    if action not in {'screenshot', 'click', 'press', 'text'} or not isinstance(args, dict):
        raise ResourceError('unsupported_action', 'Unsupported desktop action')
    fields = {'screenshot': set(), 'click': {'x', 'y'}, 'press': {'key'}, 'text': {'text'}}[action]
    if set(args) != fields:
        raise ResourceError('invalid_input', 'Unexpected action arguments')
    if action == 'click' and any(type(args[k]) is not int or not 0 <= args[k] < 8192 for k in fields):
        raise ResourceError('invalid_input', 'Invalid pointer coordinates')
    if action == 'press' and (not isinstance(args['key'], str) or args['key'] not in KEYS):
        raise ResourceError('invalid_input', 'Key is not registered')
    if action == 'text' and (not isinstance(args['text'], str) or not 1 <= len(args['text']) <= 1024
                            or any(not 32 <= ord(c) <= 126 for c in args['text'])):
        raise ResourceError('invalid_input', 'Expected 1..1024 printable ASCII characters')


class DesktopGateway(BrowserGateway):
    def __init__(self, state_dir, supervisor, *, host, slots, authorize_work, work_revision,
                 operator, admission, clock=None, watchdog_interval=.2, start_watchdog=True):
        if not callable(work_revision) or not isinstance(slots, dict) or not 1 <= len(slots) <= 32:
            raise ResourceError('invalid_config', 'Revision hook and bounded fixed slots required')
        self.slots = dict(slots)
        for slot, target in self.slots.items():
            _identifier(slot, 'slot')
            if target != host:
                raise ResourceError('invalid_config', 'Desktop actuator must be local to its host')
        super().__init__(state_dir, supervisor, host=host, authorize_work=authorize_work,
                         work_revision=work_revision, operator_check=operator, admission_check=admission,
                         max_sessions=len(slots), clock=clock, watchdog_interval=watchdog_interval,
                         start_watchdog=start_watchdog)

    def call(self, actor, operation, params, request_id):
        if not isinstance(operation, str) or not operation.startswith('desktop.') or operation[8:] not in PUBLIC:
            raise ResourceError('unsupported_operation', 'Unknown desktop operation')
        return super().call(actor, 'browser.' + operation[8:], params, request_id)

    def _fence(self, lease):
        self._ensure_running()
        self._authorized(lease['actor'], lease['work_id'], lease['metadata']['work_revision'])
        current = self.store.get(lease['actor'], lease['lease_id'])
        if current['generation'] != lease['generation'] or current['status'] != 'active':
            raise ResourceError('lease_inactive', 'Desktop lease no longer active')

    def _open(self, actor, params, request_id):
        _keys(params, {'work_id', 'attempt_id', 'slot_id', 'ttl_seconds'})
        work, attempt, slot = params.get('work_id'), params.get('attempt_id'), params.get('slot_id')
        for value, name in ((work, 'work'), (attempt, 'attempt'), (slot, 'slot')):
            _identifier(value, name)
        if slot not in self.slots:
            raise ResourceError('slot_unavailable', 'Desktop slot is not registered')
        revision = self._authorized(actor, work)
        with self._admission_guard:
            self._ensure_running()
            self._authorized(actor, work, revision)
            lease = self.store.acquire(actor, work, attempt, request_id, [f'desktop/{self.host}/{slot}'],
                        params.get('ttl_seconds', 300), {'host': self.host, 'adapter': 'tart-vnc',
                        'profile_id': slot, 'work_revision': revision, 'request_fingerprint': _fingerprint(params)})
            if lease.get('replayed'):
                return {'lease': lease, 'replayed': True}
            try:
                if self.admission_check(self.host) is not True:
                    raise ValueError()
            except Exception:
                self.store.revoke(actor, lease['lease_id'], lease['generation'], 'admission_failed')
                self.store.cleanup_result(lease['lease_id'], lease['generation'], True, {'code': 'not_launched', 'clean': True})
                raise ResourceError('capacity_unavailable', 'Fresh VM-sized admission required') from None
        with self._command_lock(lease['lease_id']):
            action_id = 'open-' + hashlib.sha256(request_id.encode()).hexdigest()
            begun = False
            try:
                self._fence(lease)
                self.store.begin_action(actor, lease['lease_id'], lease['generation'], action_id, 'open',
                                        _fingerprint(slot), mutating=False)
                begun = True
                self.supervisor.open(lease, {'slot_id': slot, 'fence': lambda: self._fence(lease)})
                self._fence(lease)
                self.store.finish_action(actor, lease['lease_id'], action_id, 'completed', {'code': 'desktop_ready'})
            except Exception:
                if begun:
                    self.store.finish_action(actor, lease['lease_id'], action_id, 'failed', {'code': 'launch_failed'})
                self.store.revoke(actor, lease['lease_id'], lease['generation'], 'launch_failed')
                self._cleanup(lease, 'launch_failed')
                raise ResourceError('launch_failed', 'Desktop launch failed; inspect cleanup receipt') from None
        return {'lease': self.store.get(actor, lease['lease_id']), 'desktop_ready': True}

    def _act(self, actor, params, request_id):
        lease = self._lease(actor, params)
        action, args = params.get('action'), params.get('args', {})
        validate_action(action, args)
        timeout = params.get('timeout', 30)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise ResourceError('invalid_input', 'Timeout must be 1..30 seconds')
        with self._command_lock(lease['lease_id']):
            self._fence(lease)
            receipt = self.store.begin_action(actor, lease['lease_id'], lease['generation'], request_id,
                        action, _fingerprint([action, args, timeout]), mutating=action != 'screenshot')
            if not receipt['dispatch']:
                return {'replayed': True, 'receipt': receipt, 'result_available': False}
            try:
                result = self.supervisor.call(lease['lease_id'], action, args, timeout=timeout,
                                             fence=lambda: self._fence(lease))
            except Exception:
                outcome = 'failed' if action == 'screenshot' else 'unknown'
                self.store.finish_action(actor, lease['lease_id'], request_id, outcome, {'code': 'actuator_unconfirmed'})
                self.store.revoke(actor, lease['lease_id'], lease['generation'], 'actuator_unconfirmed')
                self._cleanup(lease, 'actuator_unconfirmed')
                raise ResourceError('action_' + outcome, 'Action unconfirmed; never replay mutation blindly') from None
            receipt = self.store.finish_action(actor, lease['lease_id'], request_id, 'completed', {'code': 'native_completed'})
            return {'receipt': receipt, 'result': result}

    def sweep(self):
        # Independent of per-lease action locks. Authorization failure is fail-closed.
        for lease in self.store.list_leases(limit=100):
            if lease['status'] == 'active':
                try:
                    self._authorized(lease['actor'], lease['work_id'], lease['metadata'].get('work_revision'))
                except ResourceError:
                    self.store.revoke(lease['actor'], lease['lease_id'], lease['generation'], 'assignment_lost')
        return super().sweep()
