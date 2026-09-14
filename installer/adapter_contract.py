"""The required public adapter inventory, shared by setup and publication checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

REQUIRED_ADAPTERS = frozenset({
    'graphiti-extraction-qwen3-1.7b',
    'graphiti-extraction-qwen3-4b',
    'capture-extraction-qwen3-4b',
})


def read_manifest(root: Path) -> list[dict]:
    path = root / 'adapters/MANIFEST.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('The release adapter manifest is missing or redirected')
    manifest = json.loads(path.read_text())
    rows = manifest.get('adapters')
    if (not isinstance(manifest.get('distribution'), dict)
            or manifest['distribution'].get('weights_included') is not True
            or manifest.get('schema') != 'borg-adapters/v2' or not isinstance(rows, list)
            or len(rows) != len(REQUIRED_ADAPTERS) or any(not isinstance(row, dict) for row in rows)
            or {row.get('name') for row in rows} != REQUIRED_ADAPTERS):
        raise ValueError('This release requires all three named BORG adapters')
    for row in rows:
        weight = row.get('weights', {})
        if (row.get('weights_present') is not True or row.get('active') is not False
                or not isinstance(weight, dict)
                or weight.get('path') != f"adapters/{row['name']}/adapters.safetensors"
                or type(weight.get('bytes')) is not int or weight['bytes'] <= 0
                or not isinstance(weight.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', weight['sha256'])):
            raise ValueError('Adapter weights must be included, inactive, and pinned: ' + row['name'])
    return rows


def verify_release(root: Path) -> None:
    for row in read_manifest(root):
        spec = row['weights']
        path = root / spec['path']
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve() / 'adapters'):
            raise ValueError('Required adapter weight is missing or redirected: ' + row['name'])
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if path.stat().st_size != spec['bytes'] or digest.hexdigest() != spec['sha256']:
            raise ValueError('Required adapter weight failed its integrity check: ' + row['name'])
