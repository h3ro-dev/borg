"""Synthetic, zero-model proof; SOURCE_VARIANT=baseline selects the preimage."""
import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from graphiti_core.nodes import EntityNode
from graphiti_core.utils.bulk_utils import resolve_edge_pointers

ROOT = Path(os.environ['BORG_SOURCE_SPAN_FIXTURE'])
variant = os.environ.get('SOURCE_VARIANT', 'candidate')
spec = importlib.util.spec_from_file_location('source_span_nodes', ROOT / variant / 'node_operations.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

def node(name, group='same', uuid='extracted'):
    return EntityNode(name=name, group_id=group, uuid=uuid, labels=['Entity', 'Document'], summary='source claim')

class SourceSpanTests(unittest.TestCase):
    def resolve(self, name, content, candidates=(), group='same', previous=()):
        n = node(name, group)
        episode = None if content is None else NS(content=content, group_id=group)
        async def retrieve(clients, nodes, override):
            return [list(candidates) for _ in nodes]
        async def forbidden(*args, **kwargs):
            raise AssertionError('fuzzy/model resolution must not run')
        with patch.object(ops, '_collect_candidate_nodes', retrieve), patch.object(ops, '_resolve_with_llm', forbidden):
            result = asyncio.run(ops.resolve_extracted_nodes(NS(llm_client=None), [n], episode, list(previous)))
        return n, result

    def test_captured_shape_normalizes_preserving_identity_and_source(self):
        content = 'See `REPAIR.md/evidence` for the receipt.'
        n, (resolved, mapping, pairs) = self.resolve('REPAIR.md', content)
        self.assertEqual(n.name, 'REPAIR.md/evidence')
        self.assertIs(resolved[0], n)
        self.assertEqual(mapping, {'extracted': 'extracted'})
        self.assertEqual(pairs, [])
        self.assertEqual(n.labels, ['Entity', 'Document'])
        self.assertEqual(n.summary, 'source claim')
        self.assertEqual(content, 'See `REPAIR.md/evidence` for the receipt.')

    def test_complete_relative_token_is_document_and_grounded(self):
        token = 'docs/REPAIR.md/evidence/check-1'
        self.assertEqual(ops._exact_identifier_key(token), ('document', token))
        n, (resolved, _, _) = self.resolve(token, '(' + token + ')')
        self.assertIs(resolved[0], n)

    def test_direct_prefix_guard_is_not_normalization(self):
        with self.assertRaisesRegex(ValueError, '^exact_identifier_source_grounding_missing$'):
            ops._exact_identifier_resolution(node('REPAIR.md'), [], NS(content='REPAIR.md/evidence'))

    def test_ambiguous_competing_continuations_refused(self):
        with self.assertRaises(ValueError):
            self.resolve('REPAIR.md', 'REPAIR.md/evidence REPAIR.md/notes')

    def test_repeated_same_complete_token_is_unique(self):
        n, _ = self.resolve('REPAIR.md', 'REPAIR.md/evidence; REPAIR.md/evidence')
        self.assertEqual(n.name, 'REPAIR.md/evidence')

    def test_standalone_plus_continuation_retains_exact_standalone(self):
        n, _ = self.resolve('REPAIR.md', 'REPAIR.md REPAIR.md/evidence')
        self.assertEqual(n.name, 'REPAIR.md')

    def test_absent_or_hallucinated_prefix_refused_even_with_candidate(self):
        with self.assertRaisesRegex(ValueError, 'source_grounding_missing'):
            self.resolve('REPAIR.md', 'OTHER.md/evidence', [node('REPAIR.md/evidence', uuid='existing')])

    def test_suffix_and_path_boundary_adversaries_refused(self):
        for text in ('REPAIR.md.backup/evidence', 'xREPAIR.md/evidence', 'dir/REPAIR.md/evidence',
                     '/REPAIR.md/evidence', 'REPAIR.md/evidence?next', 'REPAIR.md/evidence#anchor',
                     'REPAIR.md/evidence:other', 'REPAIR.md/evidence\\other', 'REPAIR.md/evidence%2Fother',
                     'REPAIR.md/evidence/../other', 'REPAIR.md/evidence//other', 'REPAIR.md/evidence/',
                     'REPAIR.md/evidence.', 'REPAIR.md/evidence—other'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.resolve('REPAIR.md', text)

    def test_full_token_guard_rejects_suffix_or_longer_path(self):
        for content in ('REPAIR.md/evidence.backup', 'x/REPAIR.md/evidence', 'REPAIR.md/evidence/next'):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, 'source_grounding_missing'):
                ops._exact_identifier_resolution(node('REPAIR.md/evidence'), [], NS(content=content))

    def test_relative_document_prefix_and_case_are_preserved(self):
        n, _ = self.resolve('docs/RePaIr.md', 'docs/RePaIr.md/Evidence')
        self.assertEqual(n.name, 'docs/RePaIr.md/Evidence')
        with self.assertRaises(ValueError):
            self.resolve('docs/repair.md', 'docs/RePaIr.md/Evidence')

    def test_plain_slash_prose_not_document_identifiers(self):
        for name in ('and/or', 'input/output', 'some/path', 'repair.mdish/evidence'):
            with self.subTest(name=name):
                self.assertIsNone(ops._exact_identifier_key(name))
                n, _ = self.resolve(name, name)
                self.assertEqual(n.name, name)

    def test_exact_same_group_dedup_and_edge_pointer_mapping(self):
        target = node('REPAIR.md/evidence', uuid='existing')
        wrong_group = node(target.name, group='other', uuid='foreign')
        case_variant = node('repair.md/evidence', uuid='lower')
        n, (resolved, mapping, pairs) = self.resolve('REPAIR.md', 'REPAIR.md/evidence', [wrong_group, case_variant, target])
        self.assertIs(resolved[0], target)
        self.assertEqual(mapping, {'extracted': 'existing'})
        self.assertEqual(pairs, [(n, target)])
        edge = NS(source_node_uuid=n.uuid, target_node_uuid='unrelated', fact='source claim')
        resolve_edge_pointers([edge], mapping)
        self.assertEqual((edge.source_node_uuid, edge.target_node_uuid, edge.fact), ('existing', 'unrelated', 'source claim'))

    def test_cross_group_or_prefix_candidate_cannot_rescue_or_replace(self):
        n, (resolved, mapping, _) = self.resolve('REPAIR.md', 'REPAIR.md/evidence',
            [node('REPAIR.md/evidence', group='other', uuid='foreign'), node('REPAIR.md', uuid='prefix')])
        self.assertIs(resolved[0], n)
        self.assertEqual(mapping['extracted'], 'extracted')

    def test_multiple_same_group_exact_candidates_keep_source_identity(self):
        n, (resolved, mapping, _) = self.resolve('REPAIR.md', 'REPAIR.md/evidence',
            [node('REPAIR.md/evidence', uuid='one'), node('REPAIR.md/evidence', uuid='two')])
        self.assertIs(resolved[0], n)
        self.assertEqual(mapping['extracted'], 'extracted')

    def test_previous_episode_is_not_normalization_authority(self):
        with self.assertRaises(ValueError):
            self.resolve('REPAIR.md', 'OTHER.md', previous=[NS(content='REPAIR.md/evidence')])

    def test_different_source_group_cannot_normalize(self):
        n = node('REPAIR.md')
        async def retrieve(*args):
            return [[]]
        with patch.object(ops, '_collect_candidate_nodes', retrieve), self.assertRaises(ValueError):
            asyncio.run(ops.resolve_extracted_nodes(NS(llm_client=None), [n], NS(content='REPAIR.md/evidence', group_id='foreign')))
        self.assertEqual(n.name, 'REPAIR.md')

    def test_normalization_precedes_retrieval_and_keeps_per_node_candidates(self):
        nodes = [node('REPAIR.md', uuid='a'), node('OTHER.md', uuid='b')]
        target = node('REPAIR.md/evidence', uuid='existing')
        async def retrieve(clients, actual, override):
            self.assertEqual([n.name for n in actual], ['REPAIR.md/evidence', 'OTHER.md/notes'])
            return [[], [target]]  # The exact candidate belongs to the other search result.
        async def forbidden(*args, **kwargs):
            raise AssertionError('fuzzy/model resolution must not run')
        with patch.object(ops, '_collect_candidate_nodes', retrieve), patch.object(ops, '_resolve_with_llm', forbidden):
            resolved, mapping, pairs = asyncio.run(ops.resolve_extracted_nodes(NS(llm_client=None), nodes,
                NS(content='REPAIR.md/evidence OTHER.md/notes', group_id='same')))
        self.assertEqual(resolved, nodes)
        self.assertEqual(mapping, {'a': 'a', 'b': 'b'})
        self.assertEqual(pairs, [])

    def test_unsupported_competing_token_is_not_discarded_for_uniqueness(self):
        with self.assertRaises(ValueError):
            self.resolve('REPAIR.md', 'REPAIR.md/evidence REPAIR.md/notes?query')

    def test_no_episode_never_expands_explicit_node(self):
        n, _ = self.resolve('REPAIR.md', None, [node('REPAIR.md/evidence', uuid='existing')])
        self.assertEqual(n.name, 'REPAIR.md')

    def test_source_full_path_basename_and_standalone_guard_preserved(self):
        for content in ('REPAIR.md', '/work/REPAIR.md'):
            n = node('REPAIR.md')
            self.assertIs(ops._exact_identifier_resolution(n, [], NS(content=content)), n)

    def test_grounding_function_byte_identical_to_preimage(self):
        import ast
        def body(path):
            text = path.read_text()
            f = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == '_exact_identifier_resolution')
            return ''.join(text.splitlines(keepends=True)[f.lineno-1:f.end_lineno])
        self.assertEqual(body(ROOT / 'baseline/node_operations.py'), body(ROOT / variant / 'node_operations.py'))

if __name__ == '__main__':
    unittest.main(verbosity=2)
