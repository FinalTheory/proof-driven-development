from __future__ import annotations

import copy
import unittest
from pathlib import Path

from harness.formalization import (
    composition_formalization_route,
    formalization_snapshot,
    subject_formalization_state,
)
from harness.model import Graph
from harness.symbolic import SymbolicBridge, TranslationAssuranceRegistry


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "symbolic_models" / "bridge.yaml"
ASSURANCE = ROOT / "symbolic_models" / "assurance.yaml"


class FormalizationRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = Graph.load()
        self.bridge = SymbolicBridge.load(BRIDGE)
        self.registry = TranslationAssuranceRegistry.load(ASSURANCE)

    def test_registry_is_exhaustive_for_all_canonical_subjects(self) -> None:
        snapshot = formalization_snapshot(self.graph, self.bridge, self.registry)
        self.assertEqual(sum(snapshot["counts"]["claims"].values()), len(self.graph.claims))
        self.assertEqual(sum(snapshot["counts"]["assumptions"].values()), len(self.graph.assumptions))
        self.assertEqual(
            sum(snapshot["counts"]["semantic_contracts"].values()),
            len(self.graph.doc["catalog"]["semantic_contracts"]),
        )

    def test_symbolic_subject_without_mapping_is_explicitly_blocked(self) -> None:
        state = subject_formalization_state(
            self.graph,
            self.bridge,
            self.registry,
            kind="claim",
            subject_id="G0_acked_edit_survives_failover",
        )
        self.assertEqual(state.mode, "symbolic")
        self.assertEqual(state.effective, "MAPPING_MISSING")

    def test_trusted_symbolic_subject_is_ready(self) -> None:
        state = subject_formalization_state(
            self.graph,
            self.bridge,
            self.registry,
            kind="claim",
            subject_id="L20_acceptance_paths_are_authoritatively_mediated",
        )
        self.assertEqual(state.effective, "SYMBOLIC_READY")

    def test_symbolic_composition_never_falls_back_to_llm(self) -> None:
        route = composition_formalization_route(
            self.graph,
            self.bridge,
            self.registry,
            "C12_live_delivery_contains_only_accepted_changes",
        )
        self.assertEqual(route["backend"], "symbolic")
        self.assertEqual(route["state"], "BLOCKED")
        self.assertTrue(route["blocking"])

    def test_explicit_non_symbolic_target_routes_to_non_symbolic_backend(self) -> None:
        doc = copy.deepcopy(self.graph.doc)
        doc["formalization"]["claims"]["G0_acked_edit_survives_failover"] = {
            "mode": "non_symbolic",
            "reason": "unsupported_logic",
            "rationale": "Synthetic routing test only.",
        }
        graph = Graph(doc, repository_root=self.graph.repository_root)
        route = composition_formalization_route(
            graph,
            self.bridge,
            self.registry,
            "G0_acked_edit_survives_failover",
        )
        self.assertEqual(route["backend"], "non_symbolic")
        self.assertEqual(route["state"], "READY")


if __name__ == "__main__":
    unittest.main()
