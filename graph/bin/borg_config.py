"""Graph entrypoint bridge to the single memory runtime configuration.

Installer exports BORG_* and copies the same memory/bin beside graphiti.
Source checkouts use memory/bin; neither route consults an owner's live tree.
"""
from pathlib import Path
import importlib.machinery
import os


if not os.environ.get("BORG_HOME", "").strip():
    raise RuntimeError(
        "BORG_HOME is required; run the installer or supply an explicit new-owner configuration"
    )

_parent = Path(__file__).resolve().parents[2]
_memory = _parent / ('mem0' if (_parent / 'mem0/bin/borg_config.py').is_file() else 'memory')
_module = importlib.machinery.SourceFileLoader(
    'borg_config_graph_shared', str(_memory / 'bin/borg_config.py')
).load_module()
CONFIG = _module.CONFIG
BorgConfigError = _module.BorgConfigError
require = _module.require
ensure_runtime_dirs = _module.ensure_runtime_dirs
