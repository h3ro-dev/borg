"""Trusted fixed-host dispatch; canonical leases remain owned by ResourceStore.

bind() accepts Store rows only, before recovery or dispatch. No host fallback,
new identity, remote endpoint discovery, or cross-host retry is performed.
"""

import threading

from .store import ResourceError, _identifier


class RoutedSupervisor:
    def __init__(self, supervisors):
        if not isinstance(supervisors, dict) or not supervisors:
            raise ResourceError("invalid_config", "A fixed supervisor map is required")
        self.supervisors = dict(supervisors)
        for host in self.supervisors:
            _identifier(host, "host")
        if len({id(value) for value in self.supervisors.values()}) != len(self.supervisors):
            raise ResourceError("invalid_config", "Hosts must have distinct supervisors")
        self._guard = threading.RLock()
        self._bindings = {}
        self._states = dict.fromkeys(self.supervisors, "recover_required")
        self._closing = False
        self._recovered = False
        self._close_guard = threading.Lock()
        self._close_receipts = {host: {} for host in self.supervisors}

    def bind(self, lease):
        host = lease.get("metadata", {}).get("host")
        if host not in self.supervisors:
            raise ResourceError("unknown_host", "Canonical lease host is not configured")
        lease_id = _identifier(lease.get("lease_id"), "lease ID")
        binding = (host, lease["actor"], lease["generation"])
        with self._guard:
            old = self._bindings.get(lease_id)
            if old is not None and old != binding:
                raise ResourceError("routing_collision", "Canonical lease binding changed")
            self._bindings[lease_id] = binding

    def readiness(self):
        with self._guard:
            return dict(self._states)

    def ready(self, host):
        with self._guard:
            return not self._closing and self._states.get(host) == "ready"

    def _target(self, lease_id, *, cleanup=False):
        with self._guard:
            binding = self._bindings.get(lease_id)
            if binding is None:
                raise ResourceError("routing_unbound", "Lease has no canonical host binding")
            host = binding[0]
            if (cleanup and self._states[host] in {"recover_required", "recovery_failed", "recovery_conflict"}) or (not cleanup and (self._closing or self._states[host] != "ready")):
                raise ResourceError("host_unavailable", "Configured browser host is unavailable")
            return self.supervisors[host]

    def open(self, lease, options):
        self.bind(lease)
        return self._target(lease["lease_id"]).open(lease, options)

    def call(self, lease_id, operation, args, timeout=30):
        return self._target(lease_id).call(lease_id, operation, args, timeout=timeout)

    def renew(self, lease_id, expires_at):
        return self._target(lease_id).renew(lease_id, expires_at)

    def cleanup(self, lease_id, reason):
        # Failed recovery does not authorize another host or prove cleanup.
        supervisor = self._target(lease_id, cleanup=True)
        try:
            receipt = supervisor.cleanup(lease_id, reason)
            if not isinstance(receipt, dict) or receipt.get("lease_id") != lease_id or type(receipt.get("clean")) is not bool:
                raise ValueError()
        except Exception:
            receipt = {"lease_id": lease_id, "clean": False, "reason_code": "host_cleanup_failed"}
        if not receipt["clean"]:
            with self._guard:
                self._states[self._bindings[lease_id][0]] = "cleanup_failed"
        return receipt

    def recover(self):
        with self._guard:
            if self._closing or self._recovered:
                raise ResourceError("recovery_fenced", "Router recovery already attempted")
            self._recovered = True
            bindings = dict(self._bindings)
        results = []
        invalid_proof = False
        for host, supervisor in self.supervisors.items():
            try:
                receipts = supervisor.recover()
                if not isinstance(receipts, list):
                    raise ValueError()
                seen = set()
                for receipt in receipts:
                    lease_id = receipt.get("lease_id") if isinstance(receipt, dict) else None
                    if (lease_id not in bindings or bindings[lease_id][0] != host
                            or lease_id in seen or type(receipt.get("clean")) is not bool):
                        raise ValueError()
                    seen.add(lease_id)
                results.extend(receipts)
                state = "ready" if all(r["clean"] for r in receipts) else "cleanup_failed"
            except Exception as exc:
                invalid_proof |= isinstance(exc, (ValueError, TypeError))
                state = "recovery_failed"
                results.extend({"lease_id": lease_id, "clean": False,
                                "reason_code": "host_recovery_failed"}
                               for lease_id, binding in bindings.items() if binding[0] == host)
            with self._guard:
                self._states[host] = state
        if invalid_proof:
            # An orphan/collision cannot be attributed to canonical resources.
            # Unlike a transport outage, it invalidates conservation globally.
            with self._guard:
                for host in self._states:
                    if self._states[host] != "recovery_failed":
                        self._states[host] = "recovery_conflict"
            return [{"lease_id": lease_id, "clean": False, "reason_code": "recovery_conflict"}
                    for lease_id in bindings]
        return results

    def close(self):
        with self._close_guard:
            with self._guard:
                self._closing = True
            results = []
            for host, supervisor in self.supervisors.items():
                try:
                    receipts = supervisor.close()
                    if not isinstance(receipts, list):
                        raise ValueError()
                    # Preserve every failed receipt; validate successful proofs
                    # before returning anything that could be mistaken for clean.
                    if any(not isinstance(r, dict) or type(r.get("clean")) is not bool for r in receipts):
                        raise ValueError()
                    with self._guard:
                        for receipt in receipts:
                            lease_id = receipt.get("lease_id")
                            if lease_id not in self._bindings or self._bindings[lease_id][0] != host:
                                raise ValueError()
                            self._close_receipts[host][lease_id] = dict(receipt, host=host)
                        merged = list(self._close_receipts[host].values())
                    results.extend(merged)
                    state = "closed" if all(r["clean"] for r in merged) else "cleanup_failed"
                except Exception:
                    state = "close_failed"
                    results.append({"host": host, "clean": False, "reason_code": "host_close_failed"})
                with self._guard:
                    self._states[host] = state
            return results
