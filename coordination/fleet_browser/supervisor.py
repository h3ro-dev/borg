"""Private task worker supervisor; gateway owns lease validation and fleet admission.

Operator-only launch_config keys: node_executable, worker_script, playwright_module,
chrome_executable (absolute files); profiles/auth_states (opaque ID -> absolute
path); max_tasks=1, launch_timeout=15, close_timeout=3, kill_timeout=2,
idle_timeout=60, max_line_bytes=262144, max_output_bytes=10485760.
No caller-selected commands, environment, filesystem paths or launch arguments.
A cooperative worker close is required for clean=True. Census is NOT OS
containment: unexpected death, lost protocol, and recovery remain quarantined.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid


class SupervisorError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _BSDInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
        (n, ctypes.c_uint32) for n in ('nfiles', 'pgid', 'jobc', 'tdev', 'tpgid', 'nice')
    ] + [('seconds', ctypes.c_uint64), ('microseconds', ctypes.c_uint64)]


def _boot_identity():
    if sys.platform == 'darwin':
        value = ctypes.create_string_buffer(128)
        size = ctypes.c_size_t(len(value))
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        if libc.sysctlbyname(b'kern.bootsessionuuid', value, ctypes.byref(size), None, 0):
            raise SupervisorError('identity_unavailable')
        return value.value.decode('ascii')
    if sys.platform.startswith('linux'):
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    raise SupervisorError('unsupported_platform')


_BOOT = _boot_identity()


def process_identity(pid):
    """None means confirmed absent; inaccessible/unknown never means absent."""
    if type(pid) is not int or pid <= 0:
        raise SupervisorError('invalid_pid')
    if sys.platform == 'darwin':
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        info = _BSDInfo()
        count = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if count != ctypes.sizeof(info):
            if ctypes.get_errno() == errno.ESRCH:
                return None
            raise SupervisorError('identity_unavailable')
        executable = ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(pid, executable, len(executable)) <= 0:
            # A zombie has exited, but is not proof of an entire tree's exit.
            if info.status == 5:
                return None
            again = _BSDInfo()
            count = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(again), ctypes.sizeof(again))
            if (count != ctypes.sizeof(again) and ctypes.get_errno() == errno.ESRCH) or (count == ctypes.sizeof(again) and again.status == 5):
                return None
            raise SupervisorError('identity_unavailable')
        return dict(pid=pid, start=f'{info.seconds}:{info.microseconds}', boot=_BOOT,
                    executable=os.path.realpath(os.fsdecode(executable.value)),
                    pgid=info.pgid, ppid=info.ppid)
    try:
        value = Path(f'/proc/{pid}/stat').read_text()
        fields = value[value.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return dict(pid=pid, start=fields[19], boot=_BOOT,
                    executable=os.path.realpath(os.readlink(f'/proc/{pid}/exe')),
                    pgid=int(fields[2]), ppid=int(fields[1]))
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        raise SupervisorError('identity_unavailable') from None


def _same(expected, current):
    return current is not None and all(expected.get(k) == current.get(k)
        for k in ('pid', 'start', 'boot', 'executable', 'pgid'))


def _census():
    """Read PID/parent/group only; never command lines or process environments."""
    if sys.platform == 'darwin':
        # PROC_UID_ONLY: owned cooperative browser processes retain this UID.
        # Avoid spawning ps on every sample, and don't inspect protected system PIDs.
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        size = lib.proc_listpids(4, os.getuid(), None, 0)
        if size <= 0: raise SupervisorError('census_unavailable')
        buffer = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 256))()
        count = lib.proc_listpids(4, os.getuid(), buffer, ctypes.sizeof(buffer))
        if count <= 0 or count >= ctypes.sizeof(buffer): raise SupervisorError('census_unavailable')
        rows = []
        for pid in buffer[:count // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 0: continue
            info = _BSDInfo()
            if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) == ctypes.sizeof(info):
                rows.append((pid, info.ppid, info.pgid))
            elif ctypes.get_errno() != errno.ESRCH:
                raise SupervisorError('census_unavailable')
        return rows
    rows = []
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            try:
                value = (path / 'stat').read_text()
                parts = value[value.rfind(')') + 2:].split()
                rows.append((int(path.name), int(parts[1]), int(parts[2])))
            except FileNotFoundError:
                pass
    return rows


def _private_dir(path):
    path = Path(os.path.abspath(path))
    if path != path.resolve() or path.is_symlink():
        raise SupervisorError('unsafe_path')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.stat()
    if not path.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise SupervisorError('unsafe_permissions')
    return path


class TaskSupervisor:
    def __init__(self, state_dir, launch_config):
        self.config = dict(launch_config)
        self.state_dir = _private_dir(state_dir)
        self.journal_dir = _private_dir(self.state_dir / 'journals')
        self.scratch_dir = _private_dir(self.state_dir / 'scratch')
        self.output_dir = _private_dir(self.state_dir / 'outputs')
        # Lifetime lock prevents two controllers from recovering/spawning one journal.
        self._lock_fd = os.open(self.state_dir / 'controller.lock',
                                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            raise SupervisorError('controller_busy') from None
        self._tasks = {}
        self._mutex = threading.RLock()
        self._close_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._monitor_stop = threading.Event()
        self._monitor = threading.Thread(target=self._watch, daemon=True)
        self._monitor.start()

    def prepare_boot_retirement(self, lease_id, generation, profile_id):
        from .boot_retirement import prepare
        return prepare(self, lease_id, generation, profile_id)

    def _limit(self, key, default, maximum):
        value = self.config.get(key, default)
        if type(value) not in (int, float) or not 0 < value <= maximum:
            raise SupervisorError('invalid_config')
        return value

    def _save(self, task):
        record = task['record']
        record['uncertain'] = task.get('uncertain', False)
        record['uncertainty_code'] = task.get('uncertainty_code')
        target = self.journal_dir / (task['key'] + '.json')
        temp = target.with_suffix('.tmp')
        data = json.dumps(record, separators=(',', ':')).encode()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
            directory = os.open(self.journal_dir, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        except OSError:
            raise SupervisorError('journal_failed') from None

    def _refresh_exec(self, task, expected, current):
        """Accept only a stable child image change along a still-live owned chain.

        Callers hold the task lock. Persist before changing any signal target;
        the strict _same guard remains the authority immediately before signals.
        """
        record = task['record']
        known = record['identities']
        if (expected is known[0] or task.get('uncertain') or record.get('uncertain')
                or task.get('uncertainty_code') or record.get('uncertainty_code')
                or record.get('untracked_group') or current is None
                or not current.get('executable') or current['executable'] == expected.get('executable')
                or any(expected.get(k) is None or expected[k] != current.get(k)
                       for k in ('pid', 'start', 'boot', 'ppid', 'pgid'))):
            return False
        by_pid = {i['pid']: i for i in known}
        parent = by_pid.get(expected['ppid'])
        ancestor = parent
        chain = []
        seen = {expected['pid']}
        while ancestor is not None:
            if ancestor['pid'] in seen:
                return False
            seen.add(ancestor['pid'])
            fresh = process_identity(ancestor['pid'])
            if not _same(ancestor, fresh) or fresh.get('ppid') != ancestor.get('ppid'):
                return False
            chain.append(ancestor['pid'])
            if ancestor is known[0]:
                break
            ancestor = by_pid.get(ancestor.get('ppid'))
        if ancestor is not known[0]:
            return False
        stable = process_identity(expected['pid'])
        if not _same(current, stable) or stable.get('ppid') != expected['ppid']:
            return False
        transitions = record.get('exec_transitions', [])
        if len(transitions) >= 64:
            return False  # Keep all accepted evidence; never silently trim it.
        event = dict(prior=dict(expected), current=dict(stable), parent=dict(parent),
                     verified_ancestor_pids=chain, observed_at=time.time(),
                     evidence='stable_birth_parent_group_child_reads')
        identities = [dict(stable) if i is expected else i for i in known]
        staged = dict(task, record=dict(record, identities=identities,
                                       exec_transitions=[*transitions, event]))
        self._save(staged)
        # Existing record/list references remain valid in _track and cleanup.
        index = known.index(expected)
        known[index] = identities[index]
        record['exec_transitions'] = staged['record']['exec_transitions']
        return True

    def _track(self, task):
        record = task['record']
        known = record['identities']
        if not known:
            return
        rows = _census()
        owned = {}
        for expected in list(known):
            current = process_identity(expected['pid'])
            if _same(expected, current):
                owned[expected['pid']] = expected
            elif current is not None:
                if self._refresh_exec(task, expected, current):
                    owned[expected['pid']] = next(i for i in known if i['pid'] == expected['pid'])
                else:
                    task['uncertain'] = True
                    task['uncertainty_code'] = task.get('uncertainty_code') or 'identity_changed'
                    self._save(task)
        changed = False
        pending = {pid: (parent, group) for pid, parent, group in rows
                   if pid not in {i['pid'] for i in known}}
        while True:
            candidates = [pid for pid, (parent, _) in pending.items() if parent in owned]
            if not candidates: break
            for pid in candidates:
                parent, group = pending.pop(pid)
                identity = process_identity(pid)
                if identity is None: continue
                # Census ancestry is only a candidate. PID reuse between that
                # snapshot and identity capture must never adopt a foreign PID.
                parent_now = process_identity(parent)
                child_now = process_identity(pid)
                # A fresh, stable child may have established its own process
                # group since the census. Disappearance remains uncertain:
                # the intermediary may have left detached descendants.
                if (identity['ppid'] != parent
                        or not _same(owned[parent], parent_now)
                        or not _same(identity, child_now) or child_now['ppid'] != parent):
                    task['uncertain'] = True
                    task['uncertainty_code'] = 'ancestry_changed'
                    record.setdefault('ancestry_observations', []).append({
                        'candidate': identity, 'current': child_now,
                        'expected_parent': owned[parent], 'current_parent': parent_now,
                        'census_parent': parent, 'census_group': group})
                    record['ancestry_observations'] = record['ancestry_observations'][-16:]
                    self._save(task)
                    continue
                owned[pid] = identity
                if pid not in {i['pid'] for i in known}:
                    known.append(identity)
                    changed = True
        # Same-group unknown processes are evidence, never an automatic kill target.
        record['untracked_group'] = sorted(pid for pid, _, group in rows
            if group in {i['pgid'] for i in known} and pid not in {i['pid'] for i in known})
        if changed: self._save(task)

    def _watch(self):
        while not self._monitor_stop.wait(.1):
            with self._mutex:
                tasks = list(self._tasks.values())
            for task in tasks:
                if task['lock'].acquire(blocking=False):
                    try:
                        if task['record']['status'] in ('starting', 'ready', 'closing'):
                            self._track(task)
                    except Exception as error:
                        task['uncertain'] = True
                        task['uncertainty_code'] = getattr(error, 'code', 'census_failed')
                    finally: task['lock'].release()

    def _rpc(self, task, operation, args, timeout):
        if operation != 'close' and task.get('stopping', False):
            raise SupervisorError('cancelled')
        task['in_flight'] = True
        task['rpc_operation'] = operation
        try:
            return self._rpc_impl(task, operation, args, timeout)
        finally:
            task['in_flight'] = False
            task['rpc_operation'] = None

    def _rpc_impl(self, task, operation, args, timeout):
        process = task['process']
        request_id = uuid.uuid4().hex
        task['pending_id'] = request_id
        data = json.dumps(dict(id=request_id, operation=operation, args=args,
            generation=task['record'].get('generation'), expires_at=task.get('expires_at'))).encode() + b'\n'
        maximum = int(self._limit('max_line_bytes', 262144, 1048576))
        if len(data) > maximum: raise SupervisorError('request_too_large')
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE)
                view = memoryview(data)
                while view:
                    if not selector.select(max(0, deadline-time.monotonic())):
                        raise SupervisorError('rpc_timeout')
                    if time.monotonic() >= deadline: raise SupervisorError('rpc_timeout')
                    try:
                        count = os.write(process.stdin.fileno(), view)
                        view = view[count:]
                    except BlockingIOError:
                        time.sleep(.005)
            response = self._read(task, deadline)
            if response.get('id') != request_id: raise SupervisorError('protocol_error')
            if 'error' in response: raise SupervisorError('worker_action_failed')
            if not isinstance(response.get('result'), dict): raise SupervisorError('protocol_error')
            return response['result']
        except (OSError, ValueError):
            raise SupervisorError('worker_unavailable') from None

    def _read(self, task, deadline):
        maximum = int(self._limit('max_line_bytes', 262144, 1048576))
        with selectors.DefaultSelector() as selector:
            selector.register(task['process'].stdout, selectors.EVENT_READ)
            while b'\n' not in task['buffer']:
                if task.get('cancel', threading.Event()).is_set(): raise SupervisorError('cancelled')
                self._track(task)
                left = deadline-time.monotonic()
                if left <= 0: raise SupervisorError('rpc_timeout')
                if not selector.select(min(left, .1)): continue
                chunk = os.read(task['process'].stdout.fileno(), min(65536, maximum + 1))
                if not chunk: raise SupervisorError('worker_unavailable')
                task['buffer'] += chunk
                if len(task['buffer']) > maximum: raise SupervisorError('response_too_large')
        line, task['buffer'] = task['buffer'].split(b'\n', 1)
        try:
            result = json.loads(line)
            if not isinstance(result, dict): raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise SupervisorError('protocol_error') from None

    def open(self, lease, options):
        if not isinstance(options, dict) or set(options) - {'profile_id', 'auth_ref', 'url'}:
            raise SupervisorError('invalid_options')
        lease_id = lease.get('lease_id')
        expires_at = lease.get('expires_at')
        generation = lease.get('generation')
        if type(generation) is not int or generation < 1 or type(expires_at) not in (int,float) or not time.time() < expires_at < time.time()+86400:
            raise SupervisorError('invalid_lease')
        if not isinstance(lease_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', lease_id):
            raise SupervisorError('invalid_lease_id')
        paths = {}
        for key in ('node_executable', 'worker_script', 'playwright_module', 'chrome_executable'):
            path = Path(self.config.get(key, ''))
            if not path.is_absolute() or not path.is_file(): raise SupervisorError('invalid_config')
            paths[key] = str(path.resolve())
        selected = {}
        for option, registry in (('profile_id', 'profiles'), ('auth_ref', 'auth_states')):
            if option in options:
                ref = options[option]
                if not isinstance(ref, str) or ref not in self.config.get(registry, {}):
                    raise SupervisorError('unapproved_reference')
                path = Path(self.config[registry][ref])
                if not path.is_absolute() or path != path.resolve() or not path.exists():
                    raise SupervisorError('unsafe_path')
                if path == self.state_dir or self.state_dir in path.parents or path in self.state_dir.parents:
                    raise SupervisorError('protected_path_overlap')
                selected[option] = str(path)
        if len(selected) > 1: raise SupervisorError('conflicting_auth')
        with self._mutex:
            if self._closing or self._closed: raise SupervisorError('supervisor_closed')
            from .boot_retirement import pending
            if pending(self): raise SupervisorError('boot_maintenance_pending')
            if lease_id in self._tasks: raise SupervisorError('duplicate_launch')
            for journal in self.journal_dir.glob('*.json'):
                try:
                    if journal.is_symlink() or journal.stat().st_size > 1048576: raise ValueError()
                    record = json.loads(journal.read_text())
                    if record.get('lease_id') == lease_id: raise SupervisorError('duplicate_launch')
                    if record.get('status') != 'clean' and record.get('lease_id') not in self._tasks:
                        from .boot_retirement import selected, recover
                        if not selected(self, journal, record) or recover(self, journal, record).get('clean') is not True:
                            raise SupervisorError('recovery_required')
                except (ValueError, OSError): raise SupervisorError('journal_unreadable') from None
            if sum(t['record']['status'] != 'clean' for t in self._tasks.values()) >= self._limit('max_tasks', 1, 16):
                raise SupervisorError('capacity_limit')
            key = uuid.uuid4().hex
            scratch = _private_dir(self.scratch_dir / key)
            output = _private_dir(self.output_dir / key)
            task = dict(key=key, lock=threading.RLock(), buffer=b'', uncertain=False, launch_attempted=False,
                        cancel=threading.Event(), expires_at=expires_at,
                        record=dict(lease_id=lease_id, generation=generation, nonce=key, status='intent', scratch=str(scratch),
                                    output=str(output), identities=[], untracked_group=[]))
            self._tasks[lease_id] = task
            self._save(task)  # fsync before Popen; intent-only recovery is ambiguous.
        with task['lock']:
            with self._mutex:
                if self._closing or self._closed or task.get('stopping'):
                    self.cleanup(lease_id, 'launch_cancelled')
                    raise SupervisorError('supervisor_closed')
            try:
                env = {'PATH':os.path.dirname(paths['node_executable']) + ':/usr/bin:/bin',
                       'TMPDIR':str(scratch), 'LANG':'en_US.UTF-8'}
                if 'HOME' in os.environ: env['HOME'] = os.environ['HOME']
                with self._mutex:
                    if self._closing or self._closed or task.get('stopping'):
                        self.cleanup(lease_id, 'launch_cancelled')
                        raise SupervisorError('supervisor_closed')
                    task['launch_attempted'] = True
                    process = subprocess.Popen([paths['node_executable'], paths['worker_script']],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        cwd=scratch, env=env, start_new_session=True, bufsize=0)
                    task['process'] = process
                os.set_blocking(process.stdin.fileno(), False)
                os.set_blocking(process.stdout.fileno(), False)
                identity = process_identity(process.pid)
                if not identity or identity['pgid'] != process.pid or identity['executable'] != paths['node_executable']:
                    raise SupervisorError('identity_unavailable')
                task['record']['identities'] = [identity]
                task['record']['status'] = 'starting'
                self._save(task)
                # Config travels over the private pipe, never argv or journal.
                config = dict(paths, expires_at=expires_at, generation=generation, nonce=key,
                    scratch=str(scratch), output=str(output), **selected,
                    idle_timeout=self._limit('idle_timeout', 60, 3600),
                    close_timeout=self._limit('close_timeout', 3, 30),
                    kill_timeout=self._limit('kill_timeout', 2, 10),
                    max_line_bytes=int(self._limit('max_line_bytes',262144,1048576)),
                    max_output_bytes=int(self._limit('max_output_bytes',10485760,104857600)))
                # Worker starts with a non-browser ready event; initialization launches Chrome.
                ready = self._read(task, time.monotonic()+self._limit('launch_timeout',15,120))
                if ready.get('event') != 'ready' or ready.get('pid') != process.pid:
                    raise SupervisorError('protocol_error')
                result = self._rpc(task, 'initialize', config, self._limit('launch_timeout',15,120))
                self._track(task)
                browser_pid = result.get('browser_pid')
                if browser_pid is not None and browser_pid not in {i['pid'] for i in task['record']['identities']}:
                    raise SupervisorError('browser_identity_unavailable')
                if options.get('url'):
                    self._rpc(task, 'open', {'url':options['url']}, self._limit('launch_timeout',15,120))
                task['record']['status'] = 'ready'
                self._save(task)
                return dict(lease_id=lease_id, status='ready', pid=process.pid)
            except Exception as error:
                if task['record']['status'] == 'clean':
                    raise SupervisorError('supervisor_closed') from None
                task['uncertain'] = True
                task['record']['status'] = 'quarantined'
                self._save(task)
                self.cleanup(lease_id, 'launch_failed')
                if isinstance(error, SupervisorError): raise
                raise SupervisorError('launch_failed') from None

    def call(self, lease_id, operation, args, timeout=30):
        if operation not in {'open','navigate','snapshot','click','fill','press','select','screenshot'}:
            raise SupervisorError('unsupported_operation')
        if not isinstance(args, dict) or type(timeout) not in (int,float) or not 0 < timeout <= 120:
            raise SupervisorError('invalid_request')
        task = self._tasks.get(lease_id)
        if not task: raise SupervisorError('unknown_lease')
        with task['lock']:
            if self._closed or self._closing: raise SupervisorError('supervisor_closed')
            if task['record']['status'] != 'ready' or task.get('stopping') or task['cancel'].is_set(): raise SupervisorError('task_not_ready')
            if time.time() >= task['expires_at']: raise SupervisorError('lease_expired')
            try: return self._rpc(task, operation, args, timeout)
            except SupervisorError as error:
                # Validation/action errors have a synchronized response; transport loss doesn't.
                if error.code != 'worker_action_failed':
                    task['uncertain'] = True
                    task['record']['status'] = 'quarantined'
                    self._save(task)
                raise

    def renew(self, lease_id, expires_at):
        task = self._tasks.get(lease_id)
        if not task: raise SupervisorError('unknown_lease')
        with task['lock']:
            if self._closed or self._closing: raise SupervisorError('supervisor_closed')
            if task['record']['status'] != 'ready' or task.get('stopping') or task['cancel'].is_set() or time.time() >= task['expires_at']:
                raise SupervisorError('lease_expired')
            if type(expires_at) not in (int,float) or not time.time() < expires_at < time.time()+86400:
                raise SupervisorError('invalid_lease')
            try:
                result = self._rpc(task, 'renew', {'expires_at':expires_at}, 3)
                task['expires_at'] = expires_at
                return result
            except SupervisorError:
                task['uncertain'] = True
                task['record']['status'] = 'quarantined'
                self._save(task)
                raise

    def _alive(self, task):
        process = task.get('process')
        if process: process.poll()  # reap owned child before identity inspection
        alive, mismatch = [], False
        for expected in task['record']['identities']:
            try:
                current = process_identity(expected['pid'])
                if current is not None and not _same(expected, current):
                    if self._refresh_exec(task, expected, current):
                        expected = next(i for i in task['record']['identities'] if i['pid'] == expected['pid'])
                    else:
                        task['uncertain'] = True
                        task['uncertainty_code'] = task.get('uncertainty_code') or 'identity_changed'
                        self._save(task)
            except SupervisorError:
                mismatch = True
                alive.append(expected['pid'])
                continue
            if current is not None:
                alive.append(expected['pid'])
                if not _same(expected, current): mismatch = True
        return alive, mismatch

    def _shutdown_proof(self, task):
        """A private, task-bound worker receipt survives controller/pipe loss.

        This is the same cooperative boundary as a close RPC acknowledgement.
        It never supplies PIDs to signal, or proves containment of arbitrary code.
        """
        record = task['record']
        if not record.get('identities') or record.get('untracked_group'):
            return False
        path = Path(record['scratch']) / 'worker-shutdown.json'
        try:
            expected = self.scratch_dir / task['key']
            if Path(record['scratch']) != expected or expected.is_symlink() or expected.resolve() != expected:
                return False
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
                    return False
                proof = json.loads(stream.read(16385))
            if (proof.get('phase') != 'closed' or proof.get('nonce') != record.get('nonce')
                    or proof.get('generation') != record.get('generation')
                    or proof.get('pid') != record['identities'][0]['pid']):
                return False
            children = proof.get('children')
            if not isinstance(children, list) or len(children) > 32:
                return False
            return all(isinstance(c, dict) and type(c.get('pid')) is int
                       and c['pid'] > 0 and c.get('exited') is True
                       and process_identity(c['pid']) is None for c in children)
        except (OSError, ValueError, SupervisorError, AttributeError, TypeError):
            return False

    def _terminal_receipt(self, lease_id):
        # Recover only the receipt requested by the gateway. Sending every
        # historical clean journal would eventually overflow the wire bound.
        found = None
        for path in self.journal_dir.glob('*.json'):
            try:
                if path.is_symlink() or path.stat().st_size > 1048576:
                    continue
                record = json.loads(path.read_text())
                if record.get('lease_id') != lease_id:
                    continue
                from .boot_retirement import selected, recover
                if selected(self, path, record):
                    return recover(self, path, record)
                receipt = record.get('receipt', {})
                if (found is not None or record.get('status') != 'clean'
                        or receipt.get('lease_id') != lease_id or receipt.get('clean') is not True):
                    raise SupervisorError('terminal_receipt_unproved')
                found = receipt
            except (ValueError, OSError, AttributeError):
                continue
        return found

    def cleanup(self, lease_id, reason):
        with self._mutex:
            if self._closed: raise SupervisorError('supervisor_closed')
        task = self._tasks.get(lease_id)
        if not task:
            prior = self._terminal_receipt(lease_id)
            if prior is not None:
                return prior
            return dict(lease_id=lease_id, clean=False, status='quarantined', reason_code='unknown_lease', remaining_pids=[])
        task['stopping'] = True
        # Cancellation precedes the RPC lock: a revoked auto-wait must not dispatch later.
        if ((task.get('in_flight') and task.get('rpc_operation') != 'close')
                or task['record']['status'] == 'starting'):
            task.setdefault('cancel', threading.Event()).set()
            task['uncertain'] = True
            identities = task['record'].get('identities', [])
            if identities:
                try:
                    if _same(identities[0], process_identity(identities[0]['pid'])):
                        os.kill(identities[0]['pid'], signal.SIGTERM)
                except (OSError, SupervisorError): pass
        with task['lock']:
            if self._closed: raise SupervisorError('supervisor_closed')
            record = task['record']
            if record['status'] == 'clean': return record['receipt']
            acknowledged = record.get('close_acknowledged') is True
            # Only a live intent that never reached Popen can prove non-launch.
            # A journal recovered after a crash has no such in-memory evidence.
            not_launched = (task.get('launch_attempted') is False and not task.get('process')
                            and not record['identities'] and not task['uncertain'])
            escalated = False
            alive, mismatch = self._alive(task)
            if not acknowledged and not mismatch and task.get('process') and not task['uncertain']:
                try:
                    self._track(task)
                    record['status'] = 'closing'
                    result = self._rpc(task, 'close', {}, self._limit('close_timeout',3,30))
                    acknowledged = result.get('closed') is True and result.get('browser_exited') is True
                except Exception as error:
                    # Closing is not an action retry. Keep the trusted worker alive
                    # through matched browser termination so it can attest exit.
                    if isinstance(error, SupervisorError) and error.code == 'rpc_timeout':
                        try:
                            for browser_signal in (signal.SIGTERM, signal.SIGKILL):
                                self._track(task)
                                _, changed = self._alive(task)
                                if changed: raise SupervisorError('identity_mismatch')
                                for expected in reversed(record['identities'][1:]):
                                    if _same(expected, process_identity(expected['pid'])):
                                        os.kill(expected['pid'], browser_signal)
                                        record.setdefault('signals_sent', []).append({'pid':expected['pid'],'signal':browser_signal.name})
                                escalated = True
                                try:
                                    response = self._read(task, time.monotonic()+self._limit('kill_timeout',2,10))
                                except SupervisorError as pending_error:
                                    if pending_error.code == 'rpc_timeout' and browser_signal == signal.SIGTERM:
                                        continue
                                    raise
                                result = response.get('result', {})
                                acknowledged = (response.get('id') == task.get('pending_id')
                                    and result.get('closed') is True and result.get('browser_exited') is True)
                                if not acknowledged: raise SupervisorError('close_unacknowledged')
                                break
                        except Exception as close_error:
                            task['uncertain'] = True
                            task['uncertainty_code'] = getattr(close_error, 'code', 'close_failed')
                    else:
                        task['uncertain'] = True
                        task['uncertainty_code'] = getattr(error, 'code', 'close_failed')
            if acknowledged:
                record['close_acknowledged'] = True
                self._save(task)
            for sig in (None, signal.SIGTERM, signal.SIGKILL):
                alive, changed = self._alive(task)
                mismatch |= changed
                if not alive or mismatch: break
                if sig:
                    for expected in reversed(record['identities']):
                        try:
                            if _same(expected, process_identity(expected['pid'])):
                                os.kill(expected['pid'], sig)
                                record.setdefault('signals_sent', []).append({'pid':expected['pid'],'signal':sig.name})
                        except ProcessLookupError: pass
                        except (OSError, SupervisorError): mismatch = True
                deadline = time.monotonic()+self._limit('kill_timeout',2,10)
                while time.monotonic() < deadline:
                    alive, changed = self._alive(task)
                    mismatch |= changed
                    if not alive or mismatch: break
                    time.sleep(.02)
            alive, changed = self._alive(task)
            mismatch |= changed
            try: self._track(task)
            except Exception as error:
                task['uncertain'] = True
                task['uncertainty_code'] = getattr(error, 'code', 'census_failed')
            alive, changed = self._alive(task)
            mismatch |= changed
            if (not alive and not mismatch and not record.get('untracked_group')
                    and task.get('uncertainty_code') in (None, 'rpc_timeout', 'worker_unavailable',
                                                       'cancelled', 'close_failed', 'close_unacknowledged')
                    and self._shutdown_proof(task)):
                acknowledged = True
                task['uncertain'] = False
                task['uncertainty_code'] = None
                record['close_acknowledged'] = True
            clean = ((acknowledged or not_launched) and not task['uncertain'] and not alive and not mismatch
                     and not record.get('untracked_group'))
            if clean:
                scratch = Path(record['scratch'])
                expected = self.scratch_dir / task['key']
                if scratch != expected or scratch.is_symlink() or scratch.resolve() != expected:
                    clean = False
                else:
                    try:
                        shutil.rmtree(scratch)
                        clean = not scratch.exists()
                    except OSError: clean = False
            code = 'exit_verified' if clean else ('identity_mismatch' if mismatch else 'containment_unproved')
            receipt = dict(lease_id=lease_id, clean=bool(clean), status='clean' if clean else 'quarantined',
                           reason_code=code, remaining_pids=sorted(set(alive)),
                           uncertainty_code=task.get('uncertainty_code'), close_acknowledged=acknowledged,
                           not_launched=not_launched,
                           browser_term_escalated=escalated,
                           untracked_group=record.get('untracked_group', []), signals_sent=record.get('signals_sent', []))
            record.update(status=receipt['status'], receipt=receipt)
            self._save(task)
            if task.get('process') and task['process'].poll() is not None:
                task['process'].stdin.close()
                task['process'].stdout.close()
            return receipt

    def recover(self):
        # Recovery may discover new journals. Hold the lifetime transition lock
        # through the complete read/cleanup so a retired instance can never
        # discover or signal its successor's resources.
        with self._close_lock:
            if self._closed or self._closing: raise SupervisorError('supervisor_closed')
            return self._recover()

    def _recover(self):
        receipts = []
        for path in sorted(self.journal_dir.glob('*.json')):
            try:
                if path.is_symlink() or path.stat().st_size > 1048576: raise ValueError()
                record = json.loads(path.read_text())
                if record.get('status') == 'clean': continue
                from .boot_retirement import selected, recover
                if selected(self, path, record):
                    receipts.append(recover(self, path, record))
                    continue
                lease_id = record['lease_id']
                if lease_id not in self._tasks:
                    uncertainty = record.get('uncertainty_code') or record.get('receipt', {}).get('uncertainty_code')
                    self._tasks[lease_id] = dict(key=path.stem, record=record,
                        lock=threading.RLock(), buffer=b'', uncertainty_code=uncertainty,
                        uncertain=(record.get('uncertain') is True or uncertainty is not None
                                   or record.get('close_acknowledged') is not True))
                    # Recovered identities may be signaled only when every identity matches.
                receipts.append(self.cleanup(lease_id, 'recovery'))
            except (ValueError, KeyError, OSError, SupervisorError):
                receipts.append(dict(clean=False,status='quarantined',reason_code='journal_unreadable',remaining_pids=[]))
        return receipts

    def close(self):
        with self._close_lock:
            with self._mutex:
                if self._closed: return []
                self._closing = True
                keys = list(self._tasks)
            self._monitor_stop.set()
            self._monitor.join(timeout=3)
            # Retain the lifetime lock if cleanup raises; a later close retries.
            receipts = [self.cleanup(key, 'supervisor_close') for key in keys]
            from .boot_retirement import close_receipts
            receipts.extend(close_receipts(self))
            if any(receipt.get('clean') is not True for receipt in receipts):
                return receipts
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._closed = True
            return receipts
