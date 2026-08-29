"""Isolated proofs for batched Grok REVISE judgment (no live store)."""

from __future__ import annotations

import importlib.machinery
import os
import unittest
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
BIN = BASE / "bin"


def load_dream():
    return importlib.machinery.SourceFileLoader(
        "mem0_dream_batch_judge_test", str(BIN / "mem0-dream")
    ).load_module()


class DreamBatchJudgeTest(unittest.TestCase):
    def test_per_shard_qwen_prompt_remains_complete(self):
        dream = load_dream()
        prompt = dream._serialize_revise_prompt([
            {"id": "legacy-row", "date": "2026-08-28", "text": "The complete original memory text."}
        ])
        self.assertIn("legacy-row", prompt)
        self.assertIn("The complete original memory text.", prompt)
        self.assertIn('"action":"keep"', prompt)

    def test_twenty_five_shards_pack_into_multiple_bounded_batch_prompts(self):
        dream = load_dream()
        events = []
        shards = [
            {
                "shard": number,
                "scope": "isolated",
                "rows": [{"id": f"row-{number}", "date": "2026-08-28", "text": "x" * 96}],
                "judge_prompt": "isolated qwen prompt",
                "admission": None,
            }
            for number in range(1, 26)
        ]
        old = os.environ.get("MEM0_TOKEN_GATE_FORCE_ESTIMATE")
        os.environ["MEM0_TOKEN_GATE_FORCE_ESTIMATE"] = "1"
        try:
            batches = dream._admit_grok_batches(shards, log=events.append)
        finally:
            if old is None:
                os.environ.pop("MEM0_TOKEN_GATE_FORCE_ESTIMATE", None)
            else:
                os.environ["MEM0_TOKEN_GATE_FORCE_ESTIMATE"] = old

        self.assertGreaterEqual(len(batches), 2)
        self.assertTrue(all(2 <= len(batch.rows) <= dream.GROK_BATCH_TARGET_SHARDS for batch in batches))
        self.assertTrue(all(batch.measurement.tokens <= batch.envelope for batch in batches))
        self.assertTrue(all(batch.envelope == 48_000 for batch in batches))
        self.assertTrue(any(line.startswith("TOKEN-ADMISSION phase=REVISE-GROK-BATCH") for line in events))

    def test_malformed_batch_verdict_falls_back_only_for_that_shard(self):
        dream = load_dream()
        events = []
        shards = [
            {
                "shard": number,
                "scope": "isolated",
                "rows": [{"id": f"row-{number}", "date": "2026-08-28", "text": "tiny"}],
                "judge_prompt": f"qwen prompt {number}",
                "admission": None,
            }
            for number in (1, 2)
        ]
        batch = type("Batch", (), {"rows": tuple(shards), "prompt": "isolated batch"})()
        original = (dream.grok_judge, dream.qwen_judge, dream.log)
        qwen_prompts = []
        try:
            dream.grok_judge = lambda _prompt: {
                "verdicts": [
                    {"shard": 1, "action": "keep"},
                    {"shard": "2", "action": "keep"},
                ]
            }
            dream.qwen_judge = lambda _client, prompt: qwen_prompts.append(prompt) or {"action": "keep"}
            dream.log = events.append
            decisions, grok_call_failed = dream._resolve_grok_batch(batch, object())
        finally:
            dream.grok_judge, dream.qwen_judge, dream.log = original

        self.assertFalse(grok_call_failed)
        self.assertEqual({1: "grok-batch", 2: "qwen"}, {item["shard"]: item["via"] for item in decisions})
        self.assertEqual(["qwen prompt 2"], qwen_prompts)
        self.assertTrue(any("JUDGE-FALLBACK shard=2 reason=malformed" in line for line in events))

    def test_grok_batch_call_uses_deterministic_mem0_cwd_and_timeout(self):
        dream = load_dream()
        original, old_enabled = dream.subprocess.run, os.environ.get("DREAM_JUDGE_GROK")
        calls = []
        try:
            os.environ["DREAM_JUDGE_GROK"] = "1"
            dream.subprocess.run = lambda *args, **kwargs: calls.append((args, kwargs)) or type(
                "Result", (), {"returncode": 0, "stdout": '{"verdicts": []}', "stderr": ""}
            )()
            self.assertEqual({"verdicts": []}, dream.grok_judge("isolated batch prompt"))
        finally:
            dream.subprocess.run = original
            if old_enabled is None:
                os.environ.pop("DREAM_JUDGE_GROK", None)
            else:
                os.environ["DREAM_JUDGE_GROK"] = old_enabled

        self.assertEqual(1, len(calls))
        _args, kwargs = calls[0]
        self.assertEqual(dream.BASE, kwargs["cwd"])
        self.assertEqual(420, kwargs["timeout"])
        self.assertEqual("1", kwargs["env"]["MEM0_CAPTURE_SKIP"])

    def test_grok_defaults_on_with_live_cli_proof(self):
        dream = load_dream()
        original, old_enabled = dream.subprocess.run, os.environ.get("DREAM_JUDGE_GROK")
        calls = []
        try:
            os.environ.pop("DREAM_JUDGE_GROK", None)
            dream.subprocess.run = lambda *_args, **_kwargs: calls.append(True)
            self.assertIsNone(dream.grok_judge("must not start a default Grok session"))
        finally:
            dream.subprocess.run = original
            if old_enabled is None:
                os.environ.pop("DREAM_JUDGE_GROK", None)
            else:
                os.environ["DREAM_JUDGE_GROK"] = old_enabled
        # Live CLI proof landed 2026-08-28 ~12:45 MDT (director): a real
        # two-shard batched call answered in 41.4s with schema-valid
        # verdicts correctly mapped (merge newer/retire older; keep for
        # distinct facts). The gate's own condition is satisfied, so the
        # default is now ON; DREAM_JUDGE_GROK=0 remains the off switch.
        self.assertEqual([True], calls)

    def test_first_failed_batch_skips_grok_for_remaining_batches(self):
        dream = load_dream()
        events, qwen_prompts, grok_prompts = [], [], []
        shards = [
            {
                "shard": number,
                "scope": "isolated",
                "rows": [{"id": f"row-{number}", "date": "2026-08-28", "text": "tiny"}],
                "judge_prompt": f"qwen prompt {number}",
                "admission": None,
            }
            for number in range(1, 5)
        ]
        batches = [
            type("Batch", (), {"rows": tuple(shards[:2]), "prompt": "batch one"})(),
            type("Batch", (), {"rows": tuple(shards[2:]), "prompt": "batch two"})(),
        ]
        original, old_enabled = (dream.grok_judge, dream.qwen_judge, dream.log), os.environ.get("DREAM_JUDGE_GROK")
        try:
            os.environ["DREAM_JUDGE_GROK"] = "1"
            dream.grok_judge = lambda prompt: grok_prompts.append(prompt) or None
            dream.qwen_judge = lambda _client, prompt: qwen_prompts.append(prompt) or {"action": "keep"}
            dream.log = events.append
            decisions = dream._judge_shard_batches(batches, object())
        finally:
            dream.grok_judge, dream.qwen_judge, dream.log = original
            if old_enabled is None:
                os.environ.pop("DREAM_JUDGE_GROK", None)
            else:
                os.environ["DREAM_JUDGE_GROK"] = old_enabled

        self.assertEqual(["batch one"], grok_prompts)
        self.assertEqual([f"qwen prompt {number}" for number in range(1, 5)], qwen_prompts)
        self.assertTrue(all(item["via"] == "qwen" for item in decisions))
        self.assertEqual(1, sum(line.startswith("GROK-DOWN") for line in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
