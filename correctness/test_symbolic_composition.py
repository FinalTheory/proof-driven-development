#!/usr/bin/env python3

import unittest
from pathlib import Path

from harness.model import Graph
from harness.symbolic import (
    SymbolicBridge,
    SymbolicCompositionVerifier,
    TranslationAssuranceRegistry,
)


BRIDGE_PATH = Path(__file__).with_name("symbolic_models") / "google_docs.poc.yaml"
ASSURANCE_PATH = Path(__file__).with_name("symbolic_models") / "assurance.poc.yaml"
TARGET = "C12_live_delivery_contains_only_accepted_changes"
PREMISE = "L116_live_delivery_is_bound_to_committed_acceptance"


def var(name):
    return {"var": name}


def call(fn, *args):
    return {"call": {"fn": fn, "args": list(args)}}


def implies(left, right):
    return {"implies": [left, right]}


def exists(vars_, body):
    return {"exists": {"vars": vars_, "body": body}}


def forall(vars_, body):
    return {"forall": {"vars": vars_, "body": body}}


class SymbolicCompositionTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph.load()
        self.bridge = SymbolicBridge.load(BRIDGE_PATH)

    def test_c12_composition_is_entailed_by_l116_symbolically(self):
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge)
        result = verifier.check(TARGET, require_trusted=False)
        self.assertEqual(result.premise_ids, (PREMISE,))
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")
        self.assertTrue(result.machine_checked)

    def test_weakening_l116_by_dropping_committed_source_breaks_composition(self):
        weakened_l116 = forall(
            {"d": "LiveDelivery"},
            implies(
                call("live_delivery_record", var("d")),
                exists(
                    {"a": "Acceptance"},
                    call("live_delivery_originates_from", var("d"), var("a")),
                ),
            ),
        )
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge)
        result = verifier.check_with_premise_overrides(
            TARGET,
            premise_formula_overrides={PREMISE: weakened_l116},
        )
        self.assertEqual(result.solver_result.status, "sat")
        self.assertEqual(result.verdict, "COUNTEREXAMPLE")
        self.assertFalse(result.machine_checked)
        self.assertIsNotNone(result.solver_result.model)

    def test_trusted_composition_succeeds_for_reviewed_target_and_premise(self):
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        result = verifier.check(TARGET, require_trusted=True)
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")
        self.assertTrue(result.machine_checked)


if __name__ == "__main__":
    unittest.main()
