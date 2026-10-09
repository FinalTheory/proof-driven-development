#!/usr/bin/env python3

from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from pathlib import Path

from harness.model import Graph
from harness.symbolic import (
    DesignObligationError,
    DesignObligationSpec,
    DesignObligationVerifier,
    SymbolicBridge,
    TranslationAssuranceRegistry,
)


ROOT = Path(__file__).resolve().parent
BRIDGE = ROOT / "symbolic_models" / "bridge.yaml"
ASSURANCE = ROOT / "symbolic_models" / "assurance.yaml"
OBLIGATIONS = ROOT / "symbolic_models" / "design_obligations.yaml"


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

    def test_accepted_reconciliation_pending_bridge_gap_is_sat(self) -> None:
        result = self.check("accepted_reconciliation_closes_pending_lifecycle")
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "sat")
        self.assertEqual(result.verdict, "GAP")
        self.assertIsNotNone(result.constrained.model)

    def test_stale_base_speculative_composition_gap_is_sat(self) -> None:
        result = self.check(
            "visible_speculative_representation_valid_on_current_base"
        )
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "sat")
        self.assertEqual(result.verdict, "GAP")
        self.assertIsNotNone(result.constrained.model)

    def test_rejected_lifecycle_historical_gap_is_now_covered(self) -> None:
        obligation = self.spec.require(
            "externally_rejected_edit_closes_active_speculative_lifecycle"
        )
        result = self.verifier.check(obligation, require_trusted=False)
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertEqual(result.verdict, "COVERED")

        weakened = self.verifier.check_with_overrides(
            obligation,
            contract_formula_overrides={
                "rejected_terminal_speculative_retirement": {"bool": True}
            },
        )
        self.assertEqual(weakened.constrained.status, "sat")
        self.assertEqual(weakened.verdict, "GAP")

    def test_visible_frontier_historical_gap_is_now_covered(self) -> None:
        obligation = self.spec.require("visible_canonical_frontier_never_regresses")
        result = self.verifier.check(obligation, require_trusted=False)
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertEqual(result.verdict, "COVERED")

        weakened = self.verifier.check_with_overrides(
            obligation,
            contract_formula_overrides={
                "visible_canonical_frontier_non_regression": {"bool": True}
            },
        )
        self.assertEqual(weakened.constrained.status, "sat")
        self.assertEqual(weakened.verdict, "GAP")

    def test_all_roots_fails_closed_until_every_root_is_symbolically_mapped(self) -> None:
        original = self.spec.require(
            "accepted_reconciliation_closes_pending_lifecycle"
        )
        all_roots = replace(
            original,
            coverage_scope="all_roots",
            claim_ids=(),
            contract_ids=(),
        )
        with self.assertRaisesRegex(
            DesignObligationError,
            "all_roots coverage requires every current root",
        ):
            self.verifier.check(all_roots, require_trusted=False)

    def test_trusted_mode_fails_closed_for_unreviewed_selected_constraints(self) -> None:
        obligation = self.spec.require(
            "accepted_reconciliation_closes_pending_lifecycle"
        )
        with self.assertRaisesRegex(Exception, "requires TRUSTED selected constraints"):
            self.verifier.check(obligation, require_trusted=True)


if __name__ == "__main__":
    unittest.main()
