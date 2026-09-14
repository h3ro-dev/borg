from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PortableTrainingTests(unittest.TestCase):
    def run_python(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / script), *args],
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_dataset_builder_requires_explicit_source_and_output(self) -> None:
        result = self.run_python("build_dataset.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--source-dir", result.stderr)
        self.assertIn("--out", result.stderr)

    def test_dataset_builder_preserves_canonical_transform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input"
            output = root / "output"
            source.mkdir()
            record = {
                "input": "A synthetic system uses a synthetic queue.",
                "date": "2026-01-02",
                "rich": {
                    "entities": [
                        {"name": "Synthetic system", "type": "system", "summary": "Fixture"},
                        {"name": "Synthetic queue", "type": "queue", "summary": "Fixture"},
                    ],
                    "relations": [
                        {
                            "source": "Synthetic system",
                            "relation": "uses",
                            "target": "Synthetic queue",
                            "fact": "A synthetic system uses a synthetic queue.",
                            "date": "2026-01-02",
                        }
                    ],
                },
            }
            (source / "pairs.jsonl").write_text(json.dumps(record) + "\n")
            result = self.run_python(
                "build_dataset.py",
                "--source-dir",
                str(source),
                "--out",
                str(output),
                "--file",
                "pairs.jsonl",
                "--valid-count",
                "0",
                "--test-count",
                "0",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(line) for line in (output / "train.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual([message["role"] for message in rows[0]["messages"]], ["user", "assistant"])

    def test_v3_builder_requires_explicit_pair_source(self) -> None:
        result = self.run_python("build_v3_dataset.py", "--report-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--pairs", result.stderr)

    def test_episode_selectors_require_owner_data_configuration(self) -> None:
        for script in ("eval/pick_episodes.py", "eval/pick_canary_25.py"):
            result = self.run_python(script)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--qdrant-url", result.stderr)
            self.assertIn("--collection", result.stderr)
            self.assertIn("--owner-id", result.stderr)

    def test_canary_comparison_requires_explicit_graph_inputs_and_output(self) -> None:
        result = self.run_python("eval/compare_arms.py")
        self.assertNotEqual(result.returncode, 0)
        for option in ("--port", "--arm-a-graph", "--arm-b-graph", "--out"):
            self.assertIn(option, result.stderr)

    def test_training_shells_are_inert_without_opt_in(self) -> None:
        for script in ("resume_v1.sh", "run_v3_chain.sh"):
            result = subprocess.run(
                ["/bin/bash", str(ROOT / script)],
                text=True,
                capture_output=True,
                timeout=5,
                cwd=ROOT,
                env={"PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 2, (script, result.stderr))
            self.assertIn("BORG_TRAINING_OPT_IN=1", result.stderr)


if __name__ == "__main__":
    unittest.main()
