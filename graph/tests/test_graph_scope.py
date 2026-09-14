import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"


def portable_config(owner="sara"):
    return {
        "BORG_OWNER_ID": owner,
        "BORG_MEMORY_SCOPE": f"personal:{owner}",
        "BORG_QDRANT_URL": "http://127.0.0.1:6333",
        "BORG_QDRANT_COLLECTION": f"mem_{owner}",
        "BORG_HISTORY_DB": "mem0/data/history.db",
        "BORG_OLLAMA_URL": "http://127.0.0.1:11434",
        "BORG_EXTRACTION_MODEL": "qwen3:4b",
        "BORG_EXTRACTION_MODEL_ID": "ollama:sha256:" + "a" * 64,
        "BORG_EMBED_MODEL": "nomic-embed-text:latest",
        "BORG_EMBED_MODEL_ID": "ollama:sha256:" + "b" * 64,
        "BORG_EMBED_DIMS": 768,
        "BORG_FALKORDB_HOST": "127.0.0.1",
        "BORG_FALKORDB_PORT": 6383,
        "BORG_FALKORDB_GRAPH": f"graph_{owner}",
        "BORG_GRAPH_LLM_URL": "http://127.0.0.1:11500/v1",
        "BORG_GRAPH_MODEL": "qwen3:4b",
    }


class GraphScopeTests(unittest.TestCase):
    def test_portable_default_scope_and_registry_are_owner_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.json").write_text(json.dumps(portable_config()))
            env = dict(os.environ, BORG_HOME=str(home), PYTHONPATH=f"{ROOT}:{BIN}")
            code = (
                "import graph_scope as g; from pathlib import Path; "
                "assert g.DEFAULT_SCOPE == 'personal:sara'; "
                "r=g.ScopeRegistry(Path(__import__('os').environ['BORG_HOME'])/'graphiti'/'data'/'scope-graphs.json'); "
                "k=r.ensure_scope(g.DEFAULT_SCOPE); assert k.startswith('memscope_'); "
                "assert r.get('personal:sara') == k; print(k)"
            )
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip().startswith("memscope_"))

    def test_no_home_fails_closed_without_an_owner_configuration(self):
        env = dict(os.environ)
        env.pop("BORG_HOME", None)
        env["PYTHONPATH"] = f"{ROOT}:{BIN}"
        result = subprocess.run(
            [sys.executable, "-c", "import graph_scope"],
            env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BORG_HOME is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
