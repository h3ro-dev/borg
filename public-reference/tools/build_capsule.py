#!/usr/bin/env python3
"""Export reviewed architecture files only; never export a runtime or Git tree."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile

MAX_FILE_BYTES = 512_000
MAX_TOTAL_BYTES = 4_000_000
SAFE_SUFFIXES = {".md", ".json", ".py", ".txt", ".toml"}
FORBIDDEN_PARTS = {".git", ".env", "data", "logs", "credentials", "tokens", "secrets", "node_modules", "__pycache__"}
RULES = {
    "provider-credential": re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xai-[A-Za-z0-9_-]{24,})\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "personal-home": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "runtime-account-id": re.compile(r"\b(?:session-[a-f0-9]{24,}|asdk_app_[a-f0-9]{20,})\b"),
    "jwt-shape": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
}
EMAIL = re.compile(r"\b[A-Za-z0-9_.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
SYNTHETIC_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "example.invalid", "example.test"}

class ReleaseError(ValueError):
    """Safe diagnostic containing a path/rule, not the matched payload."""


def safe_read(root: Path, name: str) -> bytes:
    rel = PurePosixPath(name)
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or rel.is_absolute() or rel.as_posix() != name:
        raise ReleaseError("noncanonical release path")
    if any(p in {"", ".", ".."} or p.lower() in FORBIDDEN_PARTS for p in rel.parts):
        raise ReleaseError("forbidden release path")
    if rel.name != "LICENSE" and rel.suffix not in SAFE_SUFFIXES:
        raise ReleaseError("artifact type excluded")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ReleaseError("symlink excluded")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ReleaseError("nonregular or hardlinked file excluded")
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ReleaseError("listed file unavailable") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ReleaseError("file size limit exceeded")
    return raw


def inspect_text(name: str, raw: bytes, deny_terms: tuple[str, ...]) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseError("non-UTF8 content excluded") from exc
    if "\x00" in text:
        raise ReleaseError("binary content excluded")
    for label, regex in RULES.items():
        if regex.search(text):
            raise ReleaseError(f"{name}: blocked by {label}")
    for match in EMAIL.finditer(text):
        if match.group(1).lower() not in SYNTHETIC_EMAIL_DOMAINS:
            raise ReleaseError(f"{name}: non-synthetic email")
    folded = text.casefold()
    for index, term in enumerate(deny_terms):
        if term.casefold() in folded:
            raise ReleaseError(f"{name}: blocked by private-term-{index + 1}")


def collect(root: Path, deny_terms: tuple[str, ...] = ()) -> dict[str, bytes]:
    root = root.absolute()
    if root.resolve() != root:
        raise ReleaseError("release root must not contain symlinks")
    manifest_bytes = safe_read(root, "release.json")
    try:
        manifest = json.loads(manifest_bytes)
    except (ValueError, UnicodeError) as exc:
        raise ReleaseError("invalid release manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("kind") != "architecture-reference-only":
        raise ReleaseError("unsupported release boundary")
    names = manifest.get("files")
    if (not isinstance(names, list) or not 1 <= len(names) <= 100
            or any(not isinstance(n, str) for n in names)
            or len(set(names)) != len(names) or "release.json" not in names):
        raise ReleaseError("invalid explicit file allowlist")
    payload: dict[str, bytes] = {}
    for name in sorted(names):
        raw = manifest_bytes if name == "release.json" else safe_read(root, name)
        inspect_text(name, raw, deny_terms)
        payload[name] = raw
    if sum(map(len, payload.values())) > MAX_TOTAL_BYTES:
        raise ReleaseError("total size limit exceeded")
    return payload


def build(root: Path, output: Path, deny_terms: tuple[str, ...] = ()) -> dict:
    payload = collect(root, deny_terms)
    checksums = {name: hashlib.sha256(raw).hexdigest() for name, raw in payload.items()}
    record = {"kind": "architecture-reference-only", "files": checksums,
              "excluded": "Git history, runtime data, credentials, models and all unlisted files",
              "limit": "Pattern scan is not general privacy or legal clearance."}
    payload["CHECKSUMS.json"] = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
    output = output.absolute()
    if output.is_symlink() or output.exists():
        raise ReleaseError("refusing to replace an existing output")
    if output.parent.resolve() != output.parent:
        raise ReleaseError("output directory must not contain symlinks")
    fd, temp = tempfile.mkstemp(prefix="borg-capsule-", suffix=".zip", dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for name, raw in sorted(payload.items()):
                info = zipfile.ZipInfo("borg-architecture/" + name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                z.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        # Exclusive creation prevents replacing a concurrently created file.
        with open(temp, "rb") as src, open(output, "xb") as dst:
            while block := src.read(65536):
                dst.write(block)
    finally:
        Path(temp).unlink(missing_ok=True)
    record["archive_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    return record


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true")
    p.add_argument("--output", type=Path)
    p.add_argument("--deny-file", type=Path, help="private newline-separated terms; values are never printed")
    args = p.parse_args()
    if args.check == bool(args.output):
        p.error("select exactly one of --check or --output")
    root = Path(__file__).absolute().parent.parent
    try:
        terms = tuple(line.strip() for line in args.deny_file.read_text().splitlines() if line.strip()) if args.deny_file else ()
        if args.check:
            result = {"status": "PASS", "file_count": len(collect(root, terms)), "boundary": "allowlisted architecture only"}
        else:
            result = build(root, args.output, terms)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (OSError, ReleaseError) as exc:
        print("RELEASE_BLOCKED: " + (str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
