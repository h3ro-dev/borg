"""Portable-home configuration proofs; no network, stores, or live paths."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "bin" / "borg_config.py"


def document(owner: str = "alice") -> dict[str, str]:
    return {
        "BORG_OWNER_ID": owner,
        "BORG_MEMORY_SCOPE": f"personal:{owner}",
        "BORG_QDRANT_URL": "http://127.0.0.1:16333",
        "BORG_QDRANT_COLLECTION": f"mem_{owner}",
        "BORG_HISTORY_DB": "history/history.db",
        "BORG_OLLAMA_URL": "http://127.0.0.1:11434",
        "BORG_EXTRACTION_MODEL": "qwen3:4b",
        "BORG_EXTRACTION_MODEL_ID": "ollama:sha256:" + "a" * 64,
        "BORG_EMBED_MODEL": "nomic-embed-text:latest",
        "BORG_EMBED_MODEL_ID": "ollama:sha256:" + "b" * 64,
        "BORG_EMBED_DIMS": "768",
        "BORG_FALKORDB_HOST": "127.0.0.1",
        "BORG_FALKORDB_PORT": "16383",
        "BORG_FALKORDB_GRAPH": f"graph_{owner}",
        "BORG_GRAPH_LLM_URL": "http://127.0.0.1:11500/v1",
        "BORG_GRAPH_MODEL": "qwen3:4b",
    }


def run(home: Path, code: str):
    env = os.environ.copy()
    env["BORG_HOME"] = str(home)
    env.pop("BORG_HISTORY_DB", None)
    env["PYTHONPATH"] = str(ROOT / "bin")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          text=True, capture_output=True)


def run_without_home(code: str):
    env = os.environ.copy()
    env.pop("BORG_HOME", None)
    env["PYTHONPATH"] = str(ROOT / "bin")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          text=True, capture_output=True)


class BorgConfigTests(unittest.TestCase):
    def test_unset_home_fails_closed_without_a_legacy_owner_fallback(self):
        result = run_without_home("import borg_config")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BORG_HOME is required", result.stderr)

    def test_two_homes_are_disjoint(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a", Path(td) / "b"
            for home, owner in ((a, "alice"), (b, "bob")):
                home.mkdir()
                (home / "config.json").write_text(json.dumps(document(owner)))
            code = (
                "import borg_config; c=borg_config.CONFIG; "
                "print(c.data_root, c.graph_data_root, c.values['BORG_HISTORY_DB'], "
                "c.values['BORG_QDRANT_COLLECTION'], c.values['BORG_MEMORY_SCOPE'])"
            )
            out_a, out_b = run(a, code), run(b, code)
            self.assertEqual(out_a.returncode, 0, out_a.stderr)
            self.assertEqual(out_b.returncode, 0, out_b.stderr)
            self.assertNotEqual(out_a.stdout, out_b.stdout)
            self.assertIn(str(a / "mem0"), out_a.stdout)
            self.assertIn(str(b / "graphiti"), out_b.stdout)

    def test_missing_and_malformed_config_fail_clearly(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "missing"
            home.mkdir()
            result = run(home, "import borg_config")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("config is missing", result.stderr)
            (home / "config.json").write_text("[]")
            result = run(home, "import borg_config")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("JSON object", result.stderr)

    def test_model_ids_are_required_and_provenance_is_digest(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cfg = document()
            cfg.pop("BORG_EXTRACTION_MODEL_ID")
            (home / "config.json").write_text(json.dumps(cfg))
            result = run(home, "import borg_config")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("BORG_EXTRACTION_MODEL_ID", result.stderr)

    def test_scope_taxonomy_is_owner_scoped_and_default_closed(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.json").write_text(json.dumps(document("sara")))
            code = (
                "import mem0_scope_lib as m; "
                "assert m.SCOPE_PERSONAL=='personal:sara'; "
                "assert m.to_scope('unknown','',False,{},'filler')[0]=='personal:sara'; print('ok')"
            )
            result = run(home, code)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ok")

    def test_native_client_helper_exports_remain_available(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.json").write_text(json.dumps(document()))
            helper = ROOT / "bin" / "mem0-fleet-configure"
            code = (
                "import importlib.machinery; "
                f"m=importlib.machinery.SourceFileLoader('fleet_cfg', {str(helper)!r}).load_module(); "
                "assert all(callable(getattr(m,n,None)) for n in "
                "('NativeRPC','raw_user_layer','hooks_data')); print('ok')"
            )
            result = run(home, code)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ok")

    def test_native_configurer_refuses_without_borg_home(self):
        helper = ROOT / "bin" / "mem0-fleet-configure"
        result = run_without_home(
            "import importlib.machinery; "
            f"m=importlib.machinery.SourceFileLoader('fleet_cfg_no_home', {str(helper)!r}).load_module(); "
            "m.main(['--machine','example-machine','--check'])"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BORG_HOME is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
