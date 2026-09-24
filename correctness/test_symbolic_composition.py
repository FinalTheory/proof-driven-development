#!/usr/bin/env python3

import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import yaml

from harness.model import Graph
from harness.symbolic import (
    SymbolicBridge,
    SymbolicCompositionError,
    SymbolicCompositionVerifier,
    TranslationAssuranceRegistry,
    build_claim_translation_subject,
    translation_subject_signature,
)
from harness.symbolic.cli import _cmd_symbolic_report


BRIDGE_PATH = Path(__file__).with_name("symbolic_models") / "google_docs.poc.yaml"
ASSURANCE_PATH = Path(__file__).with_name("symbolic_models") / "assurance.poc.yaml"

C12_TARGET = "C12_live_delivery_contains_only_accepted_changes"
C12_PREMISE = "L116_live_delivery_is_bound_to_committed_acceptance"
C12_ARTIFACT_PATH = (
    Path(__file__).with_name("symbolic_artifacts")
    / "C12_live_delivery_contains_only_accepted_changes.composition.yaml"
)

C14_TARGET = "C14_all_acceptance_paths_honor_idempotency_identity"
C14_PREMISES = (
    "L20_acceptance_paths_are_authoritatively_mediated",
    "L21_accepted_key_gate_rejects_new_acceptance",
)
C14_ARTIFACT_PATH = (
    Path(__file__).with_name("symbolic_artifacts")
    / "C14_all_acceptance_paths_honor_idempotency_identity.composition.yaml"
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

    def test_trusted_c12_composition_succeeds(self):
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        result = verifier.check(C12_TARGET, require_trusted=True)
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")
        self.assertTrue(result.machine_checked)

    def test_persisted_c12_machine_check_artifact_is_current(self):
        artifact = yaml.safe_load(C12_ARTIFACT_PATH.read_text())
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        result = verifier.check(C12_TARGET, require_trusted=True)

        target_subject = build_claim_translation_subject(
            self.graph, self.bridge, C12_TARGET
        )
        premise_subject = build_claim_translation_subject(
            self.graph, self.bridge, C12_PREMISE
        )

        self.assertEqual(
            artifact["composition_signature"],
            result.composition_signature,
        )
        self.assertEqual(
            artifact["translation_assurance"]["target"]["subject_signature"],
            translation_subject_signature(target_subject),
        )
        self.assertEqual(
            artifact["translation_assurance"]["premises"][C12_PREMISE]["subject_signature"],
            translation_subject_signature(premise_subject),
        )
        self.assertEqual(artifact["result"]["solver_status"], "unsat")
        self.assertEqual(artifact["result"]["verdict"], "ENTAILED")
        self.assertEqual(artifact["mutation_test"]["solver_status"], "sat")
        self.assertEqual(artifact["mutation_test"]["verdict"], "COUNTEREXAMPLE")

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
        self.assertIn(f"{C12_TARGET}  [MACHINE-CHECKED]", rendered)
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

    def test_persisted_c14_machine_check_artifact_is_current(self):
        artifact = yaml.safe_load(C14_ARTIFACT_PATH.read_text())
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        result = verifier.check(C14_TARGET, require_trusted=True)

        target_subject = build_claim_translation_subject(
            self.graph, self.bridge, C14_TARGET
        )
        premise_subject_signatures = {
            premise: translation_subject_signature(
                build_claim_translation_subject(self.graph, self.bridge, premise)
            )
            for premise in C14_PREMISES
        }

        self.assertEqual(
            artifact["composition_signature"],
            result.composition_signature,
        )
        self.assertEqual(
            artifact["translation_assurance"]["target"]["subject_signature"],
            translation_subject_signature(target_subject),
        )
        for premise in C14_PREMISES:
            self.assertEqual(
                artifact["translation_assurance"]["premises"][premise]["subject_signature"],
                premise_subject_signatures[premise],
            )

        self.assertEqual(artifact["result"]["solver_status"], "unsat")
        self.assertEqual(artifact["result"]["verdict"], "ENTAILED")

        mutation_by_premise = {
            item["weakened_premise"]: item
            for item in artifact["mutation_tests"]
        }
        self.assertEqual(set(mutation_by_premise), set(C14_PREMISES))
        for premise in C14_PREMISES:
            self.assertEqual(
                mutation_by_premise[premise]["solver_status"],
                "sat",
            )
            self.assertEqual(
                mutation_by_premise[premise]["verdict"],
                "COUNTEREXAMPLE",
            )


if __name__ == "__main__":
    unittest.main()
