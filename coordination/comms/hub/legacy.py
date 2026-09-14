"""Read-only legacy conversion with stable import requests; originals stay intact."""
import hashlib
import json
from pathlib import Path


def prepare_import(path, recipients, scope="/", work_id=None):
    """Return a request that imports historical content without replaying old work."""
    source = Path(path).resolve()
    raw = source.read_bytes()
    if len(raw) > 128 * 1024:
        raise ValueError("Legacy record exceeds 128 KiB")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Legacy record must be an object")
    digest = hashlib.sha256(raw).hexdigest()
    body = data.get("body", data.get("text", ""))
    if not isinstance(body, str) or len(body.encode()) > 16 * 1024:
        raise ValueError("Legacy body must be text of at most 16 KiB")
    original_id = data.get("msgId", data.get("msg_id", data.get("id", digest)))
    artifacts = data.get("artifacts", [])
    if not isinstance(artifacts, list) or not all(isinstance(v, str) for v in artifacts):
        raise ValueError("Legacy artifacts must be a list of strings")
    if not recipients or not all(isinstance(v, str) and v for v in recipients):
        raise ValueError("Explicit registered recipients are required")
    params = {"to": recipients, "kind": "information", "scope": scope,
              "subject": str(data.get("subject") or "Imported historical message")[:200],
              "body": body, "artifacts": artifacts + [str(source), "sha256:" + digest,
                                                        "legacy-id:" + str(original_id)]}
    historical_work = work_id or data.get("work_id", data.get("workId"))
    if historical_work:
        params["work_id"] = str(historical_work)
    return {"operation": "messages.send", "params": params, "request_id": "legacy:" + digest}


def import_record(store, actor, path, recipients, scope="/", work_id=None):
    request = prepare_import(path, recipients, scope, work_id)
    return store.call(actor, **request)
