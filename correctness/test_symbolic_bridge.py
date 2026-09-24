#!/usr/bin/env python3

import copy
import unittest
from pathlib import Path

import yaml

from harness.model import Graph, UniqueKeyLoader
from harness.symbolic import SymbolicBridge, SymbolicBridgeError, SymbolicVerifier, check_program


BRIDGE_PATH = Path(__file__).with_name("symbolic_models") / "google_docs.poc.yaml"


def var(name: str):
    return {"var": name}


def call(fn: str, *args):
    return {"call": {"fn": fn, "args": list(args)}}


def eq(left, right):
    return {"eq": [left, right]}


def neq(left, right):
    return {"neq": [left, right]}


def not_(value):
    return {"not": value}


def and_(*values):
    return {"and": list(values)}


def or_(*values):
    return {"or": list(values)}


def implies(left, right):
    return {"implies": [left, right]}


def forall(vars_: dict[str, str], body):
    return {"forall": {"vars": vars_, "body": body}}


def exists(vars_: dict[str, str], body):
    return {"exists": {"vars": vars_, "body": body}}


class SymbolicBridgeTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph.load()
        self.bridge = SymbolicBridge.load(BRIDGE_PATH)
        self.verifier = SymbolicVerifier(self.graph, self.bridge)

    def test_real_contract_mappings_match_canonical_contract_symbols(self):
        self.bridge.validate_against_graph(self.graph)
        program = self.bridge.compile_program(
            self.graph,
            contract_ids=[
                "client_canonical_frontier_coherence",
                "visible_canonical_speculative_exclusion",
                "request_authoring_frontier_binding",
            ],
            query={"bool": True},
        )
        self.assertEqual(len(program.constraints), 3)

    def test_bridge_rejects_symbol_coverage_drift(self):
        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        broken = copy.deepcopy(raw)
        broken["functions"]["request_raw_edit_bound_to_source_content"][
            "catalog_symbols"
        ] = ["client_local_document"]
        bridge = SymbolicBridge.from_data(broken)
        with self.assertRaises(SymbolicBridgeError) as ctx:
            bridge.validate_against_graph(self.graph)
        self.assertIn("missing=['raw_edit']", str(ctx.exception))

    def test_bridge_requires_explicit_predicate_meaning(self):
        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        broken = copy.deepcopy(raw)
        del broken["functions"]["visible_document"]["meaning"]
        with self.assertRaises(SymbolicBridgeError) as ctx:
            SymbolicBridge.from_data(broken)
        self.assertIn("meaning", str(ctx.exception))

    def test_claim_mapping_cannot_anonymize_explicit_canonical_vocabulary(self):
        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        broken = copy.deepcopy(raw)
        broken["claims"]["L5_append_requires_current_epoch"] = {
            "contracts": [],
            "formula": {"bool": True},
        }
        bridge = SymbolicBridge.from_data(broken)
        with self.assertRaises(SymbolicBridgeError) as ctx:
            bridge.validate_against_graph(self.graph)
        message = str(ctx.exception)
        self.assertIn("symbolic claim catalog coverage mismatch", message)
        self.assertIn("document_current_epoch", message)
        self.assertIn("owner_epoch", message)

    def test_bridge_rejects_dimension_collapse_for_canonical_state(self):
        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        broken = copy.deepcopy(raw)
        broken["sorts"].append("Epoch")
        broken["functions"]["collapsed_current_epoch"] = {
            "args": ["Document"],
            "returns": "Epoch",
            "catalog_symbols": ["document_current_epoch"],
            "meaning": "Reads the authoritative current epoch for a document.",
        }
        bridge = SymbolicBridge.from_data(broken)
        with self.assertRaises(SymbolicBridgeError) as ctx:
            bridge.validate_against_graph(self.graph)
        self.assertIn(
            "expected=['document', 'observation'], actual=None",
            str(ctx.exception),
        )

    def test_bridge_rejects_dimension_labels_that_do_not_match_function_arity(self):
        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        broken = copy.deepcopy(raw)
        broken["sorts"].append("Epoch")
        broken["functions"]["collapsed_current_epoch"] = {
            "args": ["Document"],
            "returns": "Epoch",
            "catalog_symbols": ["document_current_epoch"],
            "meaning": "Reads the authoritative current epoch for a document.",
            "dimensions": ["document", "observation"],
        }
        with self.assertRaises(SymbolicBridgeError) as ctx:
            SymbolicBridge.from_data(broken)
        self.assertIn("dimensions must align 1:1 with args", str(ctx.exception))

    def test_bridge_accepts_explicit_document_observation_dimensions(self):
        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        extended = copy.deepcopy(raw)
        extended["sorts"].append("Epoch")
        extended["functions"]["observed_current_epoch"] = {
            "args": ["Document", "AuthoritativeState"],
            "returns": "Epoch",
            "catalog_symbols": ["document_current_epoch"],
            "meaning": "Reads document_current_epoch at one explicit authoritative observation.",
            "dimensions": ["document", "observation"],
        }
        bridge = SymbolicBridge.from_data(extended)
        bridge.validate_against_graph(self.graph)

    def test_bridge_rejects_claim_contract_coverage_drift(self):
        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        broken = copy.deepcopy(raw)
        broken["claims"]["L108_acceptance_preserves_request_identity"]["contracts"] = [
            "live_delivery_acceptance_binding"
        ]
        bridge = SymbolicBridge.from_data(broken)
        with self.assertRaises(SymbolicBridgeError) as ctx:
            bridge.validate_against_graph(self.graph)
        self.assertIn("symbolic claim-contract coverage mismatch", str(ctx.exception))

    def test_bridge_rejects_observation_scope_drift(self):
        raw = yaml.load(BRIDGE_PATH.read_text(), Loader=UniqueKeyLoader)
        broken = copy.deepcopy(raw)
        broken["contracts"]["client_canonical_frontier_coherence"][
            "observation_scope"
        ] = "all_reachable_states"
        bridge = SymbolicBridge.from_data(broken)
        with self.assertRaises(SymbolicBridgeError) as ctx:
            bridge.validate_against_graph(self.graph)
        self.assertIn("observation_scope mismatch", str(ctx.exception))

    def test_canonical_client_coherence_excludes_wrong_visible_base(self):
        bad_state = exists(
            {"s": "ClientState"},
            not_(
                call(
                    "canonical_content_matches",
                    var("s"),
                    call("visible_document", var("s")),
                    call("visible_frontier", var("s")),
                )
            ),
        )
        without_contract = self.bridge.compile_program(
            self.graph,
            contract_ids=[],
            query=bad_state,
        )
        with_contract = self.bridge.compile_program(
            self.graph,
            contract_ids=["client_canonical_frontier_coherence"],
            query=bad_state,
        )
        self.assertEqual(check_program(without_contract).status, "sat")
        self.assertEqual(check_program(with_contract).status, "unsat")

    def test_request_provenance_excludes_cross_document_request_binding(self):
        bad_request = exists(
            {"r": "Request", "s": "ClientState"},
            and_(
                call("authored_from", var("r"), var("s")),
                neq(
                    call("request_document", var("r")),
                    call("visible_document", var("s")),
                ),
            ),
        )
        without_contract = self.bridge.compile_program(
            self.graph,
            contract_ids=[],
            query=bad_request,
        )
        with_contract = self.bridge.compile_program(
            self.graph,
            contract_ids=["request_authoring_frontier_binding"],
            query=bad_request,
        )
        self.assertEqual(check_program(without_contract).status, "sat")
        self.assertEqual(check_program(with_contract).status, "unsat")

    def test_existing_observer_contracts_admit_cross_document_unresolved_overlay(self):
        bad_overlay = exists(
            {"s": "ClientState", "o": "Overlay"},
            and_(
                call("visible_overlay", var("s"), var("o")),
                not_(call("canonical_contains_same_edit", var("s"), var("o"))),
                neq(
                    call("overlay_document", var("o")),
                    call("visible_document", var("s")),
                ),
            ),
        )
        existing = self.bridge.compile_program(
            self.graph,
            contract_ids=[
                "client_canonical_frontier_coherence",
                "visible_canonical_speculative_exclusion",
            ],
            query=bad_overlay,
        )
        self.assertEqual(check_program(existing).status, "sat")

        candidate_repair = forall(
            {"s": "ClientState", "o": "Overlay"},
            implies(
                call("visible_overlay", var("s"), var("o")),
                eq(
                    call("overlay_document", var("o")),
                    call("visible_document", var("s")),
                ),
            ),
        )
        repaired = self.bridge.compile_program(
            self.graph,
            contract_ids=[
                "client_canonical_frontier_coherence",
                "visible_canonical_speculative_exclusion",
            ],
            query=bad_overlay,
            extra_constraints=[candidate_repair],
        )
        self.assertEqual(check_program(repaired).status, "unsat")

    def test_catchup_head_binding_has_stable_exclusion_interface(self):
        wrong_head = exists(
            {"r": "CatchupResponse"},
            and_(
                call("eligible_catchup_response", var("r")),
                neq(
                    call("catchup_response_head_value", var("r")),
                    call("captured_authoritative_head", var("r")),
                ),
            ),
        )
        result = self.verifier.exclusion_check(
            wrong_head,
            contracts=["catchup_head_binding"],
        )
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertTrue(result.closes_counterexample)

    def test_catchup_head_binding_preserves_head_only_for_eligible_responses(self):
        lost_head = exists(
            {"r": "CatchupResponse"},
            and_(
                call("eligible_catchup_response", var("r")),
                not_(call("catchup_head_preserved", var("r"))),
            ),
        )
        result = self.verifier.exclusion_check(
            lost_head,
            contracts=["catchup_head_binding"],
        )
        self.assertTrue(result.closes_counterexample)

        out_of_scope = exists(
            {"r": "CatchupResponse"},
            and_(
                not_(call("eligible_catchup_response", var("r"))),
                not_(call("catchup_head_preserved", var("r"))),
            ),
        )
        self.assertEqual(
            self.verifier.check(
                out_of_scope,
                contracts=["catchup_head_binding"],
            ).status,
            "sat",
        )

    def test_accepted_evidence_correspondence_has_stable_exclusion_interface(self):
        correspondence_violation = exists(
            {"s": "AuthoritativeState", "k": "IdempotencyKey"},
            and_(
                call("within_retry_reconnect_eligibility", var("s"), var("k")),
                neq(
                    call("accepted_evidence_for_key", var("s"), var("k")),
                    call(
                        "exactly_one_committed_acceptance_for_key",
                        var("s"),
                        var("k"),
                    ),
                ),
            ),
        )
        result = self.verifier.exclusion_check(
            correspondence_violation,
            contracts=["accepted_evidence_correspondence"],
        )
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertTrue(result.closes_counterexample)

    def test_accepted_evidence_correspondence_does_not_overreach_after_eligibility(self):
        out_of_scope_violation = exists(
            {"s": "AuthoritativeState", "k": "IdempotencyKey"},
            and_(
                not_(
                    call(
                        "within_retry_reconnect_eligibility",
                        var("s"),
                        var("k"),
                    )
                ),
                neq(
                    call("accepted_evidence_for_key", var("s"), var("k")),
                    call(
                        "exactly_one_committed_acceptance_for_key",
                        var("s"),
                        var("k"),
                    ),
                ),
            ),
        )
        self.assertEqual(
            self.verifier.check(
                out_of_scope_violation,
                contracts=["accepted_evidence_correspondence"],
            ).status,
            "sat",
        )

    def test_accepted_evidence_provenance_has_stable_exclusion_interface(self):
        matching_committed_source = exists(
            {"a": "Acceptance"},
            and_(
                call("committed_canonical_acceptance", var("a")),
                eq(
                    call("evidence_key", var("e")),
                    call("acceptance_key", var("a")),
                ),
                call(
                    "evidence_derived_from_acceptance",
                    var("e"),
                    var("a"),
                ),
            ),
        )
        unsupported_evidence = exists(
            {"e": "AcceptedEvidence"},
            and_(
                call("authoritative_evidence", var("e")),
                not_(matching_committed_source),
            ),
        )
        result = self.verifier.exclusion_check(
            unsupported_evidence,
            contracts=["accepted_evidence_provenance"],
        )
        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertTrue(result.closes_counterexample)

    def test_acceptance_request_identity_binding_closes_cross_binding(self):
        bad_identity = exists(
            {"a": "Acceptance", "r": "Request"},
            and_(
                call("acceptance_produced_from_request", var("a"), var("r")),
                or_(
                    neq(
                        call("acceptance_document", var("a")),
                        call("request_document", var("r")),
                    ),
                    neq(
                        call("acceptance_change_id", var("a")),
                        call("request_change_id", var("r")),
                    ),
                    not_(
                        call(
                            "acceptance_idempotency_identity_matches_request",
                            var("a"),
                            var("r"),
                        )
                    ),
                ),
            ),
        )
        result = self.verifier.exclusion_check(
            bad_identity,
            contracts=["acceptance_request_identity_binding"],
        )
        self.assertTrue(result.closes_counterexample)

    def test_live_delivery_acceptance_binding_requires_committed_source(self):
        missing_source = exists(
            {"d": "LiveDelivery"},
            and_(
                call("live_delivery_record", var("d")),
                not_(
                    exists(
                        {"a": "Acceptance"},
                        and_(
                            call("committed_history_record", var("a")),
                            call("live_delivery_originates_from", var("d"), var("a")),
                            call("live_delivery_preserves_payload_revision", var("d"), var("a")),
                        ),
                    )
                ),
            ),
        )
        result = self.verifier.exclusion_check(
            missing_source,
            contracts=["live_delivery_acceptance_binding"],
        )
        self.assertTrue(result.closes_counterexample)

    def test_recovery_head_binding_closes_capture_or_preservation_violation(self):
        bad_recovery = exists(
            {"r": "RecoveryAttempt"},
            and_(
                call("recovery_head_capture_event", var("r")),
                or_(
                    neq(
                        call("recovery_head_value", var("r")),
                        call("captured_recovery_authoritative_head", var("r")),
                    ),
                    not_(call("recovery_head_preserved", var("r"))),
                ),
            ),
        )
        result = self.verifier.exclusion_check(
            bad_recovery,
            contracts=["recovery_head_binding"],
        )
        self.assertTrue(result.closes_counterexample)

    def test_claim_L92_excludes_wrong_recovery_head_without_lifecycle_overreach(self):
        bad_head = exists(
            {"r": "RecoveryAttempt"},
            and_(
                call("recovery_head_capture_event", var("r")),
                neq(
                    call("recovery_head_value", var("r")),
                    call("captured_recovery_authoritative_head", var("r")),
                ),
            ),
        )
        result = self.verifier.claim_exclusion_check(
            bad_head,
            claims=["L92_captured_recovery_head_equals_authoritative_frontier"],
        )
        self.assertTrue(result.closes_counterexample)

        unrelated_lifecycle_violation = exists(
            {"r": "RecoveryAttempt"},
            and_(
                call("recovery_head_capture_event", var("r")),
                not_(call("recovery_head_preserved", var("r"))),
            ),
        )
        self.assertEqual(
            self.verifier.check_claims(
                unrelated_lifecycle_violation,
                claims=["L92_captured_recovery_head_equals_authoritative_frontier"],
            ).status,
            "sat",
        )

    def test_claim_L108_excludes_intermediate_identity_breakage(self):
        broken_path = exists(
            {"a": "Acceptance", "r": "Request"},
            and_(
                call("acceptance_produced_from_request", var("a"), var("r")),
                not_(
                    call(
                        "acceptance_identity_preserved_end_to_end",
                        var("a"),
                        var("r"),
                    )
                ),
            ),
        )
        result = self.verifier.claim_exclusion_check(
            broken_path,
            claims=["L108_acceptance_preserves_request_identity"],
        )
        self.assertTrue(result.closes_counterexample)

    def test_claim_L116_excludes_live_delivery_without_committed_source(self):
        missing_source = exists(
            {"d": "LiveDelivery"},
            and_(
                call("live_delivery_record", var("d")),
                not_(
                    exists(
                        {"a": "Acceptance"},
                        and_(
                            call("committed_history_record", var("a")),
                            call("live_delivery_originates_from", var("d"), var("a")),
                            call("live_delivery_preserves_payload_revision", var("d"), var("a")),
                        ),
                    )
                ),
            ),
        )
        result = self.verifier.claim_exclusion_check(
            missing_source,
            claims=["L116_live_delivery_is_bound_to_committed_acceptance"],
        )
        self.assertTrue(result.closes_counterexample)


if __name__ == "__main__":
    unittest.main()
