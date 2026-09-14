"""Local operator maintenance proof, never a browser RPC or caller marker.

Only a pre-boot native intent can authorize this separate containment method.
Original journals remain immutable; a private retirement receipt is additive.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import uuid

from .supervisor import SupervisorError, _boot_identity, _private_dir


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _native():
    if sys.platform != 'darwin':
        raise SupervisorError('boot_platform_unverified')
    result = subprocess.run(['/usr/sbin/ioreg', '-a', '-r', '-d', '1', '-c', 'IOPlatformExpertDevice'],
                            capture_output=True, timeout=10, check=True)
    if result.stderr or len(result.stdout) > 65536:
        raise SupervisorError('hardware_unverified')
    rows = plistlib.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1:
        raise SupervisorError('hardware_unverified')
    hardware = str(uuid.UUID(rows[0]['IOPlatformUUID']))
    boot = str(uuid.UUID(_boot_identity()))
    return dict(hardware=hardware, boot=boot)


def _read(path, maximum=1048576):
    path = Path(path)
    if path.resolve() != path:
        raise SupervisorError('unsafe_maintenance_path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > maximum):
            raise SupervisorError('unsafe_maintenance_file')
        data = stream.read(maximum + 1)
        if len(data) > maximum: raise SupervisorError('maintenance_file_too_large')
        return data


def _write_once(path, data):
    # No replacement of an operator intent, original journal or prior receipt.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _paths(supervisor, key):
    # Keys originate only from an existing journal basename.
    if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
        raise SupervisorError('invalid_journal_key')
    directory = supervisor.state_dir / 'boot-retirement'
    if directory.exists():
        _private_dir(directory)
    return directory, directory/(key+'.intent.json'), directory/(key+'.receipt.json')


def _configuration(supervisor):
    profiles = supervisor.config.get('profiles', {})
    auth = supervisor.config.get('auth_states', {})
    if not isinstance(profiles, dict) or not isinstance(auth, dict):
        raise SupervisorError('profile_config_unverified')
    hashes = {}
    for name, value in profiles.items():
        path = Path(value)
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise SupervisorError('profile_config_unverified')
        info = path.stat()
        if info.st_uid != os.getuid():
            raise SupervisorError('profile_config_unverified')
        hashes['profile:'+name] = _digest(_encoded([str(path), info.st_dev, info.st_ino]))
    for name, value in auth.items():
        hashes['auth:'+name] = _digest(_read(Path(value)))
    return dict(config_sha256=_digest(_encoded(supervisor.config)), profile_hashes=hashes)


def _unoccupied(supervisor):
    # Refuse stale Singleton markers too: interpreting/removing them is separate
    # operator maintenance, not evidence manufactured by boot recovery.
    for value in supervisor.config.get('profiles', {}).values():
        path = Path(value)
        if any(os.path.lexists(path/name) for name in ('SingletonLock','SingletonCookie','SingletonSocket','DevToolsActivePort')):
            raise SupervisorError('profile_occupancy_unknown')
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '-t', '+D', str(path)],
                                capture_output=True, timeout=15)
        if result.returncode != 1 or result.stdout or result.stderr:
            raise SupervisorError('profile_occupancy_unknown')


def _validate_record(supervisor, path, record, lease_id, generation, old_boot):
    if (not isinstance(lease_id, str) or not lease_id or record.get('lease_id') != lease_id or type(generation) is not int or generation < 1
            or type(record.get('generation')) is not int or record.get('generation') != generation or record.get('nonce') != path.stem
            or record.get('status') == 'clean'):
        raise SupervisorError('boot_scope_mismatch')
    identities = record.get('identities')
    if (not isinstance(identities, list) or not identities
            or any(not isinstance(i, dict) or not isinstance(i.get('boot'), str)
                   or i['boot'].lower() != old_boot.lower() for i in identities)):
        raise SupervisorError('boot_identity_unverified')
    expected = supervisor.scratch_dir/path.stem
    if record.get('scratch') != str(expected) or expected.resolve() != expected or expected.is_symlink():
        raise SupervisorError('unsafe_scratch_path')


def prepare(supervisor, lease_id, generation, profile_id):
    """Trusted LOCAL operator only, with the ordinary service stopped.

    No supplied path, hardware identity, boot value, hash or occupancy override.
    The supervisor lifetime lock and no-loaded-task requirement fence preparation.
    """
    with supervisor._close_lock, supervisor._mutex:
        if supervisor._closed or supervisor._closing or supervisor._tasks:
            raise SupervisorError('maintenance_requires_idle_controller')
        if (not isinstance(profile_id, str) or profile_id not in supervisor.config.get('profiles', {})):
            raise SupervisorError('legacy_profile_binding_required')
        matches = []
        for path in supervisor.journal_dir.glob('*.json'):
            raw = _read(path); record = json.loads(raw)
            if record.get('lease_id') == lease_id: matches.append((path, raw, record))
        if len(matches) != 1: raise SupervisorError('boot_scope_mismatch')
        path, raw, record = matches[0]
        native = _native()
        _validate_record(supervisor,path,record,lease_id,generation,native['boot'])
        config = _configuration(supervisor)
        directory, intent_path, _ = _paths(supervisor,path.stem)
        _private_dir(directory)
        intent = dict(schema=1, journal_path=str(path), journal_sha256=_digest(raw),
                      journal_preimage=raw.decode('utf-8'), lease_id=lease_id,generation=generation,
                      native=native, configuration=config, profile_id=profile_id)
        if _native() != native: raise SupervisorError('native_identity_changed')
        _write_once(intent_path,_encoded(intent))
        return dict(lease_id=lease_id,generation=generation,intent_sha256=_digest(_encoded(intent)))


def _intent(supervisor, path, record):
    _, intent_path, receipt_path = _paths(supervisor,path.stem)
    raw = _read(intent_path, 2097152); intent = json.loads(raw)
    preimage = intent['journal_preimage'].encode('utf-8')
    if (intent.get('schema') != 1 or intent.get('journal_path') != str(path)
            or _digest(preimage) != intent.get('journal_sha256')
            or _read(path) != preimage or json.loads(preimage) != record):
        raise SupervisorError('boot_preimage_changed')
    journals = list(supervisor.journal_dir.glob('*.json'))
    if len(journals) > 1024 or sum(json.loads(_read(p)).get('lease_id') == intent['lease_id'] for p in journals) != 1:
        raise SupervisorError('boot_scope_collision')
    native = _native()
    if native['hardware'] != intent['native']['hardware'] or native['boot'] == intent['native']['boot']:
        raise SupervisorError('boot_transition_unverified')
    _validate_record(supervisor,path,record,intent['lease_id'],intent['generation'],intent['native']['boot'])
    return intent, raw, receipt_path, native


def selected(supervisor, path, record=None):
    directory = supervisor.state_dir/'boot-retirement'
    if not os.path.lexists(directory): return False
    _, intent_path, _ = _paths(supervisor,path.stem)
    if os.path.lexists(intent_path): return True
    if record is not None:
        # A duplicate journal for a selected lease is also fenced: it must not
        # fall through to ordinary PID cleanup when its selected proof rejects.
        try:
            for intent in directory.glob('*.intent.json'):
                if json.loads(_read(intent,2097152)).get('lease_id') == record.get('lease_id'):
                    return True
        except Exception:
            return True
    return False


def recover(supervisor, path, record):
    """No process identity reads, adoption or signals in this path."""
    try:
        intent, raw, receipt_path, native = _intent(supervisor,path,record)
        if receipt_path.exists():
            sealed = json.loads(_read(receipt_path))
            if (sealed.get('intent_sha256') != _digest(raw)
                    or sealed.get('native', {}).get('hardware') != native['hardware']
                    or not sealed.get('native', {}).get('boot')
                    or sealed['native']['boot'] == intent['native']['boot']
                    or sealed.get('receipt') != _receipt(intent)):
                raise SupervisorError('boot_receipt_unverified')
            return sealed['receipt']
        if (intent.get('profile_id') not in supervisor.config.get('profiles', {})
                or _configuration(supervisor) != intent.get('configuration')):
            raise SupervisorError('boot_configuration_changed')
        _unoccupied(supervisor)
        if _native() != native or _configuration(supervisor) != intent['configuration']:
            raise SupervisorError('native_identity_changed')
        # Recheck the exact preimage immediately before bounded deletion.
        if _digest(_read(path)) != intent['journal_sha256']:
            raise SupervisorError('boot_preimage_changed')
        scratch = supervisor.scratch_dir/path.stem
        if scratch.resolve() != scratch or scratch.is_symlink():
            raise SupervisorError('unsafe_scratch_path')
        if scratch.exists(): shutil.rmtree(scratch)
        if scratch.exists(): raise SupervisorError('scratch_cleanup_failed')
        receipt = _receipt(intent)
        _write_once(receipt_path,_encoded(dict(intent_sha256=_digest(raw),native=native,receipt=receipt)))
        return receipt
    except (OSError, ValueError, KeyError, TypeError, SupervisorError, subprocess.SubprocessError):
        return dict(lease_id=record.get('lease_id'),clean=False,status='quarantined',
                    reason_code='boot_retirement_unproved',remaining_pids=[],close_acknowledged=False)


def _receipt(intent):
    return dict(lease_id=intent['lease_id'],generation=intent['generation'],clean=True,status='clean',
                reason_code='boot_transition',containment_method='boot_transition',
                remaining_pids=[],close_acknowledged=False,preimage_sha256=intent['journal_sha256'])


def pending(supervisor):
    directory = supervisor.state_dir/'boot-retirement'
    if not directory.exists(): return False
    for intent in directory.glob('*.intent.json'):
        path = supervisor.journal_dir/(intent.name.removesuffix('.intent.json')+'.json')
        try:
            record = json.loads(_read(path))
            _, _, receipt, _ = _intent(supervisor,path,record)
            if not receipt.exists() or recover(supervisor,path,record).get('clean') is not True:
                return True
        except Exception:
            return True
    return False


def close_receipts(supervisor):
    """An unproved selected intent cannot disappear behind an empty close."""
    directory = supervisor.state_dir/'boot-retirement'
    if not directory.exists(): return []
    receipts = []
    for intent in directory.glob('*.intent.json'):
        path = supervisor.journal_dir/(intent.name.removesuffix('.intent.json')+'.json')
        try:
            receipts.append(recover(supervisor,path,json.loads(_read(path))))
        except Exception:
            receipts.append(dict(clean=False,status='quarantined',reason_code='boot_retirement_unproved',remaining_pids=[],close_acknowledged=False))
    return receipts
