"""Publish this probe invocation into the existing single manager source."""
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from collections.abc import Mapping
from borg_config import CONFIG

BASE = CONFIG.mem0_root
TARGET = Path(os.environ['BORG_MEMORY_HEALTH_OUTPUT']).expanduser() if os.environ.get('BORG_MEMORY_HEALTH_OUTPUT') else CONFIG.data_root / 'memory-health.json'
RECALL_CHECKS = {'route', 'auth', 'tools_list', 'positive_canary', 'negative_canary'}
STATES = {'PASS', 'FAIL', 'UNKNOWN', 'NOT_RUN'}
ISO_CLOCK = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z')
STORE_ID = 'memory.mem0.store'
STORE_STATUSES = {'available', 'denied', 'rejected', 'unavailable', 'malformed', 'proof_unavailable'}


def remember(row, previous):
    """Previous evidence supplies history only, never current probe success."""
    state = previous.get('state')
    if state in {'PASS', 'FAIL'}:
        row['last_observed_state'] = state
        row['last_observed_at'] = previous.get('observed_at')
    elif previous.get('last_observed_state') in {'PASS', 'FAIL'}:
        row['last_observed_state'] = previous['last_observed_state']
        row['last_observed_at'] = previous.get('last_observed_at', previous.get('observed_at'))


def _strict_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _strict_scope_counts(value):
    if not isinstance(value, dict):
        return None
    clean = {}
    for scope, count in value.items():
        if not isinstance(scope, str) or not scope or len(scope) > 80 or any(ord(char) < 32 for char in scope):
            return None
        parsed = _strict_count(count)
        if parsed is None:
            return None
        clean[scope] = parsed
    return clean


def _safe_detail_text(value, default, limit=160):
    if isinstance(value, str) and 0 < len(value) <= limit and not any(ord(char) < 32 for char in value):
        return value
    return default


def _store_detail(details):
    if not isinstance(details, Mapping):
        return None
    for key in (STORE_ID, 'store_stats', 'store'):
        candidate = details.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _normalize_store_detail(raw):
    """Keep only credential-free, principal-scoped count metadata."""

    if not isinstance(raw, Mapping):
        return {'state': 'UNKNOWN', 'status': 'proof_unavailable', 'reason': 'measurement_missing'}
    raw_state = raw.get('state')
    raw_status = raw.get('status')
    state = raw_state if isinstance(raw_state, str) and raw_state in STATES else 'UNKNOWN'
    status = raw_status if isinstance(raw_status, str) and raw_status in STORE_STATUSES else 'malformed'
    normalized = {
        'state': state,
        'status': status,
        'count_semantics': _safe_detail_text(
            raw.get('count_semantics'),
            'principal-visible non-canary mem0 collection records; active/retired status not inferred',
        ),
        'scope_semantics': _safe_detail_text(
            raw.get('scope_semantics'),
            "by_scope is limited to this principal's visible scopes",
        ),
        'atomic': raw.get('atomic') if isinstance(raw.get('atomic'), bool) else False,
    }
    principal = raw.get('principal')
    if isinstance(principal, str) and 0 < len(principal) <= 80 and not any(ord(char) < 32 for char in principal):
        normalized['principal'] = principal
    reason = raw.get('reason')
    if isinstance(reason, str) and 0 < len(reason) <= 160 and not any(ord(char) < 32 for char in reason):
        normalized['reason'] = reason

    if state == 'PASS' and status != 'available':
        normalized.update(state='FAIL' if status in {'denied', 'rejected'} else 'UNKNOWN')
        normalized['reason'] = normalized.get('reason', f'status_{status}')
    if normalized['state'] == 'PASS':
        visible = _strict_count(raw.get('visible'))
        if visible is None:
            normalized.update(state='UNKNOWN', status='malformed', reason='visible_count_invalid_or_missing')
            return normalized
        normalized['visible'] = visible
        if 'by_scope' in raw:
            scope_counts = _strict_scope_counts(raw.get('by_scope'))
            if scope_counts is None:
                normalized.update(state='UNKNOWN', status='malformed', reason='by_scope_invalid')
                normalized.pop('visible', None)
                return normalized
            if sum(scope_counts.values()) != visible:
                normalized.update(state='UNKNOWN', status='malformed', reason='scoped_sum_mismatch')
                normalized.pop('visible', None)
                return normalized
            normalized['by_scope'] = scope_counts
    return normalized


def _remember_store_detail(row, previous):
    if not isinstance(previous, Mapping):
        return
    previous_details = previous.get('details')
    if isinstance(previous_details, Mapping) and any(
        key in previous_details for key in ('visible', 'count', 'by_scope')
    ):
        row['last_observed_details'] = copy.deepcopy(previous_details)
        if isinstance(previous_details.get('observed_at'), str):
            row['last_observed_at'] = previous_details['observed_at']
        return

    coverage = previous.get('coverage')
    candidates = [coverage] if isinstance(coverage, Mapping) else []
    if isinstance(coverage, Mapping):
        candidates.extend(
            candidate
            for key in ('measurement', 'current_probe')
            for candidate in [coverage.get(key)]
            if isinstance(candidate, Mapping)
        )
    fallback_at = previous.get('observed_at')
    for candidate in candidates:
        source = candidate.get('measurement') if isinstance(candidate.get('measurement'), Mapping) else candidate
        if not any(key in source for key in ('visible', 'count', 'by_scope')):
            continue
        historical = {
            key: copy.deepcopy(source[key])
            for key in ('principal', 'visible', 'count', 'by_scope')
            if key in source
        }
        observed_at = source.get('observed_at')
        if not isinstance(observed_at, str):
            for key in ('last_read_at', 'lastread'):
                if isinstance(source.get(key), str):
                    observed_at = source[key]
                    break
        if not isinstance(observed_at, str) and isinstance(fallback_at, str):
            observed_at = fallback_at
        if isinstance(observed_at, str):
            historical['observed_at'] = observed_at
            row['last_observed_at'] = observed_at
        row['last_observed_details'] = historical
        return
    historical = {
        key: copy.deepcopy(previous[key])
        for key in ('principal', 'visible', 'count', 'by_scope', 'observed_at')
        if key in previous
    }
    if historical:
        row['last_observed_details'] = historical


def _project_store(components, prior, details, run_id, started_at):
    row = components.get(STORE_ID)
    if row is None:
        return
    previous = prior.get(STORE_ID, row.copy())
    remember(row, previous)
    _remember_store_detail(row, previous)
    raw = _store_detail(details)
    current = _normalize_store_detail(raw)
    measurement_at = raw.get('observed_at') if isinstance(raw, Mapping) else None
    if not isinstance(measurement_at, str) or not ISO_CLOCK.fullmatch(measurement_at):
        measurement_at = started_at.isoformat()
    current['measured_at'] = measurement_at
    current['observed_at'] = started_at.isoformat()
    current['run_id'] = run_id
    row['details'] = current
    row.update(
        state=current['state'],
        observed_at=started_at.isoformat(),
        root_cause=None if current['state'] == 'PASS' else 'current-store-measurement-incomplete-or-failed',
    )
    row.setdefault('coverage', {})['current_probe'] = {
        'run_id': run_id,
        'measurement': copy.deepcopy(current),
        'principal': current.get('principal', 'door-health'),
        'boundary': 'authenticated memory_stats read; principal-visible collection availability/count only',
    }


def age_components(components, now):
    for row in components:
        if row.get('state') not in {'PASS', 'FAIL'}:
            continue
        value = row.get('observed_at')
        try:
            if not isinstance(value, str) or not ISO_CLOCK.fullmatch(value):
                raise ValueError('noncanonical clock')
            at = datetime.fromisoformat(value.replace('Z', '+00:00'))
            stale = at > now or (now-at).total_seconds() > row['freshness_budget_s']
        except (ValueError, TypeError, KeyError):
            stale = True
        if stale:
            remember(row, row.copy())
            row.update(state='UNKNOWN', stale_at_generated=True)
        else:
            row.pop('stale_at_generated', None)
        row.pop('stale_age_s', None)


def project(baseline, checks, run_id, started_at, now, previous=None, details=None):
    # Current clocks are UTC datetime objects made by the probe, not parsed
    # Denver wall-clock strings from an append-only journal.
    if any(not isinstance(t, datetime) or t.utcoffset() != timedelta(0) for t in (started_at, now)):
        raise ValueError('current run clocks must be aware UTC datetimes')
    if started_at > now or str(uuid.UUID(run_id)) != run_id:
        raise ValueError('invalid current run identity')
    if (previous or {}).get('refresh', {}).get('run_id') == run_id:
        raise ValueError('current run already published')
    out = copy.deepcopy(baseline)
    # Freeze the baseline's validity at its own assembly time. A future-dated
    # observation cannot become valid merely because publication happens later.
    generated = out.get('generated_at')
    if not isinstance(generated, str) or not ISO_CLOCK.fullmatch(generated):
        for row in out['components']:
            remember(row, row.copy())
            row.update(state='UNKNOWN', baseline_clock_invalid=True)
    else:
        age_components(out['components'], datetime.fromisoformat(generated.replace('Z','+00:00')))
    out.update(mode='collector_snapshot', generated_at=now.isoformat())
    out.pop('adoption', None)
    out.pop('live_changes_applied', None)
    out['refresh'] = {'producer':'existing memory-door-health invocation', 'run_id':run_id,
                      'started_at':started_at.isoformat(), 'completed_at':now.isoformat(),
                      'canonical_source':str(TARGET), 'model_calls':0, 'new_scanner':False}
    components = {c['id']:c for c in out['components']}
    prior = {c['id']:c for c in (previous or out).get('components', [])}
    checks = {k:v for k,v in checks.items() if isinstance(k,str) and v in STATES}
    for key, required in [('memory.mem0.recall', RECALL_CHECKS),
                          ('memory.graphiti.ingestion', {'graph_dependency','graph_freshness'})]:
        row = components[key]
        remember(row, prior.get(key, row.copy()))
        current = {k:checks[k] for k in sorted(required) if k in checks}
        values = set(current.values())
        state = 'UNKNOWN' if not required.issubset(current) else 'FAIL' if 'FAIL' in values else 'PASS' if values=={'PASS'} else 'UNKNOWN'
        # A current watermark failure proves a scoped freshness defect. Its
        # success alone cannot prove that the whole graph backlog was ingested.
        if key == 'memory.graphiti.ingestion' and state == 'PASS':
            state = 'UNKNOWN'
        row.update(state=state, observed_at=started_at.isoformat(),
                   root_cause=None if state=='PASS' else 'current-probe-incomplete-or-failed' if key.endswith('recall') else 'scoped-graph-freshness-or-backlog-unproved')
        row.setdefault('coverage', {})['current_probe'] = {'run_id':run_id,'checks':current,
              'principal':'door-health','allowed_scopes':['ops','team:project'],
              'boundary':'authenticated HTTP read' if key.endswith('recall') else 'allowed graph groups availability and ingestion watermark; full backlog unproved'}
        if state in {'PASS','FAIL'}:
            remember(row, row.copy())
    _project_store(components, prior, details, run_id, started_at)
    age_components(out['components'], now)
    out['status_summary'] = {s:sum(c['state']==s for c in out['components']) for s in sorted(STATES)}
    return out


def publish(checks, run_id, started_at, *, base=BASE, target=TARGET, now=None, details=None):
    """One atomic, synced file; lock contention is an explicit failure."""
    now = now or datetime.now(timezone.utc)
    baseline_path = base/'data/memory-health-baseline.json'
    raw_baseline = baseline_path.read_bytes()
    baseline = json.loads(raw_baseline)
    contract = Path(baseline['contract']).read_bytes()
    if hashlib.sha256(contract).hexdigest() != baseline['contract_sha256']:
        raise ValueError('baseline contract binding changed')
    target.parent.mkdir(parents=True, exist_ok=True)
    with (base/'data/memory-health-publish.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = json.loads(target.read_text()) if target.exists() else None
        out = project(baseline, checks, run_id, started_at, now, previous, details)
        # Reuse this existing scheduled invocation, never the request path.
        # Queue availability cannot promote native capture/recall acceptance.
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "memory_queue_projection", Path(__file__).with_name("memory_queue_projection.py"))
            queue_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(queue_module)
            out["queue_metadata"] = queue_module.collect()
        except Exception:
            out["queue_metadata"] = {"schema": "memory-queue-projection/v1",
                "published_at": datetime.now(timezone.utc).isoformat(), "machines": [],
                "host_coverage": {"observed": 0, "denominator": 7, "attempted": 3},
                "error": "queue_projection_unavailable"}
        out['refresh']['baseline_sha256'] = hashlib.sha256(raw_baseline).hexdigest()
        out['refresh']['canonical_source'] = str(target)
        raw = (json.dumps(out, indent=2)+'\n').encode()
        temporary = target.with_name(target.name+'.'+run_id+'.next')
        try:
            with temporary.open('xb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    return {'run_id':run_id,'source':str(target),'sha256':hashlib.sha256(raw).hexdigest()}
