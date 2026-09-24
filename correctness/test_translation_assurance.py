#!/usr/bin/env python3

import copy
import unittest
from pathlib import Path

import yaml

from harness.model import Graph
from harness.symbolic import (
    AssuranceStatus,
    ComparisonReview,
    ComparisonVerdict,
    SubjectTrustStatus,
    SymbolicBridge,
    SymbolicVerifier,
    TranslationAssuranceRegistry,
    TranslationReviewError,
    TranslationStatus,
    assurance_record_payload,
    aggregate_translation_assurance,
    build_claim_translation_subject,
    build_direct_comparison_prompt,
    build_roundtrip_comparison_prompt,
    build_roundtrip_translation_prompt,
    build_translation_subject,
    parse_comparison_review,
    parse_comparison_review_or_incomplete,
    parse_roundtrip_translation,
    parse_roundtrip_translation_or_incomplete,
    translation_subject_signature,
)


BRIDGE_PATH = Path(__file__).with_name("symbolic_models") / "bridge.yaml"
ASSURANCE_PATH = Path(__file__).with_name("symbolic_models") / "assurance.yaml"

class TranslationAssuranceTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph.load()
        self.bridge = SymbolicBridge.load(BRIDGE_PATH)
        self.subject = build_translation_subject(
            self.graph,
            self.bridge,
            "accepted_evidence_correspondence",
        )
        self.signature = translation_subject_signature(self.subject)

    def _trusted_registry(self):
        equivalent = ComparisonReview(
            ComparisonVerdict.EQUIVALENT,
            "equivalent",
            (),
            self.signature,
        )
        roundtrip = parse_roundtrip_translation(
            f"""
subject_signature: {self.signature}
status: TRANSLATED
natural_language: For every eligible identity, evidence exists iff exactly one committed acceptance exists.
ambiguities: []
"""
        )
        record = assurance_record_payload(
            self.subject,
            roundtrip=roundtrip,
            direct_reviews={
                "weakening": equivalent,
                "strengthening": equivalent,
            },
            roundtrip_reviews={
                "weakening": equivalent,
                "strengthening": equivalent,
            },
        )
        return TranslationAssuranceRegistry.from_data(
            {
                "version": 1,
                "records": {
                    "contract:accepted_evidence_correspondence": record,
                },
            }
        )

    def test_claim_subject_reuses_contract_vocabulary_with_explicit_formula(self):
        claim = build_claim_translation_subject(
            self.graph,
            self.bridge,
            "L108_acceptance_preserves_request_identity",
        )
        self.assertEqual(
            claim.contract_ids,
            ("acceptance_request_identity_binding",),
        )
        self.assertIn("canonical acceptance produced from a submitted logical request", claim.source_context())
        self.assertIn("acceptance_request_identity_binding", claim.symbolic_context())
        self.assertIn("acceptance_identity_preserved_end_to_end", claim.symbolic_context())
        self.assertIn("durable_evidence_identity_matches_request", claim.symbolic_context())

    def test_claim_formula_can_project_narrower_property_than_referenced_contract(self):
        claim = build_claim_translation_subject(
            self.graph,
            self.bridge,
            "L92_captured_recovery_head_equals_authoritative_frontier",
        )
        self.assertIn("recovery_head_value", claim.symbolic_context())
        self.assertNotIn("recovery_head_preserved", claim.symbolic_context())
        self.assertIn("acceptance resumption", claim.source_context())
        self.assertIn("context only", claim.source_context())

    def test_roundtrip_prompt_is_blind_to_source_contract(self):
        prompt = build_roundtrip_translation_prompt(self.subject)
        definition = self.subject.canonical_contract["definition"]
        self.assertNotIn(definition, prompt)
        self.assertIn("accepted_evidence_for_key", prompt)
        self.assertIn("within_retry_reconnect_eligibility", prompt)
        self.assertIn("declared dimensions above are authoritative", prompt)
        self.assertIn("do not collapse distinct entity, state, or observation dimensions", prompt)

    def test_direct_prompt_contains_source_and_symbolic_formula(self):
        prompt = build_direct_comparison_prompt(self.subject, focus="weakening")
        self.assertIn("During the 30-day normal retry/reconnect eligibility horizon", prompt)
        self.assertIn("accepted_evidence_for_key", prompt)
        self.assertIn("omitted requirements", prompt)
        self.assertIn("helper predicate/function meanings must not silently supply", prompt)

    def test_roundtrip_comparison_prompt_uses_source_and_blind_rendering(self):
        prompt = build_roundtrip_comparison_prompt(
            self.subject,
            "For eligible identities, evidence exists exactly when one committed acceptance exists.",
            focus="strengthening",
        )
        self.assertIn("During the 30-day normal retry/reconnect eligibility horizon", prompt)
        self.assertIn("For eligible identities", prompt)
        self.assertIn("stronger universal scope", prompt)
        self.assertIn("semantic-dimensionality preservation", prompt)

    def test_roundtrip_parser_enforces_contract(self):
        parsed = parse_roundtrip_translation(
            f"""
subject_signature: {self.signature}
status: TRANSLATED
natural_language: Evidence and acceptance correspond inside the eligibility boundary.
ambiguities: []
"""
        )
        self.assertEqual(parsed.status, TranslationStatus.TRANSLATED)

    def test_roundtrip_parser_strips_writer_completion_sentinel(self):
        parsed = parse_roundtrip_translation(
            f"""
subject_signature: {self.signature}
status: TRANSLATED
natural_language: Equivalent proposition.
ambiguities: []
WRITERSUBAGENTCOMPLETE7D3A9F6C
"""
        )
        self.assertEqual(parsed.status, TranslationStatus.TRANSLATED)

    def test_invalid_agent_artifact_becomes_incomplete(self):
        comparison = parse_comparison_review_or_incomplete("not: expected")
        self.assertEqual(comparison.verdict, ComparisonVerdict.INCOMPLETE)
        translation = parse_roundtrip_translation_or_incomplete("not: expected")
        self.assertEqual(translation.status, TranslationStatus.INCOMPLETE)

    def test_comparison_parser_enforces_empty_differences_for_equivalent(self):
        parsed = parse_comparison_review(
            f"""
subject_signature: {self.signature}
verdict: EQUIVALENT
reason: Quantifiers, eligibility guard, and biconditional are preserved.
differences: []
"""
        )
        self.assertEqual(parsed.verdict, ComparisonVerdict.EQUIVALENT)

    def test_aggregation_requires_two_independent_reviews_per_path(self):
        equivalent = ComparisonReview(
            ComparisonVerdict.EQUIVALENT,
            "equivalent",
            (),
        )
        incomplete = aggregate_translation_assurance(
            direct_reviews=[equivalent],
            roundtrip_reviews=[equivalent, equivalent],
        )
        self.assertEqual(incomplete.status, AssuranceStatus.INCOMPLETE)

        trusted = aggregate_translation_assurance(
            direct_reviews=[equivalent, equivalent],
            roundtrip_reviews=[equivalent, equivalent],
        )
        self.assertEqual(trusted.status, AssuranceStatus.TRUSTED)

    def test_any_semantic_disagreement_rejects_translation(self):
        equivalent = ComparisonReview(
            ComparisonVerdict.EQUIVALENT,
            "equivalent",
            (),
        )
        stronger = ComparisonReview(
            ComparisonVerdict.STRONGER,
            "formula applies outside source scope",
            ("scope widened",),
        )
        summary = aggregate_translation_assurance(
            direct_reviews=[equivalent, stronger],
            roundtrip_reviews=[equivalent, equivalent],
        )
        self.assertEqual(summary.status, AssuranceStatus.REJECTED)


    def test_assurance_record_rejects_review_from_other_subject_signature(self):
        equivalent = ComparisonReview(
            ComparisonVerdict.EQUIVALENT,
            "equivalent",
            (),
            self.signature,
        )
        stale = ComparisonReview(
            ComparisonVerdict.EQUIVALENT,
            "equivalent but from another prompt",
            (),
            "0" * 64,
        )
        roundtrip = parse_roundtrip_translation(
            f"""
subject_signature: {self.signature}
status: TRANSLATED
natural_language: Equivalent proposition.
ambiguities: []
"""
        )
        with self.assertRaises(TranslationReviewError):
            assurance_record_payload(
                self.subject,
                roundtrip=roundtrip,
                direct_reviews={
                    "weakening": stale,
                    "strengthening": equivalent,
                },
                roundtrip_reviews={
                    "weakening": equivalent,
                    "strengthening": equivalent,
                },
            )

    def test_assurance_registry_marks_matching_subject_trusted(self):
        registry = self._trusted_registry()
        result = registry.evaluate(
            self.graph,
            self.bridge,
            kind="contract",
            subject_id="accepted_evidence_correspondence",
        )
        self.assertEqual(result.status, SubjectTrustStatus.TRUSTED)
        self.assertTrue(result.trusted)

    def test_assurance_registry_marks_changed_symbolic_meaning_stale(self):
        registry = self._trusted_registry()
        raw = yaml.safe_load(BRIDGE_PATH.read_text())
        changed = copy.deepcopy(raw)
        changed["functions"]["accepted_evidence_for_key"]["meaning"] += " Changed."
        changed_bridge = SymbolicBridge.from_data(changed)
        result = registry.evaluate(
            self.graph,
            changed_bridge,
            kind="contract",
            subject_id="accepted_evidence_correspondence",
        )
        self.assertEqual(result.status, SubjectTrustStatus.STALE)

    def test_trusted_verifier_requires_current_assurance_record(self):
        registry = self._trusted_registry()
        verifier = SymbolicVerifier(self.graph, self.bridge, registry)
        self.assertEqual(
            verifier.trusted_check(
                {"bool": True},
                contracts=["accepted_evidence_correspondence"],
            ).status,
            "sat",
        )
        with self.assertRaises(TranslationReviewError):
            verifier.trusted_check(
                {"bool": True},
                contracts=["client_canonical_frontier_coherence"],
            )


if __name__ == "__main__":
    unittest.main()
