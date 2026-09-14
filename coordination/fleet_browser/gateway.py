"""Synchronous browser operations behind existing authenticated Inbox calls.

No offline replay, raw browser endpoint, or second identity database. The
supervisor can cancel a waiting action independently of normal RPC locking.
"""

from __future__ import annotations

from collections import OrderedDict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit
import uuid
import weakref

from .store import ResourceError, ResourceStore, _identifier
from .routing import RoutedSupervisor


_ACTIONS = {"navigate", "snapshot", "click", "fill", "press", "select", "screenshot"}
_READ_ACTIONS = {"snapshot", "screenshot"}
_PUBLIC = {"browser.open", "browser.act", "browser.renew", "browser.close", "browser.status",
           "browser.stop", "browser.resume", "browser.reconcile"}


def _url(value):
    if value == "about:blank":
        return value
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ResourceError("invalid_url", "Expected a bounded HTTP or HTTPS URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
    except ValueError as exc:
        raise ResourceError("invalid_url", "Only HTTP, HTTPS and about:blank navigation is supported") from exc
    return value


def _fingerprint(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ResourceError("invalid_input", "Invalid request data") from exc
    if len(raw) > 128 * 1024:
        raise ResourceError("request_too_large", "Request exceeds the bounded browser interface")
    return hashlib.sha256(raw).hexdigest()


def _keys(params, allowed):
    if not isinstance(params, dict) or set(params) - allowed:
        raise ResourceError("invalid_input", "Unsupported browser request fields")


class BrowserGateway:
    def __init__(self, state_dir, supervisor, *, host, authorize_work, operator_check,
                 admission_check, profiles=None, max_sessions=2, clock=None, watchdog_interval=1.0,
                 start_watchdog=True, work_revision=None):
        self.state_dir = Path(state_dir).absolute()
        if self.state_dir.is_symlink():
            raise ResourceError("unsafe_state_path", "State directory must not be a symlink")
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.state_dir.stat().st_uid != os.getuid() or self.state_dir.stat().st_mode & 0o077:
            raise ResourceError("unsafe_state_path", "Gateway state must be private and owned")
        _identifier(host, "host")
        if type(max_sessions) is not int or not 1 <= max_sessions <= 64:
            raise ResourceError("invalid_config", "Invalid browser capacity")
        if not all(callable(fn) for fn in (authorize_work, operator_check, admission_check)):
            raise ResourceError("invalid_config", "Existing authorization and admission hooks are required")
        if work_revision is not None and not callable(work_revision):
            raise ResourceError("invalid_config", "Work revision hook must be callable")
        lock = self.state_dir / ".controller.lock"
        if lock.is_symlink():
            raise ResourceError("unsafe_state_path", "Controller lock must not be a symlink")
        self._lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._lock_fd)
            raise ResourceError("controller_running", "A gateway already owns this state") from exc
        try:
            self.host, self.supervisor = host, supervisor
            self.authorize_work, self.operator_check, self.admission_check = authorize_work, operator_check, admission_check
            self.work_revision = work_revision
            self.profiles, self.max_sessions = dict(profiles or {}), max_sessions
            self.store = ResourceStore(self.state_dir / "resources.sqlite3", clock)
            self.controller_id = str(uuid.uuid4())
            self._guard = threading.RLock()
            self._admission_guard = threading.Lock()
            self._host_admission = {h: threading.Lock() for h in supervisor.supervisors} if isinstance(supervisor, RoutedSupervisor) else {host: self._admission_guard}
            if host not in self._host_admission:
                raise ResourceError("invalid_config", "Default browser host is not configured")
            self._commands = weakref.WeakValueDictionary()
            self._lifecycle = threading.Condition(self._guard)
            self._active_calls = 0
            self._close_guard = threading.Lock()
            self._closed = False
            self._results = OrderedDict()
            self._closing = False
            self._stop_event = threading.Event()
            self._watchdog_interval = max(0.05, float(watchdog_interval))
            self._watchdog_error = None
            recovered = self.store.recover(self.controller_id)
            if isinstance(self.supervisor, RoutedSupervisor):
                if self.store.count_live() != len(recovered):
                    raise ResourceError("recovery_incomplete", "Canonical recovery exceeds the Store enumeration bound")
                for lease in recovered:
                    self.supervisor.bind(lease)
            # Recovery examines only supervisor-owned journals. Ambiguous rows
            # stay reserved; restart cannot manufacture cleanup success.
            restored = set()
            for receipt in self.supervisor.recover():
                lease_id = receipt.get("lease_id")
                lease = next((r for r in recovered if r["lease_id"] == lease_id), None)
                if lease:
                    self._record_cleanup(lease, receipt)
                    restored.add(lease_id)
            # A crash can occur after the supervisor seals clean proof but
            # before the Hub records it. Query only unresolved lease IDs.
            for lease in recovered:
                if lease["lease_id"] not in restored:
                    self._cleanup(lease, "gateway_recovery")
            self._watchdog = None
            if start_watchdog:
                self._watchdog = threading.Thread(target=self._watch, name="browser-resource-watchdog", daemon=True)
                self._watchdog.start()
        except BaseException:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise

    def _authorized(self, actor, work_id, expected_revision=None):
        try:
            allowed = self.authorize_work(actor, work_id)
            revision = self.work_revision(actor, work_id) if self.work_revision and allowed is True else None
            if self.work_revision and allowed is True and (not isinstance(revision, str) or not revision or len(revision) > 128):
                raise ValueError()
        except Exception as exc:
            raise ResourceError("authorization_unavailable", "Current work ownership could not be verified") from None
        if allowed is not True or (expected_revision is not None and revision != expected_revision):
            raise ResourceError("work_not_owned", "Caller does not own this work")
        return revision

    def _command_lock(self, lease_id):
        with self._guard:
            return self._commands.setdefault(lease_id, threading.RLock())

    def _operator(self, actor):
        try:
            allowed = self.operator_check(actor)
        except Exception as exc:
            raise ResourceError("authorization_unavailable", "Operator authority could not be verified") from None
        if allowed is not True:
            raise ResourceError("operator_required", "Operator authority is required")

    def _lease(self, actor, params, *, operator=False):
        lease_id = params.get("lease_id")
        generation = params.get("generation")
        if operator:
            self._operator(actor)
            owner = params.get("owner", actor)
            _identifier(owner, "lease owner")
        else:
            owner = actor
        lease = self.store.get(owner, lease_id)
        if type(generation) is not int or lease["generation"] != generation:
            raise ResourceError("stale_generation", "Lease generation is no longer current")
        if not operator:
            self._authorized(actor, lease["work_id"], lease["metadata"].get("work_revision"))
        return lease

    def _ensure_running(self):
        with self._guard:
            if self._closing:
                raise ResourceError("gateway_stopping", "Browser gateway is stopping")

    def call(self, actor, operation, params, request_id):
        with self._lifecycle:
            self._ensure_running()
            self._active_calls += 1
        try:
            return self._call(actor, operation, params, request_id)
        finally:
            with self._lifecycle:
                self._active_calls -= 1
                self._lifecycle.notify_all()

    def _call(self, actor, operation, params, request_id):
        _identifier(actor, "actor")
        _identifier(request_id, "request ID")
        if operation not in _PUBLIC:
            raise ResourceError("unsupported_operation", "Unknown browser operation")
        if self._closing:
            raise ResourceError("gateway_stopping", "Browser gateway is stopping")
        _fingerprint(params)
        if operation == "browser.open":
            return self._open(actor, params, request_id)
        if operation == "browser.act":
            _keys(params, {"lease_id", "generation", "action", "args", "timeout"})
            return self._act(actor, params, request_id)
        if operation == "browser.status":
            _keys(params, {"lease_id", "generation"})
            if "lease_id" in params:
                return self._lease(actor, params)
            result = {"leases": self.store.list_leases(actor), "watchdog": "error" if self._watchdog_error else "ok"}
            if isinstance(self.supervisor, RoutedSupervisor):
                result["hosts"] = self.supervisor.readiness()
            return result
        operator = operation in {"browser.stop", "browser.resume", "browser.reconcile"}
        allowed = {"lease_id", "generation", "ttl_seconds"} if operation == "browser.renew" else {"lease_id", "generation"}
        if operator:
            allowed |= {"owner", "evidence"} if operation == "browser.reconcile" else {"owner"}
        _keys(params, allowed)
        lease = self._lease(actor, params, operator=operator)
        if operation == "browser.renew":
            with self._command_lock(lease["lease_id"]):
                self._ensure_running()
                self._authorized(actor, lease["work_id"], lease["metadata"].get("work_revision"))
                fingerprint = _fingerprint(params)
                action_receipt = self.store.begin_action(actor, lease["lease_id"], lease["generation"], request_id,
                                                          "renew", fingerprint, mutating=False)
                if not action_receipt["dispatch"]:
                    return {"lease": self.store.get(actor, lease["lease_id"]), "replayed": True, "receipt": action_receipt}
                try:
                    renewed = self.store.renew(actor, lease["lease_id"], lease["generation"], params.get("ttl_seconds", 300))
                    self.supervisor.renew(lease["lease_id"], renewed["expires_at"])
                except Exception as exc:
                    self.store.finish_action(actor, lease["lease_id"], request_id, "failed", {"code": "renewal_failed"})
                    self.store.revoke(actor, lease["lease_id"], lease["generation"], "renewal_failed")
                    self._cleanup(lease, "renewal_failed")
                    raise ResourceError("renewal_failed", "Worker could not confirm the renewed deadline") from None
                self.store.finish_action(actor, lease["lease_id"], request_id, "completed", {"code": "deadline_renewed"})
                return renewed
        control = (actor, request_id, _fingerprint([operation, params]))
        if operation == "browser.reconcile":
            return self.store.reconcile(lease["actor"], lease["lease_id"], lease["generation"], params.get("evidence"), control=control)
        if operation == "browser.resume":
            return self.store.resume(lease["actor"], lease["lease_id"], lease["generation"], control=control)
        if operation == "browser.stop":
            changed = self.store.hold(lease["actor"], lease["lease_id"], lease["generation"], control=control)
        else:
            changed = self.store.revoke(actor, lease["lease_id"], lease["generation"], "client_close", control=control)
        if changed.get("replayed"):
            return changed
        return self._cleanup(lease, "operator_stop" if operator else "client_close")

    def _open(self, actor, params, request_id):
        _keys(params, {"work_id", "attempt_id", "profile_id", "url", "ttl_seconds"})
        work_id, attempt_id = params.get("work_id"), params.get("attempt_id")
        _identifier(work_id, "work ID")
        _identifier(attempt_id, "attempt ID")
        revision = self._authorized(actor, work_id)
        if "url" in params:
            _url(params["url"])
        session_key = uuid.uuid5(uuid.NAMESPACE_URL, actor + ":" + request_id).hex
        selected_host = self.host
        resources = [f"browser/{selected_host}/{session_key}"]
        metadata = {"host": self.host, "adapter": "playwright-pipe", "request_fingerprint": _fingerprint(params)}
        if revision is not None:
            metadata["work_revision"] = revision
        options = {}
        if params.get("profile_id") is not None:
            profile_id = _identifier(params["profile_id"], "profile ID")
            profile = self.profiles.get(profile_id)
            if not profile or profile.get("host", self.host) not in self._host_admission:
                raise ResourceError("profile_unavailable", "Profile is not registered on this browser host")
            selected_host = profile.get("host", self.host)
            metadata["host"] = selected_host
            resources[0] = f"browser/{selected_host}/{session_key}"
            metadata["profile_id"] = profile_id
            if profile.get("persistent", True):
                resources.append(f"profile/{selected_host}/{profile_id}")
                options["profile_id"] = profile_id
            if profile.get("auth_ref"):
                options["auth_ref"] = profile["auth_ref"]
            if profile.get("account_id"):
                provider = _identifier(profile.get("provider"), "provider")
                tenant = _identifier(profile.get("tenant_id"), "tenant")
                account = _identifier(profile["account_id"], "account")
                resources.append(f"account/{provider}/{tenant}/{account}")
                metadata.update(tenant_id=tenant, account_id=account)
        # Admission/lease allocation is short; slow browser launch does not
        # block unrelated leases. The acquired lease counts as a live resource.
        with self._host_admission[selected_host]:
            self._ensure_running()
            self._authorized(actor, work_id, revision)
            self._ensure_running()
            lease = self.store.acquire(actor, work_id, attempt_id, request_id, resources,
                                       params.get("ttl_seconds", 300), metadata)
            if lease.get("replayed"):
                return {"lease": lease, "replayed": True}
            try:
                admitted = (not isinstance(self.supervisor, RoutedSupervisor) or self.supervisor.ready(selected_host)) and self.admission_check(selected_host)
            except Exception:
                self.store.revoke(actor, lease["lease_id"], lease["generation"], "admission_failed")
                self.store.cleanup_result(lease["lease_id"], lease["generation"], True, {"clean": True, "code": "not_launched"})
                raise ResourceError("capacity_unavailable", "Fresh browser workload admission is unavailable") from None
            if admitted is not True or self.store.count_live() > self.max_sessions:
                self.store.revoke(actor, lease["lease_id"], lease["generation"], "admission_failed")
                self.store.cleanup_result(lease["lease_id"], lease["generation"], True, {"clean": True, "code": "not_launched"})
                raise ResourceError("capacity_unavailable", "Fresh browser workload admission did not pass")
        with self._command_lock(lease["lease_id"]):
            action_id = "open-" + hashlib.sha256(request_id.encode()).hexdigest()
            try:
                self._ensure_running()
                self._authorized(actor, work_id, revision)
                self._ensure_running()
                self.store.begin_action(actor, lease["lease_id"], lease["generation"], action_id, "open", _fingerprint(options), mutating=False)
            except ResourceError:
                self.store.revoke(actor, lease["lease_id"], lease["generation"], "launch_not_authorized")
                self.store.cleanup_result(lease["lease_id"], lease["generation"], True, {"clean": True, "code": "not_launched"})
                raise
            try:
                # Initial launch is always blank. URL navigation is a separately
                # fenced action, so stop-during-launch cannot submit a stale URL.
                self.supervisor.open(lease, options)
                self.store.finish_action(actor, lease["lease_id"], action_id, "completed", {"code": "worker_ready"})
            except Exception as exc:
                self.store.finish_action(actor, lease["lease_id"], action_id, "failed", {"code": "launch_failed"})
                self.store.revoke(actor, lease["lease_id"], lease["generation"], "launch_failed")
                self._cleanup(lease, "launch_failed")
                raise ResourceError("launch_failed", "Owned browser could not be launched; inspect cleanup receipt") from None
            current = self.store.get(actor, lease["lease_id"])
            if self._closing or current["status"] != "active":
                self.store.revoke(actor, lease["lease_id"], lease["generation"], "revoked_during_launch")
                self._cleanup(current, "revoked_during_launch")
                raise ResourceError("lease_inactive", "Lease ended during browser launch")
        result = {"lease": current, "browser_ready": True}
        if params.get("url"):
            result["navigation"] = self._act(actor, {"lease_id": lease["lease_id"], "generation": lease["generation"],
                                                    "action": "navigate", "args": {"url": params["url"]}},
                                              "navigate-" + hashlib.sha256(request_id.encode()).hexdigest())
        return result

    def _act(self, actor, params, request_id):
        lease = self._lease(actor, params)
        action, args = params.get("action"), params.get("args", {})
        if action not in _ACTIONS or not isinstance(args, dict):
            raise ResourceError("unsupported_action", "Action is not supported by this browser adapter")
        if action == "navigate":
            _url(args.get("url"))
        timeout = params.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 30:
            raise ResourceError("invalid_input", "Action timeout must be between 0.1 and 30 seconds")
        fingerprint = _fingerprint([action, args, timeout])
        cache_key = (lease["lease_id"], request_id)
        with self._command_lock(lease["lease_id"]):
            self._ensure_running()
            self._authorized(actor, lease["work_id"], lease["metadata"].get("work_revision"))
            self._ensure_running()
            receipt = self.store.begin_action(actor, lease["lease_id"], lease["generation"], request_id,
                                               action, fingerprint, mutating=action not in _READ_ACTIONS)
            if not receipt["dispatch"]:
                with self._guard:
                    cached = self._results.get(cache_key)
                return {"replayed": True, "receipt": receipt, "result_available": cached is not None, "result": cached}
            try:
                result = self.supervisor.call(lease["lease_id"], action, args, timeout=float(timeout))
            except Exception as exc:
                outcome = "failed" if action in _READ_ACTIONS else "unknown"
                receipt = self.store.finish_action(actor, lease["lease_id"], request_id, outcome, {"code": "worker_call_failed"})
                self.store.revoke(actor, lease["lease_id"], lease["generation"], "worker_call_failed")
                self._cleanup(lease, "worker_call_failed")
                raise ResourceError("action_unknown" if outcome == "unknown" else "action_failed",
                                    "Browser action did not complete reliably; inspect lease status before retrying") from None
            receipt = self.store.finish_action(actor, lease["lease_id"], request_id, "completed", {"code": "worker_completed"})
            # Never persist returned page contents. Cache is bounded to 32 small
            # results and disappears on restart; the durable receipt prevents replay.
            try:
                encoded = json.dumps(result, allow_nan=False).encode()
            except (TypeError, ValueError) as exc:
                raise ResourceError("invalid_worker_result", "Worker result was not valid JSON") from exc
            if len(encoded) > 256 * 1024:
                raise ResourceError("result_too_large", "Action completed but its result exceeded the response bound")
            with self._guard:
                self._results[cache_key] = result
                while len(self._results) > 32:
                    self._results.popitem(last=False)
            return {"receipt": receipt, "result": result}

    def _record_cleanup(self, lease, receipt):
        code = str(receipt.get("reason_code", receipt.get("status", "cleanup_unknown")))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", code):
            code = "cleanup_unknown"
        clean = receipt.get("clean") is True and receipt.get("lease_id") == lease["lease_id"]
        summary = {"clean": clean, "code": code}
        if isinstance(receipt.get("remaining_pids"), list):
            summary["process_count"] = len(receipt["remaining_pids"])
        return self.store.cleanup_result(lease["lease_id"], lease["generation"], clean, summary)

    def _cleanup(self, lease, reason):
        current = self.store.get(lease["actor"], lease["lease_id"])
        if current["status"] == "closed":
            return current
        try:
            if isinstance(self.supervisor, RoutedSupervisor):
                self.supervisor.bind(lease)
            receipt = self.supervisor.cleanup(lease["lease_id"], reason)
        except Exception:
            receipt = {"clean": False, "reason_code": "supervisor_cleanup_failed"}
        return self._record_cleanup(current, receipt)

    def sweep(self):
        return [self._cleanup(lease, lease.get("reason") or "lease_expired") for lease in self.store.expired()]

    def _watch(self):
        while not self._stop_event.wait(self._watchdog_interval):
            try:
                self.sweep()
                self._watchdog_error = None
            except Exception:
                # Failure stays visible; worker deadlines independently stop
                # input. Never treat a failed sweep as permission to reuse state.
                self._watchdog_error = "sweep_failed"

    def _close_supervisor(self):
        receipts = self.supervisor.close()
        if isinstance(receipts, list) and any(r.get("clean") is not True for r in receipts):
            raise ResourceError("shutdown_incomplete", "Supervisor retains quarantined resources")

    def close(self):
        # Closing fences calls immediately, but a transient failure does not
        # turn the next close into a no-op. Keep controller ownership until all
        # participating calls and the watchdog have drained.
        with self._close_guard:
            if self._closed:
                return
            with self._lifecycle:
                self._closing = True
            self._stop_event.set()
            if self._watchdog:
                self._watchdog.join(timeout=5)
            failed = False
            try:
                for lease in self.store.list_leases(limit=100):
                    if lease["status"] != "closed":
                        self.store.revoke(lease["actor"], lease["lease_id"], lease["generation"], "gateway_shutdown")
                        self._cleanup(lease, "gateway_shutdown")
            except Exception:
                failed = True
            try:
                # Cancellation must not wait for the ordinary action mutex.
                self._close_supervisor()
            except Exception:
                failed = True
            with self._lifecycle:
                drained = self._lifecycle.wait_for(lambda: self._active_calls == 0, timeout=5)
            if not drained or (self._watchdog and self._watchdog.is_alive()):
                raise ResourceError("shutdown_pending", "Shutdown is waiting for participating calls; retry close")
            if failed:
                raise ResourceError("shutdown_incomplete", "Shutdown cleanup failed; retry close")
            # A call already between its dispatch fence and worker entry may
            # have finished during cancellation. Reconcile its final journal
            # before releasing the old controller epoch.
            try:
                for lease in self.store.list_leases(limit=100):
                    if lease["status"] != "closed":
                        self.store.revoke(lease["actor"], lease["lease_id"], lease["generation"], "gateway_shutdown")
                        self._cleanup(lease, "gateway_shutdown")
                self._close_supervisor()
            except Exception:
                raise ResourceError("shutdown_incomplete", "Shutdown cleanup failed; retry close") from None
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._closed = True
