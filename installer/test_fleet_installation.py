"""Enrollment transactions use real private files and a synthetic SSH peer."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from installer import config
from installer import clients
from installer.fleet import manage
from fleet_tools import Fleet


class DummyClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def close(self):
        pass


class FleetInstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(temporary.cleanup)
        self.doc = config.initialize(Path(temporary.name) / "owner", "alice")
        self.root = Path(self.doc["home"])
        self.connector = self.root / "borg-context/config.json"
        self.registry = self.root / "borg-context/fleet.json"
        self.expected = {key: self.doc[key] for key in ("instance_id", "owner", "home")}
        self.args = SimpleNamespace(operation="add", host="target", ssh_alias="target",
            remote_home=Path("/home/alice/.borg"), owner="alice",
            instance_id=str(uuid.uuid4()), label=None, role=[])

    def legacy_config(self):
        doc = config.read_private(self.connector)
        doc.pop("identity")
        doc.pop("fleet")
        config.write_private(self.connector, json.dumps(doc), replace=True)

    @contextmanager
    def peer(self, during_verify=lambda: None):
        async def verify(fleet, client, row):
            during_verify()
            return {"schema": "borg-identity/v1", "server_generation": "synthetic",
                    **{key: row[key] for key in ("instance_id", "owner", "home")}}
        with patch.object(Fleet, "_new_client", lambda *args: DummyClient()), \
             patch.object(Fleet, "verify", verify):
            yield

    def test_owner_edit_during_verification_is_preserved(self):
        self.legacy_config()

        def owner_edit():
            doc = config.read_private(self.connector)
            doc["computer"]["concurrency"] = {"max_in_flight": 1}
            config.write_private(self.connector, json.dumps(doc), replace=True)

        with self.peer(owner_edit):
            result = manage(self.doc, self.args)
        current = config.read_private(self.connector)
        self.assertEqual(current["computer"].get("concurrency"), {"max_in_flight": 1})
        self.assertEqual(current["identity"], self.expected)
        self.assertEqual(result["state"], "enrolled")
        self.assertTrue(result["restart_connector_required"])

    def test_client_setup_and_enrollment_preserve_both_updates(self):
        self.legacy_config()
        client_path = self.root / "coordination/data/connector-client.json"
        config.write_private(self.root / "coordination/config.json",
                             json.dumps({"connector_client_config": str(client_path)}))
        read_paused, release_read, verified, enrolled = [threading.Event() for _ in range(4)]
        errors = []
        original_read = config.read_private

        def read(path):
            value = original_read(path)
            if path == self.connector and threading.current_thread().name == "client-setup":
                read_paused.set()
                if not release_read.wait(5):
                    raise RuntimeError("test did not release client setup")
            return value

        def setup():
            try:
                clients.configure_clients(self.doc)
            except RuntimeError as exc:
                if str(exc) != "stop before provider setup":
                    errors.append(exc)

        def enroll():
            try:
                manage(self.doc, self.args)
                enrolled.set()
            except Exception as exc:
                errors.append(exc)

        with self.peer(verified.set), patch.object(config, "read_private", read), \
             patch.object(clients.importlib.machinery.SourceFileLoader, "load_module",
                          side_effect=RuntimeError("stop before provider setup")):
            setup_thread = threading.Thread(target=setup, name="client-setup", daemon=True)
            enroll_thread = threading.Thread(target=enroll, daemon=True)
            setup_thread.start()
            try:
                self.assertTrue(read_paused.wait(2))
                enroll_thread.start()
                self.assertTrue(verified.wait(2))
                self.assertFalse(enrolled.wait(.1), "enrollment must wait for the client's read/write transaction")
            finally:
                release_read.set()
                setup_thread.join(5)
                if enroll_thread.ident is not None:
                    enroll_thread.join(5)
            self.assertFalse(setup_thread.is_alive())
            self.assertFalse(enroll_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(enrolled.is_set())
        current = config.read_private(self.connector)
        self.assertEqual(current["identity"], self.expected)
        self.assertEqual(current["fleet"], {"registry_file": str(self.registry)})
        self.assertEqual(current["computer"]["inbox_client_config"], str(client_path))

    def test_conflicting_owner_pins_during_verification_prevent_enrollment(self):
        for legacy in (True, False):
            for key in ("identity", "fleet"):
                with self.subTest(legacy=legacy, key=key):
                    doc = config.read_private(self.connector)
                    doc.update(identity=self.expected, fleet={"registry_file": str(self.registry)})
                    config.write_private(self.connector, json.dumps(doc), replace=True)
                    if legacy:
                        self.legacy_config()
                    before = self.registry.read_bytes()
                    owner_bytes = []

                    def owner_edit():
                        doc = config.read_private(self.connector)
                        doc[key] = {"owner_edit": "must survive"}
                        config.write_private(self.connector, json.dumps(doc), replace=True)
                        owner_bytes.append(self.connector.read_bytes())

                    with self.peer(owner_edit):
                        with self.assertRaisesRegex(ValueError, "reconciliation"):
                            manage(self.doc, self.args)
                    self.assertEqual(self.registry.read_bytes(), before)
                    self.assertEqual(self.connector.read_bytes(), owner_bytes[0])

    def test_config_replace_failure_does_not_commit_registry(self):
        self.legacy_config()
        before = self.registry.read_bytes(), self.connector.read_bytes()
        replace = os.replace

        def fail_connector(source, target):
            if target == self.connector:
                raise OSError("synthetic config failure")
            return replace(source, target)

        with self.peer(), patch.object(config.os, "replace", fail_connector):
            with self.assertRaisesRegex(OSError, "synthetic config failure"):
                manage(self.doc, self.args)
        self.assertEqual((self.registry.read_bytes(), self.connector.read_bytes()), before)
        self.assertEqual(list(self.connector.parent.glob(".borg-write-*")), [])

    def test_registry_failure_leaves_compatible_config_and_can_retry(self):
        self.legacy_config()
        before = self.registry.read_bytes()
        replace = os.replace
        order = []

        def fail_registry(source, target):
            order.append(target)
            if target == self.registry:
                self.assertEqual(config.read_private(self.connector).get("identity"), self.expected)
                raise OSError("synthetic registry failure")
            return replace(source, target)

        with self.peer(), patch.object(config.os, "replace", fail_registry):
            with self.assertRaisesRegex(RuntimeError, "restart the connector before retrying"):
                manage(self.doc, self.args)
        self.assertEqual(order, [self.connector, self.registry])
        self.assertEqual(self.registry.read_bytes(), before)
        with self.peer():
            self.assertEqual(manage(self.doc, self.args)["state"], "enrolled")
        self.assertEqual(len(config.read_private(self.registry)["hosts"]), 1)

    def test_compatible_config_is_not_rewritten(self):
        before = self.connector.read_bytes(), self.connector.stat().st_ino
        with self.peer():
            result = manage(self.doc, self.args)
        self.assertFalse(result["restart_connector_required"])
        self.assertEqual((self.connector.read_bytes(), self.connector.stat().st_ino), before)

    def test_verification_failure_changes_neither_file(self):
        self.legacy_config()
        before = self.connector.read_bytes(), self.registry.read_bytes()

        def fail():
            raise RuntimeError("synthetic target failure")

        with self.peer(fail):
            with self.assertRaisesRegex(RuntimeError, "enrollment refused"):
                manage(self.doc, self.args)
        self.assertEqual((self.connector.read_bytes(), self.registry.read_bytes()), before)

    def test_disable_and_repeat_enrollment(self):
        with self.peer():
            manage(self.doc, self.args)
            manage(self.doc, self.args)
        self.assertEqual(len(config.read_private(self.registry)["hosts"]), 1)
        self.args.operation = "disable"
        with patch.object(Fleet, "_new_client", side_effect=AssertionError("unexpected SSH")):
            self.assertEqual(manage(self.doc, self.args)["state"], "disabled")
        self.assertFalse(config.read_private(self.registry)["hosts"][0]["enabled"])

    def test_config_lock_covers_final_read_and_both_replacements(self):
        self.legacy_config()
        read, replace = config.read_private, os.replace
        reads, replacements = [], []

        def assert_locked():
            with self.connector.with_name(".config.json.lock").open("w") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def checked_read(path):
            if path == self.connector:
                reads.append(path)
                if len(reads) == 2:
                    assert_locked()
            return read(path)

        def checked_replace(source, target):
            if target in (self.connector, self.registry):
                assert_locked()
                replacements.append(target)
            return replace(source, target)

        with self.peer(), patch.object(config, "read_private", checked_read), \
             patch.object(config.os, "replace", checked_replace):
            manage(self.doc, self.args)
        self.assertEqual(len(reads), 2)
        self.assertEqual(replacements, [self.connector, self.registry])

    def test_private_writer_waits_until_enrollment_commit(self):
        self.legacy_config()
        replace, flock = os.replace, fcntl.flock
        attempted, finished = threading.Event(), threading.Event()
        errors, blocked = [], []

        def owner_edit():
            try:
                doc = config.read_private(self.connector)
                doc["owner_note"] = "retained after enrollment"
                config.write_private(self.connector, json.dumps(doc), replace=True)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        writer = threading.Thread(target=owner_edit, daemon=True)

        def checked_flock(file, operation):
            if threading.current_thread() is writer:
                try:
                    flock(file, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.append(True)
                finally:
                    attempted.set()
            return flock(file, operation)

        def checked_replace(source, target):
            if target == self.registry:
                writer.start()
                self.assertTrue(attempted.wait(5), "owner writer did not attempt the shared lock")
                self.assertEqual(blocked, [True])
                self.assertFalse(finished.is_set())
            return replace(source, target)

        try:
            with self.peer(), patch.object(config.fcntl, "flock", checked_flock), \
                 patch.object(config.os, "replace", checked_replace):
                manage(self.doc, self.args)
                self.assertTrue(finished.wait(5), "owner writer did not resume after commit")
        finally:
            if writer.ident is not None:
                writer.join(5)
        self.assertEqual(errors, [])
        current = config.read_private(self.connector)
        self.assertEqual(current["owner_note"], "retained after enrollment")
        self.assertEqual(current["identity"], self.expected)
        self.assertEqual(len(config.read_private(self.registry)["hosts"]), 1)

    def test_private_file_and_lock_custody_guards(self):
        for lock in (False, True):
            for kind in ("symlink", "hardlink", "public", "directory", "fifo"):
                with self.subTest(lock=lock, kind=kind):
                    target = self.root / (str(lock) + "-" + kind + ".json")
                    unsafe = target.with_name("." + target.name + ".lock") if lock else target
                    if kind == "symlink":
                        unsafe.symlink_to(self.connector)
                    elif kind == "hardlink":
                        os.link(self.connector, unsafe)
                    elif kind == "public":
                        unsafe.write_text("{}")
                        unsafe.chmod(0o644)
                    elif kind == "directory":
                        unsafe.mkdir()
                    else:
                        os.mkfifo(unsafe, 0o600)
                    before = self.connector.read_bytes()
                    try:
                        with self.assertRaises((ValueError, OSError)):
                            config.write_private(target, "{}", replace=not lock)
                        self.assertEqual(self.connector.read_bytes(), before)
                    finally:
                        unsafe.rmdir() if kind == "directory" else unsafe.unlink()

    def test_owner_config_becoming_unsafe_during_probe_prevents_enrollment(self):
        before = self.registry.read_bytes()
        with self.peer(lambda: self.connector.chmod(0o644)):
            with self.assertRaisesRegex(ValueError, "owner-only regular file"):
                manage(self.doc, self.args)
        self.assertEqual(self.registry.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
