"""Private, fresh per-event authority checks. No authority state or positive cache."""
import ctypes
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

from fleet_browser.supervisor import _private_dir, _same, process_identity

MAX_FRAME = 2048
READ_TIMEOUT = 1.0
MAX_WORKERS = 4
FIELDS = {'actor', 'work_id', 'lease_id', 'generation', 'work_revision', 'host'}


class FenceError(RuntimeError):
    """Only fixed error codes; never raw request or SSH exceptions."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _request(value):
    if type(value) is not dict or set(value) != FIELDS:
        raise FenceError('invalid_request')
    for key in FIELDS - {'generation'}:
        limit = 128 if key in ('work_revision', 'host') else 256
        text = value[key]
        if type(text) is not str or not 1 <= len(text.encode('utf-8')) <= limit or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise FenceError('invalid_request')
    if type(value['generation']) is not int or not 1 <= value['generation'] <= 2**63 - 1:
        raise FenceError('invalid_request')
    return dict(value)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FenceError('duplicate_field')
        result[key] = value
    return result


def _receive(connection, deadline, maximum=MAX_FRAME):
    data = bytearray()
    while len(data) <= maximum:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FenceError('read_timeout')
        connection.settimeout(remaining)
        part = connection.recv(min(512, maximum + 1 - len(data)))
        if not part:
            raise FenceError('truncated_frame')
        data.extend(part)
        if b'\n' in data:
            if not data.endswith(b'\n') or data.count(b'\n') != 1 or len(data) > maximum:
                raise FenceError('invalid_frame')
            return json.loads(data, object_pairs_hook=_pairs)
    raise FenceError('oversized_frame')


def _peer_uid(connection):
    if hasattr(socket, 'SO_PEERCRED'):
        return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if sys.platform == 'darwin':
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.CDLL(None, use_errno=True).getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise FenceError('peer_unknown')
        return uid.value
    raise FenceError('peer_unsupported')


def _socket_path(value):
    if type(value) is not str or not value.startswith('/') or '\x00' in value or ':' in value or any(c.isspace() for c in value) or len(os.fsencode(value)) > 100:
        raise FenceError('invalid_socket_path')
    path = Path(value)
    if path != path.resolve():
        raise FenceError('unsafe_socket_path')
    return path


def _socket_id(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise FenceError('unsafe_socket')
    return info.st_dev, info.st_ino


def _remove_socket(path, identity):
    current = _socket_id(path)
    if current is None:
        return
    if identity is None or current != identity:
        raise FenceError('socket_changed')
    path.unlink()


class DesktopFenceServer:
    """Call start() explicitly. Callback must be bounded, trusted and thread-safe."""
    def __init__(self, socket_path, check):
        self.path = _socket_path(socket_path)
        if not callable(check):
            raise FenceError('invalid_callback')
        self.check = check
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads = set()
        self._connections = set()
        self._listener = None
        self._acceptor = None
        self._identity = None

    def start(self):
        with self._lock:
            if self._listener is not None or self._stop.is_set():
                raise FenceError('server_state')
            _private_dir(self.path.parent)
            if os.path.lexists(self.path):
                raise FenceError('socket_collision')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(self.path))
                os.chmod(self.path, 0o600)
                self._identity = _socket_id(self.path)
                listener.listen(MAX_WORKERS)
                listener.settimeout(.1)
            except Exception:
                listener.close()
                if self._identity is not None:
                    _remove_socket(self.path, self._identity)
                raise FenceError('bind_failed') from None
            self._listener = listener
            self._acceptor = threading.Thread(target=self._accept, name='desktop-fence', daemon=True)
            self._acceptor.start()
        return self

    def _accept(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self._stop.is_set() or len(self._threads) >= MAX_WORKERS:
                    connection.close()
                    continue
                worker = threading.Thread(target=self._handle, args=(connection,), daemon=True)
                self._threads.add(worker)
                self._connections.add(connection)
                worker.start()

    def _handle(self, connection):
        deadline = time.monotonic() + READ_TIMEOUT
        allowed = False
        try:
            if _peer_uid(connection) != os.getuid():
                raise FenceError('peer_denied')
            request = _request(_receive(connection, deadline))
            allowed = self.check(request) is True
        except Exception:
            allowed = False
        try:
            if time.monotonic() >= deadline or self._stop.is_set():
                allowed = False
            connection.settimeout(.1)
            connection.sendall(b'{"allowed":true}\n' if allowed else b'{"allowed":false}\n')
        except OSError:
            pass
        finally:
            connection.close()
            with self._lock:
                self._connections.discard(connection)
                self._threads.discard(threading.current_thread())

    def close(self):
        self._stop.set()
        with self._lock:
            if self._listener is not None:
                self._listener.close()
            for connection in self._connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            threads = [self._acceptor, *self._threads]
        deadline = time.monotonic() + READ_TIMEOUT + .2
        for worker in threads:
            if worker is not None:
                worker.join(max(0, deadline - time.monotonic()))
        if any(t is not None and t.is_alive() for t in threads):
            raise FenceError('shutdown_pending')
        if self._identity is not None:
            _remove_socket(self.path, self._identity)


def _ssh_preflight(command, timeout):
    # Value-blind local expansion only: never emit/store the full SSH configuration.
    try:
        result = subprocess.run([command[0], '-G', *command[1:]],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        if result.returncode != 0 or len(result.stdout) > 262144:
            raise FenceError('ssh_config_unknown')
        for line in result.stdout.decode('utf-8').splitlines():
            key, _, value = line.partition(' ')
            if key in ('localforward', 'remoteforward', 'dynamicforward') or key == 'clearallforwardings' and value != 'no':
                raise FenceError('ssh_forward_config')
    except Exception:
        raise FenceError('ssh_config_refused') from None


class DesktopFenceClient:
    """One owned SSH command per service lifetime; each call makes a fresh query.

    macOS sshd can refuse stream-local forwarding while an authenticated command
    can reach the same private socket. The fixed helper carries only this narrow
    protocol, and its clean exit is required before its carrier guard is released.
    """
    def __init__(self, *, target, python, package_root, remote_socket, state_dir, timeout=3):
        if type(target) is not str or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,200}', target) or target.startswith('-'):
            raise FenceError('invalid_target')
        for path in (python, package_root):
            if type(path) is not str or not path.startswith('/') or '\x00' in path:
                raise FenceError('invalid_remote_path')
        remote = _socket_path(remote_socket)
        if ':' in str(remote) or any(c.isspace() for c in str(remote)):
            raise FenceError('invalid_remote_path')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 10:
            raise FenceError('invalid_timeout')
        self.timeout = timeout
        self._lock = threading.Lock()
        self._closed = False
        self._retired = False
        self.uncertain = False
        self.process = None
        self.identity = None
        self._socket_identity = None
        root = _private_dir(state_dir)
        # Validate length before creating scratch; socket names stay short on Darwin.
        _socket_path(str(root / 'df-12345678' / 's'))
        self._guard_path = root / '.fence-active'
        try:
            fd = os.open(self._guard_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise FenceError('carrier_state_exists') from None
        with os.fdopen(fd, 'w') as marker:
            info = os.fstat(marker.fileno())
            self._guard_identity = (info.st_dev, info.st_ino)
        self.directory = Path(tempfile.mkdtemp(prefix='df-', dir=root))
        self.path = _socket_path(str(self.directory / 's'))
        self._record_identity = None
        self._record('launch_intent')
        command = ['/usr/bin/ssh', '-T', '-S', 'none',
                   '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                   '-o', 'ControlMaster=no', '-o', 'ControlPersist=no',
                   '-o', 'ForkAfterAuthentication=no', '-o', 'PermitLocalCommand=no',
                   '-o', 'ProxyCommand=none', '-o', 'ProxyJump=none',
                   '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'Tunnel=no',
                   '-o', 'RemoteCommand=none', '-o', 'StreamLocalBindMask=0177', '-o', 'StreamLocalBindUnlink=no',
                   '-o', 'ConnectTimeout=2', '-o', 'ServerAliveInterval=1',
                   '-o', 'ServerAliveCountMax=1']
        try:
            _ssh_preflight([*command, target], timeout)
            helper = 'import sys;sys.path.insert(0,' + repr(package_root) + ');from fleet_desktop.fencing import serve_stdio;serve_stdio(' + repr(str(remote)) + ')'
            command.extend([target, shlex.join([python, '-c', helper])])
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
            self.identity = process_identity(self.process.pid)
            if not self.identity or any(self.identity.get(k) is None for k in ('pid', 'start', 'boot', 'executable', 'pgid')):
                raise FenceError('identity_unknown')
            self._record('starting')
            ready = _read_stdio(self.process.stdout, time.monotonic()+timeout, 64)
            if type(ready) is not dict or set(ready) != {'ready'} or ready['ready'] is not True:
                raise FenceError('carrier_failed')
            self._record('ready')
            return
        except Exception:
            try:
                self.close()
            except Exception:
                self.uncertain = True
                try:
                    self._record('uncertain')
                except Exception:
                    pass
            error = FenceError('carrier_start_failed')
            error.state_dir = str(self.directory)
            raise error from None

    def _record(self, status):
        path = self.directory / 'state.json'
        flags = os.O_WRONLY | os.O_NOFOLLOW
        if self._record_identity is None:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, 'w') as f:
            info = os.fstat(f.fileno())
            current = (info.st_dev, info.st_ino)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or self._record_identity is not None and current != self._record_identity):
                raise FenceError('record_changed')
            self._record_identity = current
            f.truncate(0)
            json.dump({'status': status, 'pid': self.process.pid if self.process else None,
                       'identity': self.identity, 'socket': str(self.path)}, f)
            f.flush()
            os.fsync(f.fileno())

    def _check_guard(self):
        info = self._guard_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != self._guard_identity):
            raise FenceError('control_changed')

    def _exchange(self, value):
        deadline = time.monotonic() + self.timeout
        frame = json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode()+b'\n'
        if len(frame) > MAX_FRAME:
            raise FenceError('oversized_frame')
        if not select.select([], [self.process.stdin], [], self.timeout)[1]:
            raise FenceError('write_timeout')
        if os.write(self.process.stdin.fileno(), frame) != len(frame):
            raise FenceError('short_write')
        return _read_stdio(self.process.stdout, deadline, 64)

    def __call__(self, lease):
        if not self._lock.acquire(timeout=self.timeout):
            return False
        try:
            if self._closed or self.uncertain or self.process.poll() is not None:
                return False
            self._check_guard()
            metadata = lease['metadata']
            request = _request({**{k: lease[k] for k in ('actor', 'work_id', 'lease_id', 'generation')},
                                'work_revision': metadata['work_revision'], 'host': metadata['host']})
            if not _same(self.identity, process_identity(self.process.pid)):
                self.uncertain = True
                self._record('uncertain')
                return False
            try:
                reply = self._exchange(request)
                if type(reply) is not dict or set(reply) != {'allowed'} or type(reply['allowed']) is not bool:
                    raise FenceError('invalid_reply')
                return reply['allowed']
            except Exception:
                # A late reply must never authorize the next, different request.
                self.uncertain = True
                self._record('uncertain')
                return False
        except Exception:
            return False
        finally:
            self._lock.release()

    def close(self):
        with self._lock:
            if self._retired:
                return
            self._closed = True
            try:
                if self.process is not None:
                    acknowledged = False
                    if self.process.poll() is None and not self.uncertain:
                        if not self.identity or not _same(self.identity, process_identity(self.process.pid)):
                            raise FenceError('identity_changed')
                        try:
                            reply = self._exchange({'close': True})
                            acknowledged = type(reply) is dict and set(reply) == {'closed'} and reply['closed'] is True
                            self.process.stdin.close()
                            self.process.wait(timeout=self.timeout)
                        except Exception:
                            pass
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        if self.process.poll() is not None:
                            break
                        if not self.identity or not _same(self.identity, process_identity(self.process.pid)):
                            raise FenceError('identity_changed')
                        os.kill(self.process.pid, sig)
                        try:
                            self.process.wait(timeout=self.timeout)
                        except subprocess.TimeoutExpired:
                            pass
                    if self.process.poll() is None:
                        raise FenceError('exit_unproved')
                    self.process.wait(timeout=0)
                    if process_identity(self.process.pid) is not None:
                        raise FenceError('pid_present')
                    # Exit zero alone after a broken stream cannot resolve a
                    # lost request; require the exact helper close exchange.
                    if not acknowledged or self.process.returncode != 0:
                        raise FenceError('helper_exit_unproved')
                    self.process.stdout.close()
                # The SSH process may have already removed its local listener.
                _remove_socket(self.path, self._socket_identity)
                if os.path.lexists(self.path):
                    raise FenceError('listener_present')
                self._record('closed')
                if os.path.lexists(self._guard_path):
                    self._check_guard()
                    self._guard_path.unlink()
                self._retired = True
            except Exception:
                self.uncertain = True
                try:
                    self._record('uncertain')
                except Exception:
                    pass
                raise FenceError('cleanup_unproved') from None


def _read_stdio(stream, deadline, maximum):
    data = bytearray()
    while len(data) <= maximum:
        remaining = deadline-time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise FenceError('read_timeout')
        part = os.read(stream.fileno(), min(512, maximum+1-len(data)))
        if not part:
            raise FenceError('truncated_frame')
        data.extend(part)
        if b'\n' in data:
            if not data.endswith(b'\n') or data.count(b'\n') != 1 or len(data) > maximum:
                raise FenceError('invalid_frame')
            return json.loads(data, object_pairs_hook=_pairs)
    raise FenceError('oversized_frame')


def serve_stdio(remote_socket):
    """Fixed SSH command: no child processes, authority state or arbitrary RPC."""
    path = _socket_path(remote_socket)
    os.write(sys.stdout.fileno(), b'{"ready":true}\n')
    while True:
        # Idle service ownership is intentional. Once a frame begins, reading
        # is bounded; EOF ends the helper and therefore the SSH command.
        select.select([sys.stdin], [], [])
        try:
            value = _read_stdio(sys.stdin, time.monotonic()+READ_TIMEOUT, MAX_FRAME)
        except Exception:
            return
        if type(value) is dict and value == {'close': True} and type(value['close']) is bool:
            os.write(sys.stdout.fileno(), b'{"closed":true}\n')
            return
        allowed = False
        try:
            request = _request(value)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(READ_TIMEOUT)
                connection.connect(str(path))
                connection.sendall(json.dumps(request, separators=(',', ':')).encode()+b'\n')
                reply = _receive(connection, time.monotonic()+READ_TIMEOUT, 64)
                allowed = type(reply) is dict and reply == {'allowed': True} and type(reply['allowed']) is bool
        except Exception:
            pass
        os.write(sys.stdout.fileno(), b'{"allowed":true}\n' if allowed else b'{"allowed":false}\n')
