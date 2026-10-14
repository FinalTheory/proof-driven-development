#!/usr/bin/env python3

import copy
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import yaml

from harness.model import Graph, UniqueKeyLoader
from harness.symbolic import (
    SymbolicBridge,
    SymbolicCompositionError,
    SymbolicCompositionVerifier,
    CheckResult,
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


def and_(*values):
    return {"and": list(values)}


def or_(*values):
    return {"or": list(values)}


def transitive_closure(fn, left, right):
    return {
        "transitive_closure": {
            "fn": fn,
            "from": left,
            "to": right,
        }
    }


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

    def _transitive_invariant_fixture(self, *, invalid_target: bool = False):
        taxonomy = "L990_synthetic_lifecycle_taxonomy"
        step_a = "L991_synthetic_lifecycle_step_a"
        step_b = "L992_synthetic_lifecycle_step_b"
        target = "C990_synthetic_finite_lifecycle_invariant"
        claim_ids = [taxonomy, step_a, step_b, target]

        graph_doc = copy.deepcopy(self.graph.doc)
        graph_doc["claims"][taxonomy] = {
            "kind": "leaf",
            "statement": "Synthetic lifecycle categories are exhaustive.",
            "semantic_contracts": [],
        }
        graph_doc["claims"][step_a] = {
            "kind": "leaf",
            "statement": "Synthetic category A preserves the invariant in one step.",
            "semantic_contracts": [],
        }
        graph_doc["claims"][step_b] = {
            "kind": "leaf",
            "statement": "Synthetic category B preserves the invariant in one step.",
            "semantic_contracts": [],
        }
        graph_doc["claims"][target] = {
            "kind": "derived",
            "statement": "Synthetic finite lifecycle paths preserve the invariant.",
            "semantic_contracts": [],
            "depends_on": [taxonomy, step_a, step_b],
        }
        graph = Graph(graph_doc)

        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        bridge_raw = copy.deepcopy(raw)
        anchors = [f"claim:{claim_id}" for claim_id in claim_ids]
        for fn, args, meaning in (
            (
                "synthetic_lifecycle_edge",
                ["TerminalResultRecord", "TerminalResultRecord"],
                "One synthetic authoritative lifecycle edge.",
            ),
            (
                "synthetic_lifecycle_category_a",
                ["TerminalResultRecord", "TerminalResultRecord"],
                "The synthetic lifecycle edge belongs to category A.",
            ),
            (
                "synthetic_lifecycle_category_b",
                ["TerminalResultRecord", "TerminalResultRecord"],
                "The synthetic lifecycle edge belongs to category B.",
            ),
            (
                "synthetic_lifecycle_invariant",
                ["TerminalResultRecord"],
                "The synthetic invariant holds at this representation.",
            ),
            (
                "synthetic_other_property",
                ["TerminalResultRecord"],
                "A different synthetic property used to test fail-closed lifting.",
            ),
        ):
            bridge_raw["functions"][fn] = {
                "args": args,
                "returns": "Bool",
                "catalog_symbols": [],
                "meaning": meaning,
                "semantic_anchors": anchors,
            }

        taxonomy_formula = forall(
            {"x": "TerminalResultRecord", "y": "TerminalResultRecord"},
            implies(
                call("synthetic_lifecycle_edge", var("x"), var("y")),
                or_(
                    call("synthetic_lifecycle_category_a", var("x"), var("y")),
                    call("synthetic_lifecycle_category_b", var("x"), var("y")),
                ),
            ),
        )

        def category_step(category):
            return forall(
                {"x": "TerminalResultRecord", "y": "TerminalResultRecord"},
                implies(
                    and_(
                        call(category, var("x"), var("y")),
                        call("synthetic_lifecycle_invariant", var("x")),
                    ),
                    and_(
                        call("synthetic_lifecycle_edge", var("x"), var("y")),
                        call("synthetic_lifecycle_invariant", var("y")),
                    ),
                ),
            )

        finite_path_conclusion = (
            call("synthetic_other_property", var("y"))
            if invalid_target
            else call("synthetic_lifecycle_invariant", var("y"))
        )
        finite_path_formula = forall(
            {"x": "TerminalResultRecord", "y": "TerminalResultRecord"},
            implies(
                and_(
                    transitive_closure(
                        "synthetic_lifecycle_edge", var("x"), var("y")
                    ),
                    call("synthetic_lifecycle_invariant", var("x")),
                ),
                finite_path_conclusion,
            ),
        )

        bridge_raw["claims"][taxonomy] = {
            "contracts": [],
            "formula": taxonomy_formula,
        }
        bridge_raw["claims"][step_a] = {
            "contracts": [],
            "formula": category_step("synthetic_lifecycle_category_a"),
        }
        bridge_raw["claims"][step_b] = {
            "contracts": [],
            "formula": category_step("synthetic_lifecycle_category_b"),
        }
        bridge_raw["claims"][target] = {
            "contracts": [],
            "formula": and_(taxonomy_formula, finite_path_formula),
        }
        bridge = SymbolicBridge.from_data(bridge_raw)
        bridge.validate_against_graph(graph)
        return graph, bridge, target, (taxonomy, step_a, step_b)

    def test_transitive_invariant_composition_lifts_one_step_proof(self):
        graph, bridge, target, premises = self._transitive_invariant_fixture()
        verifier = SymbolicCompositionVerifier(graph, bridge)
        result = verifier.check(target, require_trusted=False)
        self.assertEqual(result.premise_ids, premises)
        self.assertEqual(result.proof_rule, "finite_transitive_invariant_lift")
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")

    def test_transitive_invariant_lift_is_sensitive_to_each_proof_premise(self):
        graph, bridge, target, premises = self._transitive_invariant_fixture()
        verifier = SymbolicCompositionVerifier(graph, bridge)
        for premise in premises:
            with self.subTest(premise=premise):
                result = verifier.check_with_premise_overrides(
                    target,
                    premise_formula_overrides={premise: {"bool": True}},
                )
                expected_rule = (
                    "finite_transitive_invariant_lift_then_smt"
                    if premise == premises[0]
                    else "finite_transitive_invariant_lift"
                )
                self.assertEqual(result.proof_rule, expected_rule)
                self.assertEqual(result.solver_result.status, "sat")
                self.assertEqual(result.verdict, "COUNTEREXAMPLE")

    def _two_phase_transitive_lemma_fixture(self):
        base = "L993_synthetic_origin_base"
        step = "L994_synthetic_origin_step"
        origin = "L995_synthetic_origin_closure"
        target = "C991_synthetic_current_state_from_finite_path"
        claim_ids = [base, step, origin, target]

        graph_doc = copy.deepcopy(self.graph.doc)
        graph_doc["claims"][base] = {
            "kind": "leaf",
            "statement": "Synthetic origin states satisfy the invariant.",
            "semantic_contracts": [],
        }
        graph_doc["claims"][step] = {
            "kind": "leaf",
            "statement": "Synthetic lifecycle edges preserve the invariant one step at a time.",
            "semantic_contracts": [],
        }
        graph_doc["claims"][origin] = {
            "kind": "leaf",
            "statement": "Every current state is either an origin or a finite descendant of one.",
            "semantic_contracts": [],
        }
        graph_doc["claims"][target] = {
            "kind": "derived",
            "statement": "Finite descendants and therefore every current state satisfy the invariant.",
            "semantic_contracts": [],
            "depends_on": [base, step, origin],
        }
        graph = Graph(graph_doc)

        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        bridge_raw = copy.deepcopy(raw)
        anchors = [f"claim:{claim_id}" for claim_id in claim_ids]
        for fn, args, meaning in (
            (
                "synthetic_two_phase_edge",
                ["TerminalResultRecord", "TerminalResultRecord"],
                "One synthetic lifecycle edge.",
            ),
            (
                "synthetic_two_phase_invariant",
                ["TerminalResultRecord"],
                "The synthetic invariant holds at this representation.",
            ),
            (
                "synthetic_two_phase_origin",
                ["TerminalResultRecord"],
                "This representation is an authoritative origin.",
            ),
            (
                "synthetic_two_phase_current",
                ["TerminalResultRecord"],
                "This representation is a current authoritative state.",
            ),
        ):
            bridge_raw["functions"][fn] = {
                "args": args,
                "returns": "Bool",
                "catalog_symbols": [],
                "meaning": meaning,
                "semantic_anchors": anchors,
            }

        base_formula = forall(
            {"x": "TerminalResultRecord"},
            implies(
                call("synthetic_two_phase_origin", var("x")),
                call("synthetic_two_phase_invariant", var("x")),
            ),
        )
        step_formula = forall(
            {"x": "TerminalResultRecord", "y": "TerminalResultRecord"},
            implies(
                and_(
                    call("synthetic_two_phase_edge", var("x"), var("y")),
                    call("synthetic_two_phase_invariant", var("x")),
                ),
                call("synthetic_two_phase_invariant", var("y")),
            ),
        )
        origin_formula = forall(
            {"y": "TerminalResultRecord"},
            implies(
                call("synthetic_two_phase_current", var("y")),
                exists(
                    {"x": "TerminalResultRecord"},
                    and_(
                        call("synthetic_two_phase_origin", var("x")),
                        or_(
                            {"eq": [var("x"), var("y")]},
                            transitive_closure(
                                "synthetic_two_phase_edge", var("x"), var("y")
                            ),
                        ),
                    ),
                ),
            ),
        )
        finite_path_formula = forall(
            {"x": "TerminalResultRecord", "y": "TerminalResultRecord"},
            implies(
                and_(
                    transitive_closure(
                        "synthetic_two_phase_edge", var("x"), var("y")
                    ),
                    call("synthetic_two_phase_invariant", var("x")),
                ),
                call("synthetic_two_phase_invariant", var("y")),
            ),
        )
        current_formula = forall(
            {"y": "TerminalResultRecord"},
            implies(
                call("synthetic_two_phase_current", var("y")),
                call("synthetic_two_phase_invariant", var("y")),
            ),
        )

        bridge_raw["claims"][base] = {"contracts": [], "formula": base_formula}
        bridge_raw["claims"][step] = {"contracts": [], "formula": step_formula}
        bridge_raw["claims"][origin] = {"contracts": [], "formula": origin_formula}
        bridge_raw["claims"][target] = {
            "contracts": [],
            "formula": and_(finite_path_formula, current_formula),
        }
        bridge = SymbolicBridge.from_data(bridge_raw)
        bridge.validate_against_graph(graph)
        return graph, bridge, target, (base, step, origin)

    def test_transitive_lift_can_feed_remaining_target_clauses_as_derived_lemma(self):
        graph, bridge, target, premises = self._two_phase_transitive_lemma_fixture()
        verifier = SymbolicCompositionVerifier(graph, bridge)
        calls = []

        def fake_check(program):
            calls.append(program)
            if len(calls) == 1:
                self.assertEqual(len(program.constraints), len(premises))
                self.assertFalse(
                    any("TransitiveClosure" in repr(value) for value in program.constraints)
                )
                self.assertNotIn("TransitiveClosure", repr(program.query))
                self.assertIn("__composition_tc__synthetic_two_phase_edge", program.functions)
                return CheckResult("unsat", None)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(program.constraints), len(premises) + 1)
            self.assertFalse(
                any("TransitiveClosure" in repr(value) for value in program.constraints)
            )
            self.assertNotIn("TransitiveClosure", repr(program.query))
            abstract_relation = "__composition_tc__synthetic_two_phase_edge"
            self.assertIn(abstract_relation, program.functions)
            # The origin premise and phase-one-derived finite-path lemma must use
            # the same arbitrary relation in phase two. The derived lemma is the
            # final extra constraint and must not retain native transitive closure.
            self.assertIn(abstract_relation, repr(program.constraints[2]))
            self.assertIn(abstract_relation, repr(program.constraints[-1]))
            return CheckResult("unsat", None)

        with patch("harness.symbolic.composition.check_program", side_effect=fake_check):
            result = verifier.check(target, require_trusted=False)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result.premise_ids, premises)
        self.assertEqual(result.proof_rule, "finite_transitive_invariant_lift_then_smt")
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")

    def test_two_phase_closure_inclusion_lifts_explicit_edge_inclusion(self):
        taxonomy = "L996_synthetic_supported_edge_is_lifecycle_edge"
        base = "L997_synthetic_supported_origin_base"
        step = "L998_synthetic_lifecycle_preserves_invariant"
        origin = "L999_synthetic_supported_origin_closure"
        target = "C992_synthetic_cross_relation_finite_path_invariant"
        claim_ids = [taxonomy, base, step, origin, target]

        graph_doc = copy.deepcopy(self.graph.doc)
        for claim_id, kind, statement in (
            (taxonomy, "leaf", "Every supported edge is a lifecycle edge."),
            (base, "leaf", "Every origin satisfies the invariant."),
            (step, "leaf", "Every lifecycle edge preserves the invariant."),
            (origin, "leaf", "Every current state descends from an origin through supported edges."),
            (target, "derived", "Every current state satisfies the lifecycle invariant."),
        ):
            graph_doc["claims"][claim_id] = {
                "kind": kind,
                "statement": statement,
                "semantic_contracts": [],
            }
        graph_doc["claims"][target]["depends_on"] = [taxonomy, base, step, origin]
        graph = Graph(graph_doc)

        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        bridge_raw = copy.deepcopy(raw)
        anchors = [f"claim:{claim_id}" for claim_id in claim_ids]
        for fn, args in (
            ("synthetic_supported_edge", ["TerminalResultRecord", "TerminalResultRecord"]),
            ("synthetic_lifecycle_edge_for_inclusion", ["TerminalResultRecord", "TerminalResultRecord"]),
            ("synthetic_cross_relation_origin", ["TerminalResultRecord"]),
            ("synthetic_cross_relation_current", ["TerminalResultRecord"]),
            ("synthetic_cross_relation_invariant", ["TerminalResultRecord"]),
        ):
            bridge_raw["functions"][fn] = {
                "args": args,
                "returns": "Bool",
                "catalog_symbols": [],
                "meaning": f"Synthetic relation {fn} for closure-inclusion regression coverage.",
                "semantic_anchors": anchors,
            }

        taxonomy_formula = forall(
            {"x": "TerminalResultRecord", "y": "TerminalResultRecord"},
            implies(
                call("synthetic_supported_edge", var("x"), var("y")),
                call("synthetic_lifecycle_edge_for_inclusion", var("x"), var("y")),
            ),
        )
        base_formula = forall(
            {"x": "TerminalResultRecord"},
            implies(
                call("synthetic_cross_relation_origin", var("x")),
                call("synthetic_cross_relation_invariant", var("x")),
            ),
        )
        step_formula = forall(
            {"x": "TerminalResultRecord", "y": "TerminalResultRecord"},
            implies(
                and_(
                    call("synthetic_lifecycle_edge_for_inclusion", var("x"), var("y")),
                    call("synthetic_cross_relation_invariant", var("x")),
                ),
                call("synthetic_cross_relation_invariant", var("y")),
            ),
        )
        origin_formula = forall(
            {"y": "TerminalResultRecord"},
            implies(
                call("synthetic_cross_relation_current", var("y")),
                exists(
                    {"x": "TerminalResultRecord"},
                    and_(
                        call("synthetic_cross_relation_origin", var("x")),
                        or_(
                            {"eq": [var("x"), var("y")]},
                            transitive_closure(
                                "synthetic_supported_edge", var("x"), var("y")
                            ),
                        ),
                    ),
                ),
            ),
        )
        finite_path_formula = forall(
            {"x": "TerminalResultRecord", "y": "TerminalResultRecord"},
            implies(
                and_(
                    transitive_closure(
                        "synthetic_lifecycle_edge_for_inclusion", var("x"), var("y")
                    ),
                    call("synthetic_cross_relation_invariant", var("x")),
                ),
                call("synthetic_cross_relation_invariant", var("y")),
            ),
        )
        current_formula = forall(
            {"y": "TerminalResultRecord"},
            implies(
                call("synthetic_cross_relation_current", var("y")),
                call("synthetic_cross_relation_invariant", var("y")),
            ),
        )
        for claim_id, formula in (
            (taxonomy, taxonomy_formula),
            (base, base_formula),
            (step, step_formula),
            (origin, origin_formula),
        ):
            bridge_raw["claims"][claim_id] = {"contracts": [], "formula": formula}
        bridge_raw["claims"][target] = {
            "contracts": [],
            "formula": and_(finite_path_formula, current_formula),
        }
        bridge = SymbolicBridge.from_data(bridge_raw)
        bridge.validate_against_graph(graph)
        verifier = SymbolicCompositionVerifier(graph, bridge)

        covered = verifier.check(target, require_trusted=False)
        self.assertEqual(covered.verdict, "ENTAILED")
        self.assertEqual(covered.solver_result.status, "unsat")

        without_inclusion = verifier.check_with_premise_overrides(
            target,
            premise_formula_overrides={taxonomy: {"bool": True}},
        )
        self.assertEqual(without_inclusion.verdict, "COUNTEREXAMPLE")
        self.assertEqual(without_inclusion.solver_result.status, "sat")

    def test_transitive_invariant_lift_rejects_non_invariant_target(self):
        graph, bridge, target, _ = self._transitive_invariant_fixture(
            invalid_target=True
        )
        verifier = SymbolicCompositionVerifier(graph, bridge)
        with self.assertRaises(SymbolicCompositionError) as ctx:
            verifier.check(target, require_trusted=False)
        self.assertIn("conclusion must be the source invariant", str(ctx.exception))

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

    def test_trusted_c12_composition_runs_after_fresh_translation_review(self):
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        result = verifier.check(C12_TARGET, require_trusted=True)
        self.assertEqual(result.premise_ids, (C12_PREMISE,))
        self.assertEqual(result.solver_result.status, "unsat")
        self.assertEqual(result.verdict, "ENTAILED")
        self.assertTrue(result.machine_checked)

    def test_current_c12_machine_check_is_recorded(self):
        entry = self.graph.doc["assurance"]["composition"][C12_TARGET]
        registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        verifier = SymbolicCompositionVerifier(self.graph, self.bridge, registry)
        result = verifier.check(C12_TARGET, require_trusted=True)
        self.assertEqual(entry["status"], "machine_checked")
        self.assertEqual(entry["signature"], result.composition_signature)
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
        self.assertIn(f"{C12_TARGET}  [MACHINE-CHECKED]", rendered)
        self.assertIn(f"{C14_TARGET}  [MACHINE-CHECKED]", rendered)
        self.assertIn("C13_idempotency_key_is_temporally_unique  [MACHINE-CHECKED]", rendered)
        self.assertIn("SMT counterexample query:", rendered)
        self.assertIn("Solver: UNSAT → ENTAILED", rendered)
        self.assertIn("Mutation sensitivity:", rendered)
        self.assertNotIn("C10_catchup_represents_exact_missing_suffix", rendered)

    def test_symbolic_report_rejects_non_machine_checked_node(self):
        args = SimpleNamespace(
            node=["C10_catchup_represents_exact_missing_suffix"],
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
