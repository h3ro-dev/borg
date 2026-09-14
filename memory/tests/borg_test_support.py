"""Generic isolated BORG_HOME fixture for modules imported during discovery."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


_TEMP = None


def activate() -> Path:
    global _TEMP
    if os.environ.get("BORG_HOME"):
        return Path(os.environ["BORG_HOME"])
    _TEMP = tempfile.TemporaryDirectory(prefix="borg-memory-tests-")
    home = Path(_TEMP.name)
    owner = "example-owner"
    config = {
        "BORG_OWNER_ID": owner,
        "BORG_MEMORY_SCOPE": f"personal:{owner}",
        "BORG_QDRANT_URL": "http://127.0.0.1:16333",
        "BORG_QDRANT_COLLECTION": "memory_example",
        "BORG_HISTORY_DB": "history/history.db",
        "BORG_OLLAMA_URL": "http://127.0.0.1:11434",
        "BORG_EXTRACTION_MODEL": "example-extractor:latest",
        "BORG_EXTRACTION_MODEL_ID": "ollama:sha256:" + "a" * 64,
        "BORG_EMBED_MODEL": "example-embed:latest",
        "BORG_EMBED_MODEL_ID": "ollama:sha256:" + "b" * 64,
        "BORG_EMBED_DIMS": "768",
        "BORG_FALKORDB_HOST": "127.0.0.1",
        "BORG_FALKORDB_PORT": "16383",
        "BORG_FALKORDB_GRAPH": "graph_example",
        "BORG_GRAPH_LLM_URL": "http://127.0.0.1:11500/v1",
        "BORG_GRAPH_MODEL": "example-graph:latest",
    }
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    os.environ["BORG_HOME"] = str(home)
    return home
