#!/usr/bin/env python3
"""Fail-closed public-source scanner and exact distribution inventory.

The guard never prints matched source text or secret-like values. Findings
contain only a relative path, line number, rule, and SHA-256 fingerprints.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Iterable
from urllib.parse import urlsplit


INVENTORY_SCHEMA = "borg-public-inventory/v1"
ALLOWLIST_SCHEMA = "borg-release-guard-allowlist/v1"

FORBIDDEN_DIRS = {
    ".beads",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".secrets",
    ".venv",
    "__pycache__",
    "corpora",
    "corpus",
    "private",
    "receipts",
    "tokens",
    "venv",
}
FORBIDDEN_NAMES = {
    ".ds_store",
    ".env",
    "accounts.json",
    "client-config.json",
    "credentials.json",
    "fleet.json",
    "hosts.json",
    "lane-report.md",
    "owner-state.json",
    "prepare_computer_backend.py",
    "report.md",
}
FORBIDDEN_SUFFIXES = {".db", ".key", ".p12", ".pem", ".pyc", ".sqlite", ".token"}

APPROVED_NETWORK_HOSTS = {
    # Official documentation links in the configurable component catalog.
    "developers.figma.com",
    "developers.google.com",
    "developers.notion.com",
    "docs.frappe.io",
    "docs.n8n.io",
    "docs.railway.com",
    "docs.sentry.io",
    "docs.slack.dev",
    "docs.twenty.com",
    "grafana.com",
    "help.penpot.app",
    "labelstud.io",
    "mlflow.org",
    "vercel.com",
    "www.blender.org",
    "www.metabase.com",
    "www.postgresql.org",
    "127.0.0.1",
    "apple.com",
    "cloudflare.com",
    "borg.utlyze.com",
    "brandfolder.com",
    "cdn.playwright.dev",
    "code.claude.com",
    "claude.com",
    "developers.cloudflare.com",
    "developers.openai.com",
    "example.com",
    "example.net",
    "example.org",
    "falkordb.com",
    "files.pythonhosted.org",
    "github.com",
    "githubusercontent.com",
    "huggingface.co",
    "h3ro-dev.github.io",
    "help.openai.com",
    "googleapis.com",
    "grok.com",
    "localhost",
    "modelcontextprotocol.io",
    "microsoft.com",
    "nodejs.org",
    "npmjs.org",
    "ollama.com",
    "openai.com",
    "peekaboo.sh",
    "pypi.org",
    "python.org",
    "qdrant.tech",
    "raw.githubusercontent.com",
    "scripts.sil.org",
    "threejs.org",
    "www.w3.org",
    "x.ai",
    "registry.npmjs.org",
    "registry.ollama.ai",
}

# Reviewed OFL fonts and original Blender artwork (see art/README.md).
# Exact pins permit only these public assets, never arbitrary binary payloads.
PUBLIC_BINARY_ASSETS = {
    "site/assets/chakra-petch-regular.ttf": (78488, "98fcd638baa5c81ff0316b7538ce330ee3b23b1302726de3526d5933a8ecf986"),
    "site/assets/chakra-petch-bold.ttf": (78384, "65fbf76d95651697275e19db4d717c0e95a789ddd3476478b05292104db278a0"),
    "site/assets/borg-ship.glb": (1709776, "05fd62b658ba07593d9754c530f7229be55f9646acdac706bee21e13969aa2c7"),
    "site/assets/borg-ship-poster.webp": (175862, "1f148822cef00792c725464c091f1ad2c4a0fe831f1e911d4f847252cd30dc70"),
    "site/assets/borg-fleet.webp": (234470, "f7d0fd992b3fa0eb8f5d7e30cc89d2644f38ae5f7e7a68fb1517686641351cca"),
    "site/assets/borg-fleet-foreground.webp": (244494, "c39ec8039abe79f65dcd8e9fe28f9d4ea31608562e2218b7ca99819d77760e40"),
    "site/assets/borg-drone.webp": (571410, "132d9694ade32027439c2e9e9e9b2fbdb3475f0b8d750cf00656a8653beab278"),
    "site/assets/borg-drone-codex.webp": (578482, "c5d37a7935b099e43ffc48c6ed389f0f2ef2b9474946027e1050269844b0d654"),
}
APPROVED_NETWORK_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".invalid",
    ".localhost",
    ".test",
)
PLACEHOLDER_WORDS = (
    "dummy",
    "example",
    "fake",
    "fixture",
    "not-a-secret",
    "placeholder",
    "redacted",
    "synthetic",
    "test",
    "xxxxx",
)

URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s<>\"')]+")
EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])([\w.+-]+)@([a-z0-9.-]+\.[a-z]{2,})(?![\w.-])")
PRIVATE_USER_PATH_RE = re.compile(r"(?<![\w.-])/Users/([A-Za-z0-9._-]+)(?:/|\b)")
TAILNET_RE = re.compile(r"(?i)\b[a-z0-9.-]+\.ts\.net\b")
SESSION_ID_RE = re.compile(r"\bsession-[0-9a-f]{24,}\b")
ESTATE_MACHINE_RE = re.compile(
    r"(?i)\b(?:[a-z][a-z0-9-]*-studio(?:-?[0-9]+)?|studio[0-9]+(?:-[a-z0-9-]+)?|"
    r"[a-z][a-z0-9-]*-macbook(?:-pro)?(?:-[0-9]+)?)\b"
)
LEGACY_HOME_RE = re.compile(
    r"(?:~|\$\{?HOME\}?)/Library/Memory|Path\.home\(\)\s*/\s*[\"']Library/Memory"
)
IMPLICIT_DATA_RE = re.compile(
    r"(?:\.claude/projects|\.codex/sessions|Library/Memory/clients|Documents/Codex/Projects)"
)
POLICY_SOURCE_RE = re.compile(r"(?:\.codex/AGENTS\.md|\.claude/CLAUDE\.md)")
PROFILE_SOURCE_RE = re.compile(r"(?:\.codex-profiles|\.claude-seats|\bPROFILES\s*=\s*\[)")
OWNER_CONTACT_RE = re.compile(
    r"(?i)\b(?:JAMES|OWNER_(?:EMAIL|PHONE)|CONTACT_(?:EMAIL|PHONE))\s*=\s*[\"'][^\"']{3,}[\"']"
)
OWNER_RUNTIME_RE = re.compile(r"\bJames(?:'s|\u2019s)?\b")

SECRET_PATTERNS = {
    "private-key-material": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    "github-token": re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}"),
    "openai-token": re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    "slack-token": re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}"),
    "jwt-token": re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}"),
}
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?[_-]?token|bearer|password|secret)"
    r"\s*[:=]\s*[\"']([^\"']{8,})[\"']"
)

NON_WAIVABLE_RULES = {
    "aws-access-key",
    "declared-release-file-missing",
    "forbidden-release-artifact",
    "github-token",
    "implicit-user-data-source",
    "implicit-user-policy-source",
    "legacy-estate-home-default",
    "openai-token",
    "owner-contact-default",
    "private-key-material",
    "private-network-address",
    "private-tailnet-host",
    "private-user-path",
    "profile-inventory-default",
    "secret-literal",
    "slack-token",
    "symlink",
    "unexpected-binary",
    "unreadable-file",
    "weight-integrity-mismatch",
}


class GuardConfigurationError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Finding:
    severity: str
    rule: str
    path: str
    line: int
    line_sha256: str
    match_sha256: str
    message: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_test_path(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return "tests" in parts or PurePosixPath(relative).name.startswith("test_")


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(word in lowered for word in PLACEHOLDER_WORDS)


def _host_is_approved(host: str) -> bool:
    host = host.lower().rstrip(".")
    return (
        host in APPROVED_NETWORK_HOSTS
        or any(host.endswith("." + allowed) for allowed in APPROVED_NETWORK_HOSTS if "." in allowed)
        or host.endswith(APPROVED_NETWORK_SUFFIXES)
    )


def _finding(
    rule: str,
    path: str,
    line_number: int,
    line: str,
    matched: str,
    message: str,
    severity: str = "critical",
) -> Finding:
    return Finding(
        severity=severity,
        rule=rule,
        path=path,
        line=line_number,
        line_sha256=_sha256(line.encode("utf-8", errors="surrogatepass")),
        match_sha256=_sha256(matched.encode("utf-8", errors="surrogatepass")),
        message=message,
    )


def _path_finding(rule: str, relative: str, message: str, severity: str = "critical") -> Finding:
    return Finding(
        severity=severity,
        rule=rule,
        path=relative,
        line=0,
        line_sha256=_sha256(relative.encode()),
        match_sha256=_sha256(relative.encode()),
        message=message,
    )


def _load_weight_contract(root: Path) -> tuple[dict[str, tuple[int, str]], list[Finding]]:
    manifest_path = root / "adapters" / "MANIFEST.json"
    if not manifest_path.is_file():
        return {}, [_path_finding("declared-release-file-missing", "adapters/MANIFEST.json", "adapter manifest is missing")]
    try:
        # The same mandatory three-adapter contract protects installation.
        if __package__:
            from .adapter_contract import read_manifest
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from adapter_contract import read_manifest
        rows = read_manifest(root)
    except (OSError, ValueError, UnicodeError) as exc:
        raise GuardConfigurationError(f"invalid adapters/MANIFEST.json: {type(exc).__name__}") from exc
    expected: dict[str, tuple[int, str]] = {}
    findings: list[Finding] = []
    for adapter in rows:
        weights = adapter.get("weights") if isinstance(adapter, dict) else None
        if not isinstance(weights, dict) or not adapter.get("weights_present"):
            continue
        relative = weights.get("path")
        digest = weights.get("sha256")
        size = weights.get("bytes")
        if not isinstance(relative, str) or not re.fullmatch(r"adapters/[A-Za-z0-9._/-]+\.safetensors", relative):
            raise GuardConfigurationError("adapter weight path is not a safe relative .safetensors path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise GuardConfigurationError(f"invalid adapter weight SHA-256 for {relative}")
        if not isinstance(size, int) or size <= 0:
            raise GuardConfigurationError(f"invalid adapter weight byte count for {relative}")
        if relative in expected:
            raise GuardConfigurationError(f"duplicate adapter weight path: {relative}")
        expected[relative] = (size, digest)
        if not (root / relative).is_file():
            findings.append(_path_finding("declared-release-file-missing", relative, "manifest-declared release weight is missing"))
    return expected, findings


def _scan_text(relative: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    is_test = _is_test_path(relative)
    is_guard_source = relative == "installer/release_guard.py"
    is_memory_code = relative.startswith("memory/") and not is_test
    for line_number, line in enumerate(text.splitlines(), 1):
        for match in PRIVATE_USER_PATH_RE.finditer(line):
            if match.group(1).lower() not in {"owner", "user", "username", "you"}:
                findings.append(_finding("private-user-path", relative, line_number, line, match.group(0), "literal user home path must be parameterized"))
        for match in TAILNET_RE.finditer(line):
            findings.append(_finding("private-tailnet-host", relative, line_number, line, match.group(0), "tailnet hostname is not public distribution data"))
        if not is_test and not is_guard_source:
            for match in ESTATE_MACHINE_RE.finditer(line):
                findings.append(_finding("estate-machine-default", relative, line_number, line, match.group(0), "machine identity or estate routing default must be supplied by the new owner", severity="high"))
            for match in SESSION_ID_RE.finditer(line):
                findings.append(_finding("worker-session-reference", relative, line_number, line, match.group(0), "worker/session receipt reference is not source material", severity="high"))
            for match in LEGACY_HOME_RE.finditer(line):
                findings.append(_finding("legacy-estate-home-default", relative, line_number, line, match.group(0), "legacy estate home must be replaced by explicit BORG_HOME configuration"))
            for match in IMPLICIT_DATA_RE.finditer(line):
                findings.append(_finding("implicit-user-data-source", relative, line_number, line, match.group(0), "owner data source must be explicit and opt-in"))
            for match in POLICY_SOURCE_RE.finditer(line):
                findings.append(_finding("implicit-user-policy-source", relative, line_number, line, match.group(0), "owner policy must be generated or explicitly selected, not inherited"))
            for match in PROFILE_SOURCE_RE.finditer(line):
                findings.append(_finding("profile-inventory-default", relative, line_number, line, match.group(0), "provider profile inventory must come from new-owner configuration"))
            for match in OWNER_CONTACT_RE.finditer(line):
                findings.append(_finding("owner-contact-default", relative, line_number, line, match.group(0), "owner contact literal must not ship"))
            if Path(relative).suffix.lower() in {".py", ".js", ".mjs", ".sh"}:
                for match in OWNER_RUNTIME_RE.finditer(line):
                    findings.append(_finding("owner-bound-runtime-copy", relative, line_number, line, match.group(0), "runtime behavior or prompt is bound to the prior owner", severity="high"))
        for match in URL_RE.finditer(line):
            url = match.group(0).rstrip(".,;:")
            if any(marker in url for marker in ("${", "}", "<", ">")):
                continue
            try:
                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                host = ""
            if not host:
                continue
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and address.is_private and not address.is_loopback:
                if not is_test:
                    findings.append(_finding("private-network-address", relative, line_number, line, host, "private network address must be configured by the new owner"))
            elif address is None and not _host_is_approved(host):
                if is_test:
                    continue
                findings.append(_finding("unapproved-network-host", relative, line_number, line, host, "network host is outside the public-source host allowlist", severity="high"))
        for match in EMAIL_RE.finditer(line):
            value = match.group(0)
            domain = match.group(2).lower()
            if _host_is_approved(domain):
                continue
            if is_test and _is_placeholder(value):
                continue
            findings.append(_finding("unapproved-email", relative, line_number, line, value, "email literal requires exact public-provenance allowlisting or removal", severity="high"))
        for rule, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(line):
                value = match.group(0)
                if _is_placeholder(value):
                    continue
                findings.append(_finding(rule, relative, line_number, line, value, "credential material must not ship"))
        for match in SECRET_ASSIGNMENT_RE.finditer(line):
            value = match.group(1)
            if _is_placeholder(value) or is_test:
                continue
            findings.append(_finding("secret-literal", relative, line_number, line, value, "non-placeholder credential-like literal must not ship"))
        if is_memory_code and ("osascript" in line or '"Messages"' in line):
            findings.append(_finding("owner-notification-side-effect", relative, line_number, line, "owner-notification", "owner notification subprocess must be opt-in and configured", severity="high"))
        if is_memory_code and "codex" in line and ("subprocess.Popen" in line or '"exec"' in line):
            findings.append(_finding("embedded-worker-launch", relative, line_number, line, "embedded-worker", "worker subprocess selection must be supplied by the conductor configuration", severity="high"))
    return findings


def _load_allowlist(path: Path | None) -> dict[tuple[str, str, int, str], str]:
    if path is None:
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardConfigurationError(f"invalid allowlist: {type(exc).__name__}") from exc
    if document.get("schema") != ALLOWLIST_SCHEMA or not isinstance(document.get("entries"), list):
        raise GuardConfigurationError(f"allowlist schema must be {ALLOWLIST_SCHEMA}")
    result: dict[tuple[str, str, int, str], str] = {}
    for entry in document["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"rule", "path", "line", "line_sha256", "reason"}:
            raise GuardConfigurationError("each allowlist entry must have exactly rule, path, line, line_sha256, reason")
        rule, relative, line, digest, reason = (entry[k] for k in ("rule", "path", "line", "line_sha256", "reason"))
        if rule in NON_WAIVABLE_RULES:
            raise GuardConfigurationError(f"rule cannot be allowlisted: {rule}")
        if not isinstance(relative, str) or relative.startswith("/") or ".." in PurePosixPath(relative).parts:
            raise GuardConfigurationError("allowlist path must be a normalized relative path")
        if not isinstance(line, int) or line < 1:
            raise GuardConfigurationError("allowlist line must be a positive integer")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise GuardConfigurationError("allowlist line_sha256 must be lowercase SHA-256")
        if not isinstance(reason, str) or not reason.strip():
            raise GuardConfigurationError("allowlist reason is required")
        key = (rule, relative, line, digest)
        if key in result:
            raise GuardConfigurationError("duplicate allowlist entry")
        result[key] = reason.strip()
    return result


def _apply_allowlist(findings: Iterable[Finding], allowlist: dict[tuple[str, str, int, str], str]) -> list[Finding]:
    kept: list[Finding] = []
    used: set[tuple[str, str, int, str]] = set()
    for finding in findings:
        key = (finding.rule, finding.path, finding.line, finding.line_sha256)
        if finding.rule not in NON_WAIVABLE_RULES and key in allowlist:
            used.add(key)
        else:
            kept.append(finding)
    for rule, relative, line, digest in sorted(set(allowlist) - used):
        kept.append(Finding("high", "allowlist-entry-unused", relative, line, digest, digest, f"stale or unmatched allowlist entry for {rule}"))
    return sorted(set(kept))


def _inside(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def scan(root: Path, *, skip: set[str] | None = None, allowlist_path: Path | None = None) -> tuple[list[dict[str, object]], list[Finding]]:
    root = root.resolve()
    if not root.is_dir():
        raise GuardConfigurationError("scan root must be a directory")
    skip = skip or set()
    expected_weights, findings = _load_weight_contract(root)
    inventory: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: _relative(item, root)):
        relative = _relative(path, root)
        if relative in skip:
            continue
        try:
            mode = path.lstat().st_mode
        except OSError:
            findings.append(_path_finding("unreadable-file", relative, "release entry cannot be inspected"))
            continue
        if stat.S_ISLNK(mode):
            findings.append(_path_finding("symlink", relative, "symlinks are not accepted in the public source inventory"))
            continue
        if stat.S_ISDIR(mode):
            if any(part.lower() in FORBIDDEN_DIRS for part in PurePosixPath(relative).parts):
                findings.append(_path_finding("forbidden-release-artifact", relative, "private, generated, or state directory must not ship"))
            continue
        if not stat.S_ISREG(mode):
            findings.append(_path_finding("forbidden-release-artifact", relative, "non-regular release entry must not ship"))
            continue
        lowered_parts = {part.lower() for part in PurePosixPath(relative).parts}
        suffix = path.suffix.lower()
        if (
            lowered_parts & FORBIDDEN_DIRS
            or path.name.lower() in FORBIDDEN_NAMES
            or suffix in FORBIDDEN_SUFFIXES
            or (suffix == ".jsonl" and not _is_test_path(relative))
        ):
            findings.append(_path_finding("forbidden-release-artifact", relative, "private data, state, receipt, credential, or generated artifact must not ship"))
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(_path_finding("unreadable-file", relative, "release file cannot be read"))
            continue
        digest = _sha256(data)
        inventory.append({"path": relative, "bytes": len(data), "sha256": digest})
        if relative in PUBLIC_BINARY_ASSETS:
            if (len(data), digest) != PUBLIC_BINARY_ASSETS[relative]:
                findings.append(_path_finding("public-asset-integrity-mismatch", relative, "website asset does not match its reviewed public source"))
            continue
        if relative in expected_weights:
            expected_size, expected_digest = expected_weights[relative]
            if len(data) != expected_size or digest != expected_digest:
                findings.append(_path_finding("weight-integrity-mismatch", relative, "adapter weight does not match the public manifest"))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(_path_finding("unexpected-binary", relative, "binary file is not a manifest-pinned adapter weight"))
            continue
        if "\x00" in text:
            findings.append(_path_finding("unexpected-binary", relative, "NUL-bearing file is not a manifest-pinned adapter weight"))
            continue
        findings.extend(_scan_text(relative, text))
    return inventory, _apply_allowlist(findings, _load_allowlist(allowlist_path))


def _inventory_digest(files: list[dict[str, object]]) -> str:
    return _sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())


def _inventory_document(files: list[dict[str, object]], excluded_self: str | None = None) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": INVENTORY_SCHEMA,
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "inventory_sha256": _inventory_digest(files),
    }
    if excluded_self is not None:
        document["excluded_self"] = excluded_self
    return document


def _load_inventory(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardConfigurationError(f"invalid inventory: {type(exc).__name__}") from exc
    if document.get("schema") != INVENTORY_SCHEMA or not isinstance(document.get("files"), list):
        raise GuardConfigurationError(f"inventory schema must be {INVENTORY_SCHEMA}")
    files = document["files"]
    if document.get("file_count") != len(files) or document.get("inventory_sha256") != _inventory_digest(files):
        raise GuardConfigurationError("inventory summary or digest is inconsistent")
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise GuardConfigurationError("inventory file entries must have exactly path, bytes, sha256")
    return document


def _compare_inventory(actual: list[dict[str, object]], expected: dict[str, object]) -> list[Finding]:
    actual_map = {str(item["path"]): item for item in actual}
    expected_map = {str(item["path"]): item for item in expected["files"]}
    findings: list[Finding] = []
    for relative in sorted(expected_map.keys() - actual_map.keys()):
        findings.append(_path_finding("inventory-file-missing", relative, "file listed in the approved inventory is missing"))
    for relative in sorted(actual_map.keys() - expected_map.keys()):
        findings.append(_path_finding("inventory-file-unexpected", relative, "file is not listed in the approved inventory"))
    for relative in sorted(actual_map.keys() & expected_map.keys()):
        if actual_map[relative] != expected_map[relative]:
            findings.append(_path_finding("inventory-file-changed", relative, "file bytes or SHA-256 differ from the approved inventory"))
    return findings


def _atomic_write_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="public source root")
    parser.add_argument("--allowlist", type=Path, help="exact line-hash allowlist")
    parser.add_argument("--inventory", type=Path, help="verify an approved exact file inventory")
    parser.add_argument("--write-inventory", type=Path, help="write an exact inventory only after a clean scan")
    parser.add_argument("--json", action="store_true", help="emit the full machine-readable scan result")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.inventory and args.write_inventory:
        print("release_guard: --inventory and --write-inventory are mutually exclusive", file=sys.stderr)
        return 2
    root = args.root.resolve()
    skip: set[str] = set()
    inventory_document = None
    inventory_self = None
    try:
        if args.inventory:
            inventory_document = _load_inventory(args.inventory)
            inventory_self = _inside(args.inventory, root)
            if inventory_self:
                if inventory_document.get("excluded_self") != inventory_self:
                    raise GuardConfigurationError("in-tree inventory must name its own excluded_self path")
                skip.add(inventory_self)
        if args.write_inventory:
            inventory_self = _inside(args.write_inventory, root)
            if inventory_self:
                skip.add(inventory_self)
        files, findings = scan(root, skip=skip, allowlist_path=args.allowlist)
        if inventory_document is not None:
            findings = sorted(set([*findings, *_compare_inventory(files, inventory_document)]))
        result = _inventory_document(files, inventory_self)
        result["status"] = "pass" if not findings else "fail"
        result["finding_count"] = len(findings)
        result["findings"] = [asdict(item) for item in findings]
        if args.write_inventory and not findings:
            _atomic_write_json(args.write_inventory.resolve(), _inventory_document(files, inventory_self))
    except GuardConfigurationError as exc:
        print(f"release_guard: configuration error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif findings:
        print(f"release_guard: FAIL findings={len(findings)} files={len(files)}")
        for finding in findings:
            location = f"{finding.path}:{finding.line}" if finding.line else finding.path
            print(f"[{finding.severity}] {finding.rule} {location} line_sha256={finding.line_sha256} - {finding.message}")
    else:
        print(
            "release_guard: PASS "
            f"files={len(files)} bytes={result['total_bytes']} inventory_sha256={result['inventory_sha256']}"
        )
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
