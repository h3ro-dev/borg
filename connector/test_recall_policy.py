"""Synthetic retrieval controls; no live corpus reads, writes, or identity claims."""
import copy
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp.exceptions import ToolError
import borg_context_server as server
from recall_policy import exact_terms, select_memories, unavailable_retrieval


def row(text, score=0.7, **extra):
    return {"id": "fixture", "memory": text, "score": score, **extra}


class SelectionTests(unittest.TestCase):
    def test_absent_opaque_identifier_abstains_despite_high_scores(self):
        values = [row("Unrelated hexadecimal source", 0.95), row("Unrelated audit state", 0.99)]
        kept, result = select_memories("fixture_82f97a6d4b1c_missing_identifier", values, 6)
        self.assertEqual(kept, [])
        self.assertEqual(result["result_floor"], "sparse")
        self.assertEqual(result["retrieval"]["omitted"]["exact_mismatch"], 2)
        self.assertEqual(result["retrieval"]["coverage"], "upstream_candidate_pool_only")

    def test_full_name_requires_explicit_quotes_not_capitalization_guessing(self):
        values = [row("Ada Another leads sales", 0.99), row("Another Example leads sales", 0.98),
                  row("Ada Example leads operations", 0.08)]
        kept, result = select_memories('"Ada Example" role', values, 6)
        self.assertEqual([r["memory"] for r in kept], ["Ada Example leads operations"])
        self.assertEqual(result["retrieval"]["match_policy"], "literal_terms")
        self.assertEqual(exact_terms("Ada Example role"), [])

    def test_identifier_prefix_is_not_identity(self):
        token = "0123456789abcdef"
        values = [row(token + "a"), row("f" + token), row("prefix_" + token), row("Commit " + token)]
        kept, _ = select_memories(token, values, 6)
        self.assertEqual(kept, values[-1:])

    def test_identifier_suffix_and_native_row_id(self):
        identifier = "0123456789abcdef"
        values = [row(identifier + "-suffix"), row(identifier + ":suffix"),
                  row("Native record without the ID in its text", id=identifier)]
        kept, _ = select_memories(identifier, values, 6)
        self.assertEqual(kept, values[-1:])

    def test_opaque_id_allows_sentence_punctuation_but_not_suffixes(self):
        identifier = "0123456789abcdef"
        values = [row("The commit is " + identifier + "."),
                  row("The commit is (" + identifier + "), confirmed."),
                  row(identifier + ".different"), row(identifier + "-different")]
        kept, _ = select_memories(identifier, values, 6)
        self.assertEqual(kept, values[:2])

    def test_oversized_numeric_score_is_filtered_without_overflow(self):
        kept, result = select_memories("general", [row("invalid score", 10 ** 1000)], 6)
        self.assertEqual(kept, [])
        self.assertEqual(result["retrieval"]["omitted"]["invalid_score"], 1)

    def test_uuid_is_opaque_but_dates_and_short_codes_are_not(self):
        identifier = "12345678-1234-1234-1234-123456789abc"
        self.assertEqual(exact_terms(identifier), [identifier])
        for query in ["2026-09-24", "test v1.2", "HTTP 404", "issue 123", "epoch_mismatch"]:
            self.assertEqual(exact_terms(query), [], query)

    def test_semantic_candidates_preserve_valid_low_scores_and_order(self):
        values = [row("A vehicle for going places", 0.08), row("An automobile", -0.1),
                  row("Legacy score missing", None)]
        kept, result = select_memories("transportation", values, 3)
        self.assertEqual(kept, values)
        self.assertEqual(result["retrieval"]["status"], "candidates")
        self.assertEqual(result["retrieval"]["match_policy"], "semantic_candidates")

    def test_whole_record_junk_is_withheld_but_discussion_is_retained(self):
        values = [row("The tool used is tool.", 1.0), row("  THE tool\nused is tool.  "),
                  row("The tool is called 'tool'."),
                  row('The extractor must reject "The tool used is tool." as vacuous.'),
                  row("The file tool is an executable that accepts a path.")]
        kept, result = select_memories("extraction quality", values, 6)
        self.assertEqual(kept, values[3:])
        self.assertEqual(result["retrieval"]["omitted"]["vacuous"], 3)

    def test_nonfinite_and_malformed_candidates_are_explicit(self):
        values = [None, {}, row(""), row("NaN", float("nan")), row("Inf", float("inf")),
                  row("Boolean", True), row("String score", "1.0"), row("Valid")]
        kept, result = select_memories("general", values, 6)
        self.assertEqual(kept, values[-1:])
        self.assertEqual(result["retrieval"]["omitted"]["malformed"], 3)
        self.assertEqual(result["retrieval"]["omitted"]["invalid_score"], 4)

    def test_bad_response_shape_is_not_an_empty_success(self):
        for value in ({"results": []}, None, "bad", ()):
            with self.subTest(value=value), self.assertRaises(ToolError):
                select_memories("general", value, 6)

    def test_exact_phrases_are_literal_whitespace_normalized_and_unicode_safe(self):
        values = [row("Élodie  Example owns a.*b"), row("Elodie Example owns axxb")]
        kept, _ = select_memories('"Élodie Example" "a.*b"', values, 6)
        self.assertEqual(kept, values[:1])

    def test_surname_prefix_does_not_satisfy_phrase(self):
        values = [row("Ada Examples works here"), row("Ada Example works here")]
        kept, _ = select_memories('"Ada Example"', values, 6)
        self.assertEqual(kept, values[1:])

    def test_all_explicit_terms_must_match(self):
        values = [row("alpha beta"), row("alpha"), row("beta")]
        kept, _ = select_memories('"alpha" and "beta"', values, 6)
        self.assertEqual(kept, values[:1])

    def test_no_mutation_and_honest_truncation_counts(self):
        values = [row("candidate " + str(i)) for i in range(20)]
        before = copy.deepcopy(values)
        kept, result = select_memories("general", values, 100)
        self.assertEqual(len(kept), 12)
        self.assertEqual(result["retrieval"]["limit_omitted"], 8)
        self.assertEqual(values, before)

    def test_empty_is_distinct_from_unavailable(self):
        kept, result = select_memories("general", [], 6)
        self.assertEqual(kept, [])
        self.assertEqual(result["retrieval"]["candidate_count"], 0)
        self.assertEqual(result["retrieval"]["status"], "sparse")
        self.assertEqual(unavailable_retrieval()["retrieval"]["status"], "unavailable")
        self.assertIsNone(unavailable_retrieval()["retrieval"]["candidate_count"])


class PublicBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = server.BorgContext(SimpleNamespace(project_roots=(Path("/example/projects"),)))
        @asynccontextmanager
        async def upstream():
            yield None, None
        self.context.upstream = upstream

    async def test_search_and_project_share_exact_selection_and_legacy_fields(self):
        values = [row("Ada Another leads sales", 0.99), row("Ada Example leads operations", 0.1)]
        with patch.object(server, "authorize"), patch.object(server, "mem0_call", AsyncMock(return_value=values)) as upstream, patch.object(server, "_repos_matching", return_value=[]):
            search = await self.context.borg_search('"Ada Example" role', 6)
            project = await self.context.borg_project_context("demo", '"Ada Example" role', 6)
        self.assertEqual(search["results"], project["recalled_context"])
        self.assertEqual(search["result_floor"], project["result_floor"])
        self.assertEqual(search["retrieval"], project["retrieval"])
        self.assertEqual(search["results"][0]["authority"], "candidate")
        self.assertIn("source_notice", search)
        self.assertEqual(project["memory_status"], "PASS")
        self.assertIn("live_repositories", project)
        self.assertEqual(upstream.call_args_list[0].args[2]["limit"], 24)

    async def test_upstream_outage_is_not_successful_sparse_recall(self):
        with patch.object(server, "authorize"), patch.object(server, "mem0_call", AsyncMock(side_effect=ToolError("fixture unavailable"))), patch.object(server, "_repos_matching", return_value=[]):
            with self.assertRaises(ToolError):
                await self.context.borg_search("general")
            project = await self.context.borg_project_context("demo")
        self.assertEqual(project["memory_status"], "UNAVAILABLE")
        self.assertEqual(project["result_floor"], "unavailable")
        self.assertIsNone(project["retrieval"]["candidate_count"])

    async def test_wrong_identity_still_fails_closed(self):
        with patch.object(server, "authorize"), patch.object(server, "mem0_call", AsyncMock(side_effect=ToolError("identity or scope differs"))), patch.object(server, "_repos_matching") as repos:
            with self.assertRaises(ToolError):
                await self.context.borg_project_context("demo")
            repos.assert_not_called()

    async def test_junk_does_not_refill_requested_slots(self):
        values = [row("The tool used is tool.", 1.0)] * 12
        with patch.object(server, "mem0_call", AsyncMock(return_value=values)):
            result = await self.context.borg_search("tool", 6)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["result_floor"], "sparse")
        self.assertEqual(result["retrieval"]["omitted"]["vacuous"], 12)


if __name__ == "__main__":
    unittest.main()
