import importlib.machinery
import os
import unittest
from borg_test_support import activate

activate()
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "mem0_runtime_contract.py"
CONTRACT = importlib.machinery.SourceFileLoader("mem0_runtime_contract_test", str(SCRIPT)).load_module()


class RuntimeContractTests(unittest.TestCase):
    def test_write_metadata_requires_scope_and_source(self):
        with self.assertRaises(ValueError):
            CONTRACT.write_metadata(scope="", source="mcp")
        with self.assertRaises(ValueError):
            CONTRACT.write_metadata(scope="team:project", source="")

    def test_write_metadata_carries_versioned_identity(self):
        meta = CONTRACT.write_metadata(scope="team:project", source="unit-test", extra={"kind": "fact"})
        self.assertEqual(CONTRACT.MEMORY_SCHEMA_VERSION, meta["schema_version"])
        self.assertEqual(CONTRACT.EMBED_MODEL_ID, meta["embedding_model_id"])
        self.assertEqual(CONTRACT.EMBED_DIMS, meta["embedding_dimensions"])
        self.assertEqual(CONTRACT.EXTRACTOR_MODEL_ID, meta["extractor_model_id"])
        self.assertEqual("fact", meta["kind"])
        ok, problems = CONTRACT.validate_new_write(meta)
        self.assertTrue(ok, problems)

    def test_reserved_identity_fields_cannot_be_overridden(self):
        with self.assertRaises(ValueError):
            CONTRACT.write_metadata(
                scope="team:project",
                source="unit-test",
                extra={"embedding_model_id": "wrong"},
            )

    def test_validation_rejects_dimension_or_schema_drift(self):
        meta = CONTRACT.write_metadata(scope="team:project", source="unit-test")
        meta["embedding_dimensions"] += 1
        meta["schema_version"] += 1
        ok, problems = CONTRACT.validate_new_write(meta)
        self.assertFalse(ok)
        self.assertIn("embedding_dimensions:mismatch", problems)
        self.assertIn("schema_version:mismatch", problems)


if __name__ == "__main__":
    unittest.main()
