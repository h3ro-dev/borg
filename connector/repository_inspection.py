"""Bounded, read-only repository observations. No memory or service operations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _needles(terms: str) -> list[str]:
    parts = terms.casefold().replace("/", " ").replace("-", " ").split()
    return list(dict.fromkeys("vuplicity" if p == "cra" else p for p in parts if len(p) > 1))


def _git_observation(repo: Path, args: tuple[str, ...], deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"status": "NOT_INSPECTED", "value": None, "reason": "budget_exhausted"}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false",
             "-c", "core.untrackedCache=false", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=min(3.0, remaining), check=False)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "value": None, "reason": "command_timeout"}
    except (OSError, UnicodeError):
        return {"status": "UNAVAILABLE", "value": None, "reason": "execution_unavailable"}
    if result.returncode != 0:
        return {"status": "UNAVAILABLE", "value": None, "reason": "git_error",
                "exit_code": result.returncode}
    # Strip only line terminators: leading spaces in porcelain status carry meaning.
    return {"status": "OBSERVED", "value": result.stdout.rstrip("\r\n")}


def _repo_snapshot(repo: Path, deadline: float) -> dict:
    row = {"path": str(repo), "status": "NOT_INSPECTED", "observed_at": _now(),
           "branch": None, "head": None, "latest_commit": None,
           "dirty_paths": None, "dirty_count": None,
           "inspection_complete": False, "inspection_budget_exhausted": False,
           "field_status": {}, "field_errors": {}}
    if time.monotonic() >= deadline:
        row["inspection_budget_exhausted"] = True
        return row
    try:
        if not repo.is_dir() or repo.resolve() != repo or not (repo / ".git").exists():
            row["status"] = "UNAVAILABLE"
            row["unavailable_reason"] = "repository_path_unavailable"
            return row
    except OSError:
        row["status"] = "UNAVAILABLE"
        row["unavailable_reason"] = "repository_path_unavailable"
        return row
    # Cheap identity precedes expensive log/status. Each command has its own
    # fair share, so a slow field cannot consume every subsequent field's time.
    commands = [("head", ("rev-parse", "--verify", "HEAD")),
                ("branch", ("branch", "--show-current")),
                ("latest_commit", ("log", "-1", "--format=%H%x00%cI%x09%s")),
                ("dirty", ("status", "--short"))]
    observations = {}
    for index, (field, args) in enumerate(commands):
        now = time.monotonic()
        share = max(0.0, deadline - now) / (len(commands) - index)
        observations[field] = _git_observation(repo, args, min(deadline, now + share))
    for field, observation in observations.items():
        row["field_status"][field] = observation["status"]
        if observation.get("reason"):
            row["field_errors"][field] = {k: observation[k] for k in ("reason", "exit_code") if k in observation}
    row["head"] = observations["head"]["value"]
    row["branch"] = observations["branch"]["value"]
    latest = observations["latest_commit"]["value"]
    if latest is not None:
        if "\x00" not in latest:
            row["field_status"]["latest_commit"] = "UNAVAILABLE"
            row["field_errors"]["latest_commit"] = {"reason": "unexpected_git_output"}
        else:
            log_head, detail = latest.split("\x00", 1)
            if row["head"] is not None and log_head != row["head"]:
                row["field_status"]["latest_commit"] = "INCONSISTENT"
                row["field_errors"]["latest_commit"] = {"reason": "head_changed_during_inspection"}
            else:
                row["latest_commit"] = detail
    dirty_text = observations["dirty"]["value"]
    if dirty_text is not None:
        dirty = dirty_text.splitlines()
        row["dirty_paths"], row["dirty_count"] = dirty[:25], len(dirty)
    row["inspection_complete"] = (bool(row["head"]) and row["branch"] is not None
        and row["latest_commit"] is not None and row["dirty_count"] is not None
        and all(v == "OBSERVED" for v in row["field_status"].values()))
    statuses = set(row["field_status"].values())
    if row["inspection_complete"]:
        row["status"] = "OBSERVED"
    elif "OBSERVED" in statuses:
        row["status"] = "PARTIAL"
    elif statuses <= {"NOT_INSPECTED", "TIMEOUT"}:
        row["status"] = "NOT_INSPECTED" if statuses == {"NOT_INSPECTED"} else "TIMEOUT"
    else:
        row["status"] = "UNAVAILABLE"
    row["inspection_budget_exhausted"] = (time.monotonic() >= deadline or
        bool(statuses & {"TIMEOUT", "NOT_INSPECTED"}))
    row["observed_at"] = _now()
    return row


def _repos_matching(terms: str, roots: tuple[Path, ...], limit: int = 8,
                    budget_seconds: float = 8.0) -> list[dict]:
    if limit <= 0 or budget_seconds <= 0:
        return []
    needles = _needles(terms)
    deadline = time.monotonic() + budget_seconds
    found = []
    for root in roots:
        if time.monotonic() >= deadline:
            break
        try:
            if not root.is_dir() or root.resolve() != root:
                continue
            for repo in root.iterdir():
                if time.monotonic() >= deadline:
                    break
                # Reject irrelevant names before doing per-entry filesystem work.
                name = repo.name.casefold().replace("-", " ")
                if needles and not any(n in name for n in needles):
                    continue
                try:
                    if not repo.is_dir() or repo.resolve() != repo or not (repo / ".git").exists():
                        continue
                    canonical = any(repo.name.casefold() in
                        (n, n + "-cloud", n + "-nexus", n + "-api") for n in needles)
                    found.append((canonical, repo.stat().st_mtime, repo))
                except OSError:
                    continue
        except OSError:
            continue
    selected = sorted(found, reverse=True)[:min(20, limit)]
    if not selected:
        return []
    workers = min(3, len(selected))
    rounds = (len(selected) + workers - 1) // workers
    share = max(0.0, deadline - time.monotonic()) / rounds

    def inspect(item):
        return _repo_snapshot(item[2], min(deadline, time.monotonic() + share))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(inspect, selected))
