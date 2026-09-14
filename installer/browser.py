"""Verify the pinned Chromium archive and installed tree before execution."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import urllib.request
import zipfile

from installer.config import managed_directory
from installer.downloads import platform_key, runtime_tree, sha256

LOCK_PATH = Path(__file__).with_name('browser-lock.json')


def specification() -> tuple[str, dict, dict]:
    lock = json.loads(LOCK_PATH.read_text())
    key = '-'.join(platform_key())
    if lock.get('schema') != 'borg-browser/v1' or lock.get('playwright') != '1.62.0':
        raise ValueError('Chromium lock does not match the pinned Playwright runtime')
    spec = lock['platforms'][key]
    if (not spec['url'].startswith('https://cdn.playwright.dev/builds/')
            or type(spec['bytes']) is not int or spec['bytes'] <= 0
            or len(spec['sha256']) != 64 or any(c not in '0123456789abcdef' for c in spec['sha256'])):
        raise ValueError('Chromium artifact requires an official URL, byte count and SHA-256')
    return key, lock, spec


def archive_path(root: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('Unsafe Chromium archive path')
    target = root.joinpath(*path.parts)
    if target == root or not target.resolve().is_relative_to(root):
        raise ValueError('Chromium archive path leaves its tree')
    return target


def extract(archive: Path, destination: Path) -> None:
    links = []
    seen = set()
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            target = archive_path(destination, info.filename)
            if target in seen:
                raise ValueError('Duplicate Chromium archive path')
            seen.add(target)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if info.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif kind == stat.S_IFLNK:
                links.append((target, package.read(info).decode()))
            elif kind in {0, stat.S_IFREG}:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with package.open(info) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600 | (mode & 0o111))
            else:
                raise ValueError('Unsupported Chromium archive entry')
        # Framework links are created after files so extraction never writes
        # through a link supplied by an archive.
        for target, value in links:
            if os.path.isabs(value) or not (target.parent / value).resolve().is_relative_to(destination):
                raise ValueError('Chromium framework link leaves its tree')
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.symlink_to(value)
    runtime_tree(destination)  # Resolve the complete link graph, including chains.


def prepare(doc: dict) -> Path:
    home = Path(doc['home'])
    key, lock, spec = specification()
    cache = managed_directory(home / 'cache/downloads')
    browsers = managed_directory(home / 'runtime/browsers')
    dest = managed_directory(browsers / ('chromium-' + lock['revision'] + '-' + key))
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    browsers.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = cache / ('chromium-' + lock['revision'] + '-' + key + '.zip')
    if archive.exists() or archive.is_symlink():
        info = archive.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('Chromium cache must be an owned regular file')
    else:
        fd, name = tempfile.mkstemp(prefix='.chromium-download-', dir=cache)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as output, urllib.request.urlopen(spec['url'], timeout=60) as response:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > spec['bytes']:
                        raise ValueError('Chromium archive exceeds its pinned byte count')
                    output.write(chunk)
            if temporary.stat().st_size != spec['bytes'] or sha256(temporary) != spec['sha256']:
                raise ValueError('Chromium archive failed its byte count or SHA-256 check')
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
    if archive.stat().st_size != spec['bytes'] or sha256(archive) != spec['sha256']:
        raise ValueError('Chromium cache failed its byte count or SHA-256 check')
    staging = Path(tempfile.mkdtemp(prefix='.chromium-extract-', dir=browsers))
    try:
        extract(archive, staging)
        candidate = archive_path(staging, spec['executable'])
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError('Pinned Chromium executable is missing')
        marker = '.borg-artifact-sha256'
        if dest.exists():
            receipt = dest / marker
            if (receipt.is_symlink() or not receipt.is_file() or receipt.read_text().strip() != spec['sha256']
                    or runtime_tree(dest) != runtime_tree(staging)):
                raise ValueError('Installed Chromium changed; preserve it before repair')
        else:
            (staging / marker).write_text(spec['sha256'] + '\n')
            os.replace(staging, dest)
        return dest / spec['executable']
    finally:
        if staging.exists():
            shutil.rmtree(staging)
