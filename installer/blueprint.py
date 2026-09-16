"""Strict, credential-free blueprint input and deterministic installation selection."""
from __future__ import annotations

import json
import math
from pathlib import Path
import platform
import re
import unicodedata

CATALOG = Path(__file__).resolve().parents[1] / "platform/catalog.json"
SCHEMA = "borg-blueprint/v1"
PLATFORMS = {"macos-arm64", "macos-x64", "linux-x64", "linux-arm64", "windows"}
FULL_SERVICES = ["qdrant", "graph", "ollama", "graph_llm", "memory", "brain", "connector", "watchdog"]
DEFAULT_COMPONENTS = ["codex", "inbox", "beads", "fleet"]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field: " + key)
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    with Path(path).open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Blueprint/catalog exceeds 1 MiB")
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Invalid blueprint/catalog JSON") from exc


def catalog() -> dict:
    data = read_json(CATALOG)
    if not isinstance(data, dict) or data.get("schema") != "borg-catalog/v1":
        raise ValueError("Expected a borg-catalog/v1 catalog")
    return data


def _fields(value, fields, where):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError(where + " must contain exactly: " + fields)


def validate(value: dict, registry: dict) -> dict:
    _fields(value, "schema catalog_version goal machines", "Blueprint")
    if value["schema"] != SCHEMA or value["catalog_version"] != registry["version"]:
        raise ValueError("Blueprint schema/catalog version does not match this release")
    if value["goal"] not in ["coding", "research", "automation", "learning", "custom"]:
        raise ValueError("Unknown blueprint goal")
    machines = value["machines"]
    if not isinstance(machines, list) or not 1 <= len(machines) <= 100:
        raise ValueError("Blueprint needs 1..100 machines")
    items = {row["id"]: row for row in registry["items"]}
    seen = set()
    for m in machines:
        _fields(m, "id label platform profile components integrations workload", "Machine")
        identity = m["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", identity) or identity in seen:
            raise ValueError("Machine IDs must be unique lowercase identifiers, 1..32 characters")
        seen.add(identity)
        label = m["label"]
        if (not isinstance(label, str) or not 1 <= len(label.encode("utf-16-le", errors="surrogatepass")) // 2 <= 60 or not label.strip()
                or any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in label)):
            raise ValueError(identity + ": label needs 1..60 plain characters")
        if not isinstance(m["platform"], str) or m["platform"] not in PLATFORMS or m["profile"] not in ["full", "tools"]:
            raise ValueError(identity + ": unknown platform or profile")
        chosen = []
        for field, kind in [("components", "component"), ("integrations", "integration")]:
            ids = m[field]
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                raise ValueError(identity + ": " + field + " must be a list without duplicates")
            for name in ids:
                row = items.get(name)
                if not row or row["type"] != kind:
                    raise ValueError(identity + ": unknown " + kind + " " + name)
                if (m["platform"] != "windows" and m["platform"] not in row["platforms"]) or m["profile"] not in row["profiles"]:
                    raise ValueError(identity + ": " + name + " is incompatible with platform/profile")
            chosen.extend(ids)
        for name in chosen:
            missing = set(items[name]["requires"]) - set(chosen)
            if missing:
                raise ValueError(identity + ": " + name + " requires " + ", ".join(sorted(missing)))
        w = m["workload"]
        _fields(w, "agents browsers builds memory_millions project_gb context_tokens training", identity + " workload")
        for name, upper in [("agents", 128), ("browsers", 32), ("builds", 32), ("project_gb", 100000)]:
            if type(w[name]) is not int or not 0 <= w[name] <= upper:
                raise ValueError(identity + ": " + name + " must be an integer in 0.." + str(upper))
        memory = w["memory_millions"]
        if type(memory) not in (int, float) or not 0 <= memory <= 100 or not math.isfinite(memory):
            raise ValueError(identity + ": memory_millions must be finite in 0..100")
        if type(w["context_tokens"]) is not int or w["context_tokens"] not in [8192, 16384, 32768]:
            raise ValueError(identity + ": context_tokens must be 8192, 16384 or 32768")
        if type(w["training"]) is not bool:
            raise ValueError(identity + ": training must be boolean")
        if w["training"] and (m["profile"] != "full" or m["platform"] != "macos-arm64" or "training" not in chosen):
            raise ValueError(identity + ": training workload requires training on full macos-arm64")
    return value


def load(path: Path, registry: dict | None = None) -> dict:
    return validate(read_json(path), registry if registry is not None else catalog())


def machine(value: dict, machine_id: str | None) -> dict:
    if machine_id is None:
        raise ValueError("--machine is required when installing a blueprint")
    for row in value["machines"]:
        if row["id"] == machine_id:
            return row
    raise ValueError("Unknown blueprint machine: " + machine_id)


def runtime_platform() -> str:
    system = {"Darwin": "macos", "Linux": "linux"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(platform.machine())
    return system + "-" + arch if system and arch else "unsupported"


def check_runtime(row: dict) -> None:
    if row["platform"] == "windows":
        raise ValueError("Windows is planning-only; native installation is unsupported")
    actual = runtime_platform()
    if row["platform"] != actual:
        raise ValueError("Blueprint platform " + row["platform"] + " does not match this host (" + actual + ")")


def selected_machine(doc: dict) -> dict | None:
    selection = doc.get("blueprint")
    return machine(selection["input"], selection["machine_id"]) if selection else None


def full(doc: dict) -> bool:
    row = selected_machine(doc)
    return row is None or row["profile"] == "full"


def selected(doc: dict, component: str) -> bool:
    row = selected_machine(doc)
    return row is None or component in row["components"]


def service_names(doc: dict) -> list[str]:
    names = list(FULL_SERVICES if full(doc) else ["connector", "watchdog"])
    names += [service for component, service in [("codex", "conductor"), ("inbox", "inbox")] if selected(doc, component)]
    if doc.get("external_access", {}).get("enabled"):
        names += ["gateway", "tunnel"]
    return names


def describe(row: dict, registry: dict) -> dict:
    stub = {"blueprint": {"input": {"machines": [row]}, "machine_id": row["id"]}}
    items = {item["id"]: item for item in registry["items"]}
    setup = [{"id": name, "status": items[name]["status"], "steps": items[name]["setup"],
              "limitations": items[name]["limitations"], "docs": items[name]["docs"],
              "readiness": "not_verified"} for name in row["components"] + row["integrations"]]
    warnings = ["Planning workload is not measured capacity or provider allowance; native admission still applies.",
                "Provider sign-in is an owner step; selections never import credentials or authenticate providers."]
    if row["profile"] == "tools":
        warnings.append("Tools profile skips memory/graph services, model pulls and capture hooks. The complete runtime/source package is retained.")
    if row["platform"] != "macos-arm64":
        warnings.append("Windows installation is unsupported." if row["platform"] == "windows" else "Native acceptance on Linux/Intel remains pending.")
    return {"id": row["id"], "label": row["label"], "profile": row["profile"], "platform": row["platform"],
            "services": service_names(stub), "setup": setup, "warnings": warnings}


def inspect(value: dict, registry: dict, machine_id: str | None = None) -> dict:
    validate(value, registry)
    rows = [machine(value, machine_id)] if machine_id else value["machines"]
    return {"schema": "borg-blueprint-inspection/v1", "valid": True,
            "machines": [describe(row, registry) for row in rows]}


def check_existing(home: Path, selection: dict | None) -> dict | None:
    """Read-only identity check before any bootstrap or owner-state write."""
    from installer import config
    root = config.absolute_root(home)
    if (root / "config.json").exists() or (root / "config.json").is_symlink():
        doc = config.load(root)
        if doc.get("blueprint") != selection:
            raise ValueError("Blueprint differs from this home; use a new home or the identical original blueprint and machine")
        if selection is not None and config.read_private(root / "blueprint.json") != selection["input"]:
            raise ValueError("Stored blueprint differs; preserve and reconcile this installation")
        return doc
    return None
