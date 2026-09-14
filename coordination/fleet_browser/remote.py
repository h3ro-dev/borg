"""Private supervisor transport over the estate's existing SSH principal.

The authenticated Inbox gateway owns authorization and resource state. This
module only moves the bounded supervisor calls to an admitted browser host.
It creates no public browser endpoint or additional credential database.
Same-user shell access remains an explicitly cooperative trust boundary.

Wire protocol v2: recover negotiates a service incarnation and one admission
ticket, then binds the client's UUID epoch. Keep each RemoteSupervisor instance
with exactly one gateway; never create a new binding to retry an unknown action.
Ordinary close leaves this service resident. Production embedding must provide
supervisor_factory for successor epochs; the CLI serve path supplies it. Failed
cleanup receipts block succession, including when a later close returns [].
Service shutdown uses server_close after stopping serve_forever, and closes the
current factory instance rather than a captured predecessor. Terminal receipts
are memory-only (32 epochs); a service restart deliberately invalidates clients.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import socket
import socketserver
import stat
import subprocess
import threading
import time
import uuid


MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 512 * 1024
METHODS = {"open", "call", "renew", "cleanup", "recover", "close"}


class RemoteError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _frame(value, maximum):
    try:
        data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    except (TypeError, ValueError, UnicodeError):
        raise RemoteError("invalid_frame") from None
    if len(data) > maximum:
        raise RemoteError("frame_too_large")
    return data


def _read_frame(stream, maximum):
    raw = stream.readline(maximum + 1)
    if not raw.endswith(b"\n") or len(raw) > maximum:
        raise RemoteError("invalid_frame")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise RemoteError("invalid_frame") from None


class RemoteSupervisor:
    """Fixed operator-configured SSH target; never caller-provided commands."""

    def __init__(self, *, target, python, package_root, socket_path, timeout=75):
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,200}", target) or target.startswith("-"):
            raise RemoteError("invalid_target")
        for value in (python, package_root, socket_path):
            if not isinstance(value, str) or not Path(value).is_absolute() or "\x00" in value:
                raise RemoteError("invalid_remote_path")
        if type(timeout) not in (int, float) or not 10 <= timeout <= 120:
            raise RemoteError("invalid_timeout")
        self.timeout = timeout
        self.epoch = uuid.uuid4().hex
        self._binding = None
        self._binding_lock = threading.Lock()
        remote = ["env", "PYTHONPATH=" + package_root, python, "-m", "fleet_browser.remote",
                  "request", "--socket", socket_path, "--timeout", str(timeout)]
        self.command = ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                        target, shlex.join(remote)]

    def _exchange(self, data):
        """Bound stdout during capture, and always reap the owned SSH process."""
        process = None
        try:
            process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, bufsize=0)
            deadline = time.monotonic() + self.timeout + 5
            output = bytearray()
            pending = memoryview(data)
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE)
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0: raise RemoteError("remote_outcome_unknown")
                    for key, events in selector.select(left):
                        if key.fileobj is process.stdin:
                            try: pending = pending[os.write(process.stdin.fileno(), pending):]
                            except BlockingIOError: continue
                            if not pending:
                                selector.unregister(process.stdin); process.stdin.close()
                        else:
                            try: chunk = os.read(process.stdout.fileno(), min(65536, MAX_RESPONSE + 1 - len(output)))
                            except BlockingIOError: continue
                            if not chunk: selector.unregister(process.stdout)
                            output.extend(chunk)
                            if len(output) > MAX_RESPONSE: raise RemoteError("remote_outcome_unknown")
            if process.wait(timeout=max(.001, deadline-time.monotonic())):
                raise RemoteError("remote_outcome_unknown")
            return bytes(output)
        except (OSError, subprocess.TimeoutExpired):
            raise RemoteError("remote_outcome_unknown") from None
        finally:
            if process is not None:
                if process.poll() is None: process.kill()
                process.wait()
                process.stdin.close(); process.stdout.close()

    def _request(self, method, params, binding):
        data = _frame({"method": method, "params": params, "epoch": self.epoch,
                       "service_id": binding["service_id"] if binding else None,
                       "ticket": binding["ticket"] if binding else None}, MAX_REQUEST)
        raw = self._exchange(data)
        if len(raw) > MAX_RESPONSE: raise RemoteError("remote_outcome_unknown")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RemoteError("remote_protocol_error") from None
        if not isinstance(result, dict) or set(result) not in ({"result"}, {"error"}):
            raise RemoteError("remote_protocol_error")
        if "error" in result:
            raise RemoteError("remote_supervisor_failed")
        return result["result"]

    def _call(self, method, params):
        if self._binding is None: raise RemoteError("recover_required")
        return self._request(method, params, self._binding)

    def open(self, lease, options):
        return self._call("open", {"lease": lease, "options": options})

    def call(self, lease_id, operation, args, timeout=30):
        return self._call("call", {"lease_id": lease_id, "operation": operation, "args": args, "timeout": timeout})

    def renew(self, lease_id, expires_at):
        return self._call("renew", {"lease_id": lease_id, "expires_at": expires_at})

    def cleanup(self, lease_id, reason):
        return self._call("cleanup", {"lease_id": lease_id, "reason": reason})

    def recover(self):
        with self._binding_lock:
            if self._binding is None:
                binding = self._request("hello", {}, None)
                if (not isinstance(binding, dict) or set(binding) != {"service_id", "ticket"}
                    or not isinstance(binding["service_id"], str) or type(binding["ticket"]) is not int):
                    raise RemoteError("remote_protocol_error")
                self._binding = binding
        return self._call("recover", {})

    def close(self):
        return self._call("close", {})


class SupervisorServer(socketserver.ThreadingUnixStreamServer):
    # Dispatch is drained explicitly while supervisor ownership is retained.
    # A failed drain must report quarantine within its bound, not hide an
    # unbounded ThreadingMixIn join behind the configured deadline.
    daemon_threads = True
    block_on_close = False

    def __init__(self, socket_path, supervisor, *, supervisor_factory=None, drain_timeout=30):
        if type(drain_timeout) not in (int, float) or not 0 < drain_timeout <= 90:
            raise RemoteError("invalid_timeout")
        path = Path(socket_path)
        parent = path.parent
        if not path.is_absolute() or parent.resolve() != parent or parent.is_symlink():
            raise RemoteError("unsafe_socket_path")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
            raise RemoteError("unsafe_socket_permissions")
        if path.is_symlink():
            raise RemoteError("unsafe_socket_path")
        if path.exists():
            entry = path.stat()
            if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid():
                raise RemoteError("unsafe_socket_path")
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    # The supervisor's lifetime lock is already held by us.
                    path.unlink()
                else:
                    raise RemoteError("supervisor_running")
        self.supervisor = supervisor
        self.supervisor_factory = supervisor_factory
        self.drain_timeout = drain_timeout
        self.closing = threading.Event()
        self._state = threading.Condition(threading.RLock())
        self._transition = threading.Lock()
        self._service_id = uuid.uuid4().hex
        self._next_ticket = 1
        self._epoch = None
        self._ticket = None
        self._phase = "unbound"
        self._active = 0
        self._receipts = {}
        self._recovered = None
        self._terminal = OrderedDict()
        self._service_closed = False
        self._slots = threading.BoundedSemaphore(64)
        self._connections = set()
        self.path = path
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o600)
        entry = path.stat()
        self._socket_identity = (entry.st_dev, entry.st_ino)

    def dispatch(self, request):
        if not isinstance(request, dict) or set(request) != {"method", "params", "epoch", "service_id", "ticket"}:
            raise RemoteError("invalid_request")
        method, params = request["method"], request["params"]
        epoch, ticket = request['epoch'], request['ticket']
        if (not isinstance(method, str) or method not in METHODS | {"hello"} or not isinstance(params, dict)
            or not isinstance(epoch, str) or not re.fullmatch(r"[a-f0-9]{32}", epoch)):
            raise RemoteError("invalid_request")
        with self._state:
            if self.closing.is_set(): raise RemoteError("supervisor_stopping")
            if method == "hello":
                if params or request['service_id'] is not None or ticket is not None: raise RemoteError("invalid_request")
                return {"service_id": self._service_id, "ticket": self._next_ticket}
            if request['service_id'] != self._service_id or type(ticket) is not int:
                raise RemoteError("stale_epoch")
        if method == "recover":
            if params: raise RemoteError("invalid_request")
            return self._recover(epoch, ticket)
        if method == "close":
            if params: raise RemoteError("invalid_request")
            with self._transition:
                with self._state:
                    prior = self._terminal.get(epoch)
                    if prior and prior['ticket'] == ticket: return prior['result']
                    self._current(epoch, ticket)
                return self._close_epoch()
        if method == "cleanup":
            with self._transition:
                with self._state:
                    self._current(epoch, ticket)
                    if self._phase == "closed":
                        if set(params) != {'lease_id', 'reason'} or params['lease_id'] not in self._receipts:
                            raise RemoteError("unknown_terminal_lease")
                        return self._receipts[params['lease_id']]
                result = self.supervisor.cleanup(**params)
                self._merge([result])
                return result
        with self._state:
            self._current(epoch, ticket)
            if self._phase != "active": raise RemoteError("epoch_closed")
            # Leave handler headroom for cancellation while normal RPCs wait.
            if self._active >= 32: raise RemoteError("remote_busy")
            self._active += 1
            supervisor = self.supervisor
        try:
            return getattr(supervisor, method)(**params)
        finally:
            with self._state:
                self._active -= 1; self._state.notify_all()

    def _current(self, epoch, ticket):
        if self.closing.is_set() or epoch != self._epoch or ticket != self._ticket:
            raise RemoteError("stale_epoch")

    def _recover(self, epoch, ticket):
        with self._transition:
            with self._state:
                if self.closing.is_set(): raise RemoteError("supervisor_stopping")
                if epoch == self._epoch:
                    self._current(epoch, ticket)
                    if self._phase == 'active': return self._recovered
                    raise RemoteError("epoch_closed")
                if ticket != self._next_ticket:
                    raise RemoteError("epoch_unavailable")
                # A replacement Hub has already acquired the canonical gateway
                # lifetime lock. Retire the abandoned epoch before admitting it;
                # process-local epoch state cannot require a crashed Hub to close.
                needs_retirement = self._phase not in ('unbound', 'closed')
            if needs_retirement:
                self._close_epoch()
            with self._state:
                if self._phase not in ('unbound', 'closed'):
                    raise RemoteError("epoch_unavailable")
                if self._phase == 'closed':
                    if self.supervisor_factory is None: raise RemoteError("factory_required")
                    self.supervisor = self.supervisor_factory()
                self._epoch, self._ticket = epoch, ticket
                self._next_ticket += 1
                self._phase, self._receipts = 'recovering', {}
            result = self.supervisor.recover()
            self._merge(result)
            self._recovered = result
            self._phase = 'active'
            return result

    def _merge(self, receipts):
        if not isinstance(receipts, list): raise RemoteError("invalid_cleanup_receipt")
        _frame({'result': receipts}, MAX_RESPONSE)
        for receipt in receipts:
            if (not isinstance(receipt, dict) or not isinstance(receipt.get('lease_id'), str)
                or type(receipt.get('clean')) is not bool):
                raise RemoteError("invalid_cleanup_receipt")
        merged = dict(self._receipts)
        for receipt in receipts: merged[receipt['lease_id']] = receipt
        _frame({'result': list(merged.values())}, MAX_RESPONSE)
        self._receipts = merged

    def _close_epoch(self):
        with self._state: self._phase = 'closing'
        self._merge(self.supervisor.close())
        with self._state:
            if not self._state.wait_for(lambda: self._active == 0, self.drain_timeout):
                raise RemoteError("shutdown_pending")
        # Recheck final journal after cancellation drained pending dispatch.
        self._merge(self.supervisor.close())
        result = list(self._receipts.values())
        if all(r['clean'] is True for r in result):
            with self._state:
                self._phase = 'closed'
                self._terminal[self._epoch] = {'ticket': self._ticket, 'result': result}
                while len(self._terminal) > 32: self._terminal.popitem(last=False)
        return result

    def process_request(self, request, client_address):
        with self._state:
            if self.closing.is_set() or not self._slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._state: self._connections.discard(request)
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._state: self._connections.discard(request)
            self._slots.release()

    def server_close(self):
        self.closing.set()
        try:
            with self._transition:
                if not self._service_closed:
                    self._merge(self.supervisor.close())
                    with self._state:
                        if not self._state.wait_for(lambda: self._active == 0, self.drain_timeout):
                            raise RemoteError("shutdown_pending")
                    self._merge(self.supervisor.close())
                    if any(r['clean'] is not True for r in self._receipts.values()):
                        raise RemoteError("shutdown_incomplete")
                    self._service_closed = True
        finally:
            # Interrupt partial/idle frame readers before ThreadingMixIn joins.
            with self._state: connections = list(self._connections)
            for connection in connections:
                try: connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            super().server_close()
            try:
                entry = self.path.lstat()
                if stat.S_ISSOCK(entry.st_mode) and (entry.st_dev, entry.st_ino) == self._socket_identity:
                    self.path.unlink()
            except FileNotFoundError: pass


class _Handler(socketserver.StreamRequestHandler):
    timeout = 90

    def handle(self):
        try:
            result = self.server.dispatch(_read_frame(self.rfile, MAX_REQUEST))
            response = _frame({"result": result}, MAX_RESPONSE)
        except Exception:
            response = _frame({"error": "supervisor_request_failed"}, MAX_RESPONSE)
        try:
            self.wfile.write(response)
        except OSError:
            pass


def request(socket_path, timeout):
    incoming = _read_frame(__import__("sys").stdin.buffer, MAX_REQUEST)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(socket_path)
        connection.sendall(_frame(incoming, MAX_REQUEST))
        with connection.makefile("rb") as stream:
            response = _read_frame(stream, MAX_RESPONSE)
    __import__("sys").stdout.buffer.write(_frame(response, MAX_RESPONSE))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    client = sub.add_parser("request")
    client.add_argument("--socket", required=True)
    client.add_argument("--timeout", type=float, default=75)
    server = sub.add_parser("serve")
    server.add_argument("--config", required=True)
    args = parser.parse_args()
    if args.command == "request":
        try:
            request(args.socket, args.timeout)
        except Exception:
            raise SystemExit(1) from None
        return
    from .supervisor import TaskSupervisor
    config_path = Path(args.config)
    if config_path.is_symlink() or config_path.stat().st_uid != os.getuid() or config_path.stat().st_mode & 0o077:
        raise RemoteError("unsafe_config")
    config = json.loads(config_path.read_text())
    factory = lambda: TaskSupervisor(config["state_dir"], config["launch_config"])
    supervisor = factory()
    try:
        instance = SupervisorServer(config["socket_path"], supervisor, supervisor_factory=factory)
    except BaseException:
        supervisor.close()
        raise
    def stop(*ignored):
        # shutdown must execute outside serve_forever's thread.
        instance.closing.set()
        threading.Thread(target=instance.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        instance.serve_forever(poll_interval=.1)
    finally:
        instance.server_close()


if __name__ == "__main__":
    main()
