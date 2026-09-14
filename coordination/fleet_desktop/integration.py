"""Hub-owned desktop lifecycle and private live dispatch authorization.

The public browser.desktop namespace uses the already deployed synchronous
Inbox transport. The actuator receives only a fresh boolean over existing SSH.
"""

import threading

from fleet_browser.integration import FleetAdmission, WorkAuthority
from fleet_browser.store import ResourceError


class FleetGateway:
    def __init__(self, browser, service, config):
        expected = {"state_dir", "host", "slots", "supervisor", "allowed_work_ids",
                    "admission", "fence_socket"}
        if not isinstance(config, dict) or set(config) != expected:
            raise ResourceError("invalid_config", "Desktop configuration fields are invalid")
        self.browser, self.service, self.config = browser, service, dict(config)
        self.desktop = self.fence_server = None
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._attempted = self._closed = self._drained = False

    def _dispatch_allowed(self, request):
        """Read only the current canonical lease and native work authority."""
        try:
            if self._closed or self.desktop is None:
                return False
            if not isinstance(request, dict) or set(request) != {
                    "actor", "work_id", "lease_id", "generation", "work_revision", "host"}:
                return False
            if type(request["generation"]) is not int or request["host"] != self.config["host"]:
                return False
            lease = self.desktop.store.get(request["actor"], request["lease_id"])
            if (lease["generation"] != request["generation"]
                    or lease["work_id"] != request["work_id"]
                    or lease["metadata"].get("host") != request["host"]
                    or lease["metadata"].get("work_revision") != request["work_revision"]):
                return False
            self.desktop._fence(lease)
            return True
        except Exception:
            return False

    def _desktop(self):
        with self._lock:
            if self._closed:
                raise ResourceError("gateway_stopping", "Desktop gateway is stopping")
            if self.desktop is not None:
                return self.desktop
            if self._attempted:
                raise ResourceError("launch_failed", "Desktop initialization needs recovery")
            self._attempted = True
            # Optional desktop support never imports the VM/VNC libraries on Hub.
            from .fencing import DesktopFenceServer
            from .gateway import DesktopGateway
            from .remote import RemoteDesktopSupervisor

            config = self.config
            authority = WorkAuthority(self.service.store, config["allowed_work_ids"])
            self.fence_server = DesktopFenceServer(config["fence_socket"], self._dispatch_allowed)
            try:
                self.fence_server.start()
                self.desktop = DesktopGateway(
                    config["state_dir"], RemoteDesktopSupervisor(**config["supervisor"]),
                    host=config["host"], slots=config["slots"],
                    authorize_work=authority.allowed_work, work_revision=authority.revision,
                    operator=authority.operator, admission=FleetAdmission(**config["admission"]))
            except BaseException:
                self.fence_server.close()
                raise
            return self.desktop

    def call(self, actor, operation, params, request_id):
        if self._closed:
            raise ResourceError("gateway_stopping", "Fleet gateway is stopping")
        if isinstance(operation, str) and operation.startswith("browser.desktop."):
            if operation[len("browser.desktop."):] not in {
                    "open", "act", "renew", "close", "status", "stop", "resume"}:
                raise ResourceError("unsupported_operation", "Unknown desktop operation")
            return self._desktop().call(actor, operation[len("browser."):], params, request_id)
        return self.browser.call(actor, operation, params, request_id)

    def close(self):
        with self._close_lock:
            with self._lock:
                self._closed = True
                if self._drained:
                    return
            errors = []
            # Close every owned boundary even when another retains quarantine.
            for component in (self.desktop, self.fence_server, self.browser):
                if component is not None:
                    try:
                        component.close()
                    except Exception as error:
                        errors.append(error)
            if errors:
                raise errors[0]
            self._drained = True
