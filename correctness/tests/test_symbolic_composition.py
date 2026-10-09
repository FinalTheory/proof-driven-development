#!/usr/bin/env python3

import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from harness.model import Graph
from harness.symbolic import (
    SymbolicBridge,
    SymbolicCompositionError,
    SymbolicCompositionVerifier,
    TranslationAssuranceRegistry,
    TranslationReviewError,
)
from harness.symbolic.cli import _cmd_symbolic_report


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "symbolic_models" / "bridge.yaml"
ASSURANCE_PATH = Path(__file__).resolve().parents[1] / "symbolic_models" / "assurance.yaml"

C12_TARGET = "C12_live_delivery_contains_only_accepted_changes"
C12_PREMISE = "L116_live_delivery_is_bound_to_committed_acceptance"

C14_TARGET = "C14_all_acceptance_paths_honor_idempotency_identity"
C14_PREMISES = (
    "L20_acceptance_paths_are_authoritatively_mediated",
    "L21_accepted_key_gate_rejects_new_acceptance",
)


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
        result = verifier.check(C12_TARGET, require_trusted=False)
        self.assertEqual(result.premise_ids, (C12_PREMISE,))
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")
        self.assertTrue(result.machine_checked)

    def test_weakening_l116_by_dropping_committed_source_breaks_composition(self):
        weakened_l116 = forall(
            {"d": "LiveDelivery"},
            implies(
                call("live_delivery_record", var("d")),
                exists(
                    {"a": "CanonicalAcceptance"},
                    call("live_delivery_originates_from", var("d"), var("a")),
                ),
            ),
        )
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge)
        result = verifier.check_with_premise_overrides(
            C12_TARGET,
            premise_formula_overrides={C12_PREMISE: weakened_l116},
        )
        self.assertEqual(result.solver_result.status, "sat")
        self.assertEqual(result.verdict, "COUNTEREXAMPLE")
        self.assertFalse(result.machine_checked)
        self.assertIsNotNone(result.solver_result.model)

    def test_trusted_c12_composition_requires_fresh_translation_review(self):
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        with self.assertRaises(TranslationReviewError) as ctx:
            verifier.check(C12_TARGET, require_trusted=True)
        self.assertIn("STALE", str(ctx.exception))

    def test_stale_c12_machine_check_is_not_recorded(self):
        entry = self.graph.doc["assurance"]["composition"][C12_TARGET]
        self.assertEqual(entry["status"], "unaudited")
        self.assertNotIn("artifact_refs", entry)

    def test_c14_two_premise_composition_is_entailed_trusted(self):
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        result = verifier.check(C14_TARGET, require_trusted=True)
        self.assertEqual(result.premise_ids, C14_PREMISES)
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")
        self.assertTrue(result.machine_checked)

    def test_c14_mutations_show_each_direct_premise_is_necessary(self):
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge)
        for premise in C14_PREMISES:
            with self.subTest(premise=premise):
                result = verifier.check_with_premise_overrides(
                    C14_TARGET,
                    premise_formula_overrides={premise: {"bool": True}},
                )
                self.assertEqual(result.solver_result.status, "sat")
                self.assertEqual(result.verdict, "COUNTEREXAMPLE")
                self.assertIsNotNone(result.solver_result.model)

    def test_symbolic_report_renders_only_machine_checked_compositions_by_default(self):
        args = SimpleNamespace(
            node=[],
            model=str(BRIDGE_PATH),
            assurance=str(ASSURANCE_PATH),
            format="text",
        )
        output = StringIO()
        with redirect_stdout(output):
            rc = _cmd_symbolic_report(self.graph, args)
        rendered = output.getvalue()

        self.assertEqual(rc, 0)
        self.assertNotIn(f"{C12_TARGET}  [MACHINE-CHECKED]", rendered)
        self.assertIn(f"{C14_TARGET}  [MACHINE-CHECKED]", rendered)
        self.assertIn("SMT counterexample query:", rendered)
        self.assertIn("Solver: UNSAT → ENTAILED", rendered)
        self.assertIn("Mutation sensitivity:", rendered)
        self.assertNotIn("C13_idempotency_key_is_temporally_unique", rendered)

    def test_symbolic_report_rejects_non_machine_checked_node(self):
        args = SimpleNamespace(
            node=["C13_idempotency_key_is_temporally_unique"],
            model=str(BRIDGE_PATH),
            assurance=str(ASSURANCE_PATH),
            format="text",
        )
        with self.assertRaises(SymbolicCompositionError):
            _cmd_symbolic_report(self.graph, args)

    def test_recorded_c14_machine_check_is_replayable_from_current_model(self):
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        result = verifier.check(C14_TARGET, require_trusted=True)
        entry = self.graph.doc["assurance"]["composition"][C14_TARGET]

        self.assertEqual(entry["status"], "machine_checked")
        self.assertEqual(entry["signature"], result.composition_signature)
        self.assertNotIn("artifact_refs", entry)
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")

        for premise in C14_PREMISES:
            replayed = verifier.check_with_premise_overrides(
                C14_TARGET, premise_formula_overrides={premise: {"bool": True}}
            )
            self.assertEqual(replayed.solver_result.status, "sat")
            self.assertEqual(replayed.verdict, "COUNTEREXAMPLE")



if __name__ == "__main__":
    unittest.main()
