"""Deliver pending Inbox work at an existing tool boundary, without starting a turn."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from . import native
from .service import MAX_REQUEST_BYTES, _atomic_write_json


CHECK_INTERVAL_SECONDS = 5


def _read_json(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError("Active checkpoint state exceeds its bound")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Active checkpoint state must be an object")
    return value


def _session_connection(config_path, runtime, raw):
    """Resolve only the child connection issued by the normal native checkpoint."""
    config_path = Path(config_path).expanduser().resolve()
    config = _read_json(config_path)
    actor = config.get("agent_id") or config.get("principal")
    session = raw.get("agentId") or raw.get("agent_id") or raw.get("session_id") or raw.get("sessionId") \
        or raw.get("thread_id") or raw.get("threadId")
    if not isinstance(actor, str) or not actor or not isinstance(session, str) or not session:
        raise ValueError("A native session and authenticated parent are required")
    child_id = "session-" + hashlib.sha256((actor + ":" + runtime + ":" + session).encode()).hexdigest()[:32]
    state = config_path.parent / ".native" / hashlib.sha256(actor.encode()).hexdigest()[:16]
    child_path = state / ".client" / "children" / (
        hashlib.sha256(child_id.encode()).hexdigest()[:24] + ".credential.config.json")
    child = _read_json(child_path)
    if child.get("agent_id") != child_id or child.get("endpoint") != config.get("endpoint"):
        raise ValueError("Native child connection does not match this session")
    credential = Path(child["credential_file"]).resolve()
    credential.relative_to((state / ".client" / "children").resolve())
    return config_path, child_path, child_id, state


def active_checkpoint(config_path, runtime, machine, raw, *, lease_seconds=None, clock=time.time):
    if runtime not in {"codex", "claude-code"} or raw.get("hook_event_name") != "PostToolUse":
        return {}
    # SessionStart/UserPromptSubmit own provisioning. An unavailable child
    # never falls back to its parent's authority or creates a second identity.
    config_path, child_path, child_id, state_root = _session_connection(config_path, runtime, raw)
    cache_root = state_root / ".active-checkpoints"
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(child_id.encode()).hexdigest()[:24]
    cache_path = cache_root / (key + ".json")
    descriptor = os.open(cache_root / (key + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NONBLOCK, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {}
        try:
            prior = _read_json(cache_path)
        except (FileNotFoundError, ValueError):
            prior = {}
        turn_id = raw.get("turn_id") or raw.get("turnId")
        tool_id = raw.get("tool_use_id") or raw.get("toolUseId")
        now = clock()
        same_turn = bool(prior) and prior.get("turn_id") == turn_id
        elapsed = now - prior.get("checked_at", 0) if isinstance(prior.get("checked_at", 0), (int, float)) else None
        if same_turn and ((tool_id and prior.get("tool_id") == tool_id)
                          or (elapsed is not None and 0 <= elapsed < CHECK_INTERVAL_SECONDS)):
            return {}
        client = native.HookClient.from_config(child_path)
        pending = client.call_sync("messages.list", {"view": "pending_metadata", "limit": 1})
        if pending.get("schema") != "inbox-pending/v1" or pending.get("recipient") != child_id:
            return {}
        counts = pending.get("counts") or pending.get("counts_lower_bound") or {}
        ready = counts.get("ready")
        if type(ready) is not int or ready < 0:
            return {}
        digest = pending.get("digest")
        complete = pending.get("coverage", {}).get("complete") is True
        current = {"turn_id": turn_id, "tool_id": tool_id, "checked_at": now,
                   "pending_digest": digest}
        # A lower bound of zero or an unchanged scanned prefix says nothing
        # about a newer hold outside that scan. Reconcile incomplete views.
        if complete and (ready == 0 or (same_turn and digest and prior.get("pending_digest") == digest)):
            _atomic_write_json(cache_path, current, mode=0o600)
            return {}
        # A fresh normal poll provides current assignment/hold/authority and
        # lease evidence. The summary only selects whether to run that poll.
        payload = dict(raw)
        if isinstance(tool_id, str) and tool_id:
            payload["event_id"] = "post-tool:" + str(turn_id or "") + ":" + tool_id
        context = native.checkpoint(config_path, runtime, machine, payload, "checkpoint",
                                    lease_seconds=lease_seconds)
        if context.get("identity", {}).get("agent_id") != child_id:
            return {}
        if not context.get("messages"):
            return {}
        context["active_delivery"] = {"schema": "inbox-active-checkpoint/v1", "source": "PostToolUse",
                                      "turn_id": turn_id, "tool_use_id": tool_id, "new_turn_started": False}
        envelope = native.render_context(runtime, "PostToolUse", context)
        # Persist only after a valid bounded envelope exists. An interrupted
        # consumer retains the Hub lease and becomes retryable at its expiry.
        _atomic_write_json(cache_path, current, mode=0o600)
        return envelope
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--phase", choices=("checkpoint",), default="checkpoint")
    parser.add_argument("--lease-seconds", type=int)
    args = parser.parse_args(argv)
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Hook input exceeds its bound")
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("Hook input must be an object")
        result = active_checkpoint(args.config, args.runtime, args.machine, raw,
                                   lease_seconds=args.lease_seconds)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        # The existing executable imposes the same six-second hard deadline.
        # Do not expose a prompt, exception or credential, or stop active work.
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
