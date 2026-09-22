#!/usr/bin/env python3

import copy
import unittest
from pathlib import Path

import yaml

from harness.model import Graph, UniqueKeyLoader
from harness.symbolic import SymbolicBridge, SymbolicBridgeError, check_program


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


if __name__ == "__main__":
    unittest.main()
