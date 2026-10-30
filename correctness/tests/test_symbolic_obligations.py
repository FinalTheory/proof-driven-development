#!/usr/bin/env python3

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from harness.model import Graph
from harness.symbolic import (
    DesignObligationError,
    DesignObligationSpec,
    DesignObligationVerifier,
    SymbolicBridge,
    TranslationAssuranceRegistry,
)

try:
    from tests.witness_check import (
        build_obligation_program,
        check_program_under_witness,
        load_witness,
    )
except ImportError:  # unittest discover loads tests/ as top-level modules
    from witness_check import (
        build_obligation_program,
        check_program_under_witness,
        load_witness,
    )


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "symbolic_models" / "bridge.yaml"
ASSURANCE = ROOT / "symbolic_models" / "assurance.yaml"
OBLIGATIONS = ROOT / "symbolic_models" / "design_obligations.yaml"


def contains_call(expr, fn_name: str) -> bool:
    if isinstance(expr, dict):
        call = expr.get("call")
        if isinstance(call, dict) and call.get("fn") == fn_name:
            return True
        return any(contains_call(value, fn_name) for value in expr.values())
    if isinstance(expr, list):
        return any(contains_call(value, fn_name) for value in expr)
    return False


class SymbolicDesignObligationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = Graph.load()
        self.bridge = SymbolicBridge.load(BRIDGE)
        self.registry = TranslationAssuranceRegistry.load(ASSURANCE)
        self.spec = DesignObligationSpec.load(OBLIGATIONS)
        self.verifier = DesignObligationVerifier(
            self.graph, self.bridge, self.registry
        )

    def check(self, obligation_id: str):
        return self.verifier.check(
            self.spec.require(obligation_id), require_trusted=False
        )

    def g12_without_clause(self, fn_name: str):
        formula = copy.deepcopy(
            self.bridge.claims["G12_client_pending_state_tracks_unresolved_edits"].formula
        )
        clauses = formula["and"]
        matching = [
            index for index, clause in enumerate(clauses) if contains_call(clause, fn_name)
        ]
        self.assertEqual(len(matching), 1, f"expected one G12 clause containing {fn_name}")
        clauses[matching[0]] = {"bool": True}
        return formula

    def test_accepted_reconciliation_pending_bridge_is_now_covered(self) -> None:
        obligation = self.spec.require("accepted_reconciliation_closes_pending_lifecycle")
        result = self.verifier.check(obligation, require_trusted=False)
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertEqual(result.verdict, "COVERED")

        # The weakened query is solver-undecidable (G2's universally
        # quantified transitive-closure clause makes z3's model-finder
        # return unknown), so the reopened gap is exhibited by a checked
        # finite model instead of solver search.
        weakened_program = build_obligation_program(
            self.verifier,
            self.bridge,
            obligation,
            claim_overrides={
                "G12_client_pending_state_tracks_unresolved_edits": self.g12_without_clause(
                    "accepted_reconciliation_published"
                )
            },
        )
        ok, failing = check_program_under_witness(
            weakened_program, load_witness("accepted_reconciliation")
        )
        self.assertTrue(ok, f"witness does not satisfy the weakened gap query: {failing}")

    def test_stale_base_speculative_composition_gap_is_sat(self) -> None:
        result = self.check(
            "visible_speculative_representation_valid_on_current_base"
        )
        self.assertEqual(result.baseline.status, "sat")
        # The constrained query is solver-undecidable (G2's universally
        # quantified transitive-closure clause makes z3's model-finder
        # return unknown), so the gap is exhibited by a checked finite
        # model: it satisfies the negated obligation together with every
        # root and assumption, which is exactly the GAP claim.
        obligation = self.spec.require(
            "visible_speculative_representation_valid_on_current_base"
        )
        program = build_obligation_program(self.verifier, self.bridge, obligation)
        ok, failing = check_program_under_witness(program, load_witness("stale_base"))
        self.assertTrue(ok, f"witness does not satisfy the gap query: {failing}")

    def test_rejected_lifecycle_historical_gap_is_now_covered(self) -> None:
        obligation = self.spec.require(
            "externally_rejected_edit_closes_active_speculative_lifecycle"
        )
        result = self.verifier.check(obligation, require_trusted=False)
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertEqual(result.verdict, "COVERED")

        weakened_program = build_obligation_program(
            self.verifier,
            self.bridge,
            obligation,
            claim_overrides={
                "G12_client_pending_state_tracks_unresolved_edits": self.g12_without_clause(
                    "definitive_rejection_published_into_state"
                )
            },
        )
        ok, failing = check_program_under_witness(
            weakened_program, load_witness("rejected_lifecycle")
        )
        self.assertTrue(ok, f"witness does not satisfy the weakened gap query: {failing}")

    def test_visible_frontier_historical_gap_is_now_covered(self) -> None:
        obligation = self.spec.require("visible_canonical_frontier_never_regresses")
        result = self.verifier.check(obligation, require_trusted=False)
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertEqual(result.verdict, "COVERED")

        weakened_program = build_obligation_program(
            self.verifier,
            self.bridge,
            obligation,
            contract_overrides={
                "visible_canonical_frontier_non_regression": {"bool": True}
            },
        )
        ok, failing = check_program_under_witness(
            weakened_program, load_witness("visible_frontier")
        )
        self.assertTrue(ok, f"witness does not satisfy the weakened gap query: {failing}")

    def test_all_roots_uses_exact_root_and_assumption_boundary(self) -> None:
        obligation = self.spec.require(
            "accepted_reconciliation_closes_pending_lifecycle"
        )
        result = self.verifier.check(obligation, require_trusted=False)
        expected_roots = {
            node_id
            for node_id, claim in self.graph.claims.items()
            if claim.get("kind") == "root"
        }
        expected_assumptions = set(self.graph.assumptions)
        self.assertEqual(set(result.claim_ids), expected_roots)
        self.assertEqual(set(result.assumption_ids), expected_assumptions)
        self.assertEqual(result.contract_ids, ())

    def _verifier_without_root_assurance(self, root_id: str) -> DesignObligationVerifier:
        records = dict(self.registry.records)
        records.pop(f"claim:{root_id}")
        registry = TranslationAssuranceRegistry(
            version=self.registry.version,
            records=records,
        )
        return DesignObligationVerifier(self.graph, self.bridge, registry)

    def test_all_roots_unreviewed_constraints_cannot_be_canonically_certified(self) -> None:
        obligation = self.spec.require(
            "accepted_reconciliation_closes_pending_lifecycle"
        )
        verifier = self._verifier_without_root_assurance(
            "G12_client_pending_state_tracks_unresolved_edits"
        )
        result = verifier.check(obligation, require_trusted=False)
        self.assertNotIn("OPAQUE", set(result.constraint_trust.values()))
        self.assertEqual(
            result.constraint_trust["claim:G12_client_pending_state_tracks_unresolved_edits"],
            "UNVERIFIED",
        )
        self.assertFalse(result.all_constraints_trusted)
        self.assertFalse(result.canonical_certified)

    def test_trusted_mode_fails_closed_for_unreviewed_all_roots_boundary(self) -> None:
        obligation = self.spec.require(
            "accepted_reconciliation_closes_pending_lifecycle"
        )
        verifier = self._verifier_without_root_assurance(
            "G12_client_pending_state_tracks_unresolved_edits"
        )
        with self.assertRaisesRegex(Exception, "requires TRUSTED constraints"):
            verifier.check(obligation, require_trusted=True)


if __name__ == "__main__":
    unittest.main()
