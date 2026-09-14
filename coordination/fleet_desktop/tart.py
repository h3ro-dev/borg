"""Local, directly owned Tart/VNC actuator. No shell, host GUI, nc or public endpoints."""
from __future__ import annotations
import fcntl
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit, unquote
import uuid

from fleet_browser.supervisor import process_identity, _same, _census, _private_dir, SupervisorError
from .gateway import validate_action


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


class TartSupervisor:
    def __init__(self, state_dir, *, tart, tart_sha256, tart_home, slots, dispatch_check=None,
                 max_output_bytes=10 * 1024 * 1024):
        if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 100 * 1024 * 1024:
            raise SupervisorError('invalid_output_limit')
        self.max_output_bytes = max_output_bytes
        self.root = _private_dir(state_dir)
        self.outputs = _private_dir(self.root / 'outputs')
        self.journals = _private_dir(self.root / 'journals')
        self.tart, self.home, self.slots = str(Path(tart).resolve()), Path(tart_home).resolve(), dict(slots)
        if not Path(tart).is_absolute() or digest(self.tart) != tart_sha256:
            raise SupervisorError('binary_pin_mismatch')
        self.env = {**os.environ, 'TART_HOME': str(self.home), 'TART_NO_AUTO_PRUNE': '1'}
        self.lockfd = os.open(self.root / 'controller.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try: fcntl.flock(self.lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lockfd)
            raise SupervisorError('controller_busy') from None
        self.dispatch_check = dispatch_check
        self.guard, self.tasks = threading.RLock(), {}
        self.closed = False
        for path in self.journals.glob('*.json'):
            record = json.loads(path.read_text())
            if record.get('spawn_pending'): record['uncertain_spawn'] = True
            self.tasks[record['lease_id']] = self._task(record)
        self.stop_event = threading.Event()
        self.monitor = threading.Thread(target=self._watch, daemon=True, name='desktop-retirement')
        self.monitor.start()

    def _task(self, record):
        task = dict(record=record, gate=threading.RLock(), cleanup=threading.Lock(),
                    action=threading.Lock(), revoked=threading.Event(), processes=[], client=None, port=None)
        if record['state'] != 'active': task['revoked'].set()
        return task

    def _save(self, task):
        save(self.journals / (task['record']['lease_id'] + '.json'), task['record'])

    def _check(self, task, fence=None):
        if task['revoked'].is_set() or (time.time() >= task['record']['expires_at'] or time.monotonic() >= task['record'].get('deadline_mono', 0)):
            raise SupervisorError('desktop_revoked')
        if fence:
            fence()
        elif self.dispatch_check:
            if self.dispatch_check(task['record'].get('lease')) is not True:
                raise SupervisorError('dispatch_not_authorized')
        if task['revoked'].is_set(): raise SupervisorError('desktop_revoked')

    def _spawn(self, task, args, *, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cleanup=False):
        with task['gate']:
            if not cleanup: self._check(task)
            # Journal exists before Popen; a crash before identity capture is ambiguous on recovery.
            task['record']['spawn_pending'] = True
            self._save(task)
            p = subprocess.Popen([self.tart, *args], env=self.env, stdin=subprocess.DEVNULL,
                                 stdout=stdout, stderr=stderr, start_new_session=True)
            task['processes'].append(p)
            ident = process_identity(p.pid)
            if ident is not None: task['record']['identities'].append(ident)
            task['record']['spawn_pending'] = False
            self._save(task)
            return p

    def _run(self, task, args, timeout=30, cleanup=False):
        p = self._spawn(task, args, cleanup=cleanup)
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Do not infer cancellation. Cleanup owns the retained identity.
            raise SupervisorError('command_unknown') from None
        if p.returncode: raise SupervisorError('tart_command_failed')
        return out or b''

    def open(self, lease, options):
        slot = options['slot_id']
        cfg = self.slots[slot]
        name = 'borg-desktop-' + uuid.UUID(lease['lease_id']).hex
        record = dict(lease_id=lease['lease_id'], generation=lease['generation'], guest=name,
                      slot=slot, lease=lease, state='active', expires_at=lease['expires_at'], deadline_mono=time.monotonic()+max(0,lease['expires_at']-time.time()), identities=[],
                      spawn_pending=False, clone_started=False, guest_identity=None, receipt=None)
        record['output_bytes'] = 0
        with self.guard:
            if self.closed or lease['lease_id'] in self.tasks: raise SupervisorError('lease_fenced')
            task = self._task(record)
            self.tasks[lease['lease_id']] = task
            self._save(task)
        fence = options.get('fence')
        if fence is None and self.dispatch_check is None:
            raise SupervisorError('dispatch_authority_required')
        self._check(task, fence)
        source = self.home / 'cache/OCIs' / cfg['image'].replace('@', '/')
        if digest(source / 'config.json') != cfg['image_config_sha256']:
            raise SupervisorError('image_pin_mismatch')
        if (self.home / 'vms' / name).exists(): raise SupervisorError('guest_name_collision')
        with task['gate']:
            self._check(task, fence)
            record['clone_started'] = True
            self._save(task)
        self._run(task, ['clone', cfg['image'], name, '--concurrency', '1'], timeout=120)
        self._run(task, ['set', name, '--random-mac', '--random-serial'])
        config = json.loads((self.home / 'vms' / name / 'config.json').read_text())
        with task['gate']:
            self._check(task, fence)
            record['guest_identity'] = {'config_sha256': digest(self.home / 'vms' / name / 'config.json'),
                                        'config': config}
            self._save(task)
        master, slave = pty.openpty()
        try:
            self._check(task, fence)
            p = self._spawn(task, ['run', name, '--no-graphics', '--no-audio', '--no-clipboard', '--vnc-experimental'],
                            stdout=slave, stderr=slave)
        finally: os.close(slave)
        raw = b''
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                self._check(task, fence)
                if p.poll() is not None: raise SupervisorError('boot_failed')
                if select.select([master], [], [], .2)[0]:
                    raw += os.read(master, 4096)
                    found = re.search(rb'vnc://[^\s]+', raw)
                    if found: break
                    if len(raw) > 65536: raise SupervisorError('boot_output_bound')
            else: raise SupervisorError('boot_unknown')
            url = urlsplit(found.group().decode())
            if url.hostname != '127.0.0.1' or not url.port or not url.password:
                raise SupervisorError('invalid_private_channel')
            task['port'] = url.port
            from vncdotool import api
            from .vnc import factory
            Factory = factory(lambda: self._check(task, task.get('fence')))
            # Direct local connection. No endpoint/password is persisted or returned.
            with task['gate']:
                self._check(task, fence)
                task['fence'] = fence
                task['client'] = api.connect('127.0.0.1::' + str(url.port), unquote(url.password),
                                             factory_class=Factory, timeout=15)
            self.call(lease['lease_id'], 'screenshot', {}, 15, fence)
            return {'ready': True}
        finally:
            raw = b''
            task['pty'] = master

    def call(self, lease_id, operation, args, timeout=30, fence=None, generation=None):
        validate_action(operation, args)
        task = self.tasks[lease_id]
        if generation is not None and generation != task['record']['generation']:
            raise SupervisorError('stale_generation')
        with task['action']:
            self._check(task, fence)
            task['fence'] = fence
            client = task['client']
            if client is None: raise SupervisorError('channel_unavailable')
            client.timeout = timeout
            if operation == 'screenshot':
                from .vnc import CaptureToken
                from PIL import Image
                deadline = min(time.monotonic() + timeout, task['record'].get('deadline_mono', 0))
                remaining = self.max_output_bytes - task['record'].get('output_bytes', 0)
                if remaining <= 0: raise SupervisorError('output_limit')
                token = CaptureToken(remaining, deadline, task['revoked'].is_set)
                try:
                    client.captureScreen(token, format='PNG')
                    png = token.result()  # A stale proxy queue result cannot finish this token.
                finally:
                    token.close()  # Late Deferred callbacks retain only a closed memory sink.
                with Image.open(io.BytesIO(png)) as source:
                    width, height = source.size
                    preview = source.convert('RGB')
                    preview.thumbnail((1024, 768))
                    encoded = io.BytesIO()
                    preview.save(encoded, format='JPEG', quality=65)
                    data = encoded.getvalue()
                    if len(data) > 300 * 1024:
                        raise SupervisorError('preview_too_large')
                    visual = {'mime_type': 'image/jpeg', 'width': preview.width,
                              'height': preview.height,
                              'data': base64.b64encode(data).decode('ascii')}
                artifact = uuid.uuid4().hex + '.png'
                path = self.outputs / artifact
                # Only this synchronous gate owner can create the PNG. Cleanup
                # sets revoked before taking the same gate; callbacks cannot write files.
                with task['gate']:
                    self._check(task, fence)
                    if time.monotonic() >= deadline: raise SupervisorError('capture_cancelled')
                    total = task['record'].get('output_bytes', 0) + len(png)
                    if total > self.max_output_bytes: raise SupervisorError('output_limit')
                    # Charge durably BEFORE creation. Failure may conservatively
                    # consume quota, but cannot retain an unaccounted artifact.
                    task['record']['output_bytes'] = total
                    self._save(task)
                    owned = None
                    try:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, 'wb') as stream:
                            info = os.fstat(stream.fileno())
                            owned = (info.st_dev, info.st_ino)
                            stream.write(png)
                            stream.flush()
                            os.fsync(stream.fileno())
                        self._check(task, fence)
                        if time.monotonic() >= deadline: raise SupervisorError('capture_cancelled')
                        return {'artifact_id': artifact, 'sha256': hashlib.sha256(png).hexdigest(),
                                'width': width, 'height': height, 'preview': visual}
                    except Exception:
                        if owned is not None:
                            info = path.lstat()
                            if (info.st_dev, info.st_ino) == owned: path.unlink()
                        raise
            if operation == 'click':
                if args['x'] >= client.protocol.screen.width or args['y'] >= client.protocol.screen.height:
                    raise SupervisorError('pointer_outside_framebuffer')
                client.mouseMove(args['x'], args['y'])
                client.mousePress(1)
            elif operation == 'press': client.keyPress(args['key'])
            else: client.desktopText(args['text'])
            return {'sent': True}

    def renew(self, lease_id, expires_at):
        task = self.tasks[lease_id]
        with task['gate']:
            self._check(task)
            task['record']['expires_at'] = expires_at
            task['record']['deadline_mono'] = time.monotonic()+max(0,expires_at-time.time())
            self._save(task)

    def _terminate(self, task):
        # Stable PID identity and owned session group, never name-based killing.
        for expected in task['record']['identities']:
            current = process_identity(expected['pid'])
            if _same(expected, current):
                os.kill(expected['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for p in task['processes']: p.poll()
            if not any(_same(i, process_identity(i['pid'])) for i in task['record']['identities']): break
            time.sleep(.05)
        for expected in task['record']['identities']:
            if _same(expected, process_identity(expected['pid'])):
                os.kill(expected['pid'], signal.SIGKILL)
        for p in task['processes']:
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired: pass

    def cleanup(self, lease_id, reason):
        with self.guard:
            task = self.tasks.get(lease_id)
            if task is None:
                # Tombstone prevents a delayed open after stop. Absence isn't cleanup proof.
                record = dict(lease_id=lease_id, generation=None, guest=None, state='revoking',
                              expires_at=0, identities=[], spawn_pending=False, clone_started=False,
                              guest_identity=None, receipt=None)
                task = self.tasks.setdefault(lease_id, self._task(record))
                self._save(task)
        task['revoked'].set()  # Never waits behind action or launch locking.
        with task['cleanup']:
            record = task['record']
            if record.get('receipt') and record['receipt'].get('clean'):
                return record['receipt']
            with task['gate']:
                if record.get('spawn_pending'): record['uncertain_spawn'] = True
                record['state'] = 'revoking'
                self._save(task)
            clean, code = False, 'cleanup_unproved'
            try:
                client = task['client']
                if client and client.protocol:
                    from twisted.internet import reactor
                    reactor.callFromThread(client.protocol.transport.abortConnection)
                name = record['guest']
                guest_dir = self.home / 'vms' / name if name else None
                if guest_dir and guest_dir.exists():
                    expected = record.get('guest_identity')
                    if expected and digest(guest_dir / 'config.json') != expected['config_sha256']:
                        raise SupervisorError('guest_identity_changed')
                    # Stop is independent of the blocked VNC action queue.
                    try: self._run(task, ['stop', name], cleanup=True, timeout=15)
                    except SupervisorError: pass
                self._terminate(task)
                if task.get('pty') is not None:
                    os.close(task.pop('pty'))
                remaining = [i['pid'] for i in record['identities'] if _same(i, process_identity(i['pid']))]
                groups = {i['pgid'] for i in record['identities']}
                descendants = [pid for pid, ppid, pgid in _census() if pgid in groups and process_identity(pid)]
                if remaining or descendants or record['spawn_pending'] or record.get('uncertain_spawn'):
                    raise SupervisorError('process_cleanup_unproved')
                if guest_dir and guest_dir.exists():
                    result = json.loads(self._run(task, ['get', name, '--format', 'json'], cleanup=True))
                    if isinstance(result, list): result = result[0]
                    if str(result.get('State', result.get('state', ''))).lower() != 'stopped':
                        raise SupervisorError('guest_not_stopped')
                    self._run(task, ['delete', name], cleanup=True)
                if guest_dir and guest_dir.exists(): raise SupervisorError('guest_still_present')
                if task['port']:
                    with socket.socket() as sock:
                        sock.settimeout(.5)
                        if sock.connect_ex(('127.0.0.1', task['port'])) == 0:
                            raise SupervisorError('listener_still_live')
                remaining = [i['pid'] for i in record['identities'] if _same(i, process_identity(i['pid']))]
                if remaining: raise SupervisorError('command_still_live')
                clean, code = True, 'guest_and_channels_retired'
            except Exception as exc:
                code = exc.code if isinstance(exc, SupervisorError) else 'cleanup_unproved'
            receipt = dict(lease_id=lease_id, generation=record['generation'], guest=record['guest'],
                           clean=clean, reason_code=code, verified_at=time.time(),
                           remaining_pids=[i['pid'] for i in record['identities'] if _same(i, process_identity(i['pid']))],
                           process_identities=record['identities'], no_nc_carriers=True,
                           no_host_sharing=True, listener_retired=clean)
            record['state'], record['receipt'] = ('closed' if clean else 'quarantined'), receipt
            self._save(task)
            return receipt

    def recover(self):
        return [self.cleanup(k, 'restart') for k in list(self.tasks)]

    def _watch(self):
        while not self.stop_event.wait(.2):
            for key, task in list(self.tasks.items()):
                if task['record']['state'] == 'active' and (time.time() >= task['record']['expires_at'] or time.monotonic() >= task['record'].get('deadline_mono', 0)):
                    self.cleanup(key, 'deadline')

    def close(self):
        self.closed = True
        self.stop_event.set()
        receipts = [self.cleanup(k, 'shutdown') for k in list(self.tasks)]
        self.monitor.join(timeout=20)
        if all(r['clean'] for r in receipts) and not self.monitor.is_alive() and self.lockfd is not None:
            fcntl.flock(self.lockfd, fcntl.LOCK_UN)
            os.close(self.lockfd)
            self.lockfd = None
        return receipts
