#!/usr/bin/env python3

import copy
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import yaml

from harness.model import Graph
from harness.symbolic import (
    CoverageSpec,
    SymbolicBridge,
    SymbolicCoverageVerifier,
    TranslationAssuranceRegistry,
    build_coverage_artifact,
)
from harness.symbolic.cli import _cmd_symbolic_cover


BRIDGE_PATH = Path(__file__).with_name("symbolic_models") / "google_docs.poc.yaml"
ASSURANCE_PATH = Path(__file__).with_name("symbolic_models") / "assurance.poc.yaml"
COVERAGE_PATH = Path(__file__).with_name("symbolic_models") / "coverage.poc.yaml"
TARGET = "L108_acceptance_preserves_request_identity"
ARTIFACT_PATH = (
    Path(__file__).with_name("symbolic_artifacts")
    / "L108_acceptance_preserves_request_identity.coverage.yaml"
)


class SymbolicCoverageTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph.load()
        self.bridge = SymbolicBridge.load(BRIDGE_PATH)
        self.registry = TranslationAssuranceRegistry.load(ASSURANCE_PATH)
        self.spec = CoverageSpec.load(COVERAGE_PATH)
        self.case = self.spec.require_case(TARGET)

    def test_l108_identity_cross_binding_is_non_vacuous_and_excluded(self):
        verifier = SymbolicCoverageVerifier(
            self.graph,
            self.bridge,
            self.registry,
        )
        result = verifier.check(self.case, require_trusted=True)

        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertEqual(result.verdict, "EXCLUDED")
        self.assertTrue(result.closes_counterexample)
        self.assertTrue(result.machine_checked)

    def test_weakening_l108_document_binding_admits_the_bad_state(self):
        weakened = copy.deepcopy(self.bridge.claims[TARGET].formula)
        obligations = weakened["forall"]["body"]["implies"][1]["and"]
        del obligations[0]

        verifier = SymbolicCoverageVerifier(self.graph, self.bridge, self.registry)
        result = verifier.check_with_overrides(
            self.case,
            claim_formula_overrides={TARGET: weakened},
        )

        self.assertEqual(result.baseline.status, "sat")
        self.assertEqual(result.constrained.status, "sat")
        self.assertEqual(result.verdict, "ADMITTED")
        self.assertFalse(result.machine_checked)
        self.assertIsNotNone(result.constrained.model)

    def test_unsat_candidate_is_rejected_as_vacuous(self):
        vacuous = replace(
            self.case,
            case_id="synthetic_vacuous_candidate",
            formula={"and": [{"bool": True}, {"bool": False}]},
            artifact_ref="symbolic_artifacts/synthetic_vacuous_candidate.coverage.yaml",
        )
        verifier = SymbolicCoverageVerifier(self.graph, self.bridge, self.registry)
        result = verifier.check(vacuous, require_trusted=True)

        self.assertEqual(result.baseline.status, "unsat")
        self.assertEqual(result.constrained.status, "unsat")
        self.assertEqual(result.verdict, "VACUOUS_CANDIDATE")
        self.assertFalse(result.closes_counterexample)
        self.assertFalse(result.machine_checked)

    def test_coverage_artifact_is_self_contained(self):
        verifier = SymbolicCoverageVerifier(
            self.graph,
            self.bridge,
            self.registry,
        )
        result = verifier.check(self.case, require_trusted=True)
        artifact = build_coverage_artifact(
            self.graph,
            self.bridge,
            self.registry,
            self.case,
            result,
        )

        self.assertEqual(artifact["case"], TARGET)
        self.assertEqual(artifact["artifact_ref"], self.case.artifact_ref)
        self.assertEqual(artifact["candidate"]["formula"], self.case.formula)
        self.assertIn(
            "acceptance_produced_from_request",
            artifact["candidate"]["vocabulary"],
        )
        self.assertEqual(
            artifact["trusted_constraints"]["claims"][0]["id"],
            TARGET,
        )
        self.assertEqual(
            artifact["trusted_constraints"]["claims"][0]["formula"],
            self.bridge.claims[TARGET].formula,
        )
        self.assertEqual(artifact["proof"]["baseline"]["solver_status"], "sat")
        self.assertEqual(artifact["proof"]["constrained"]["solver_status"], "unsat")
        self.assertEqual(artifact["result"]["verdict"], "EXCLUDED")

    def test_persisted_l108_coverage_artifact_is_current(self):
        artifact = yaml.safe_load(ARTIFACT_PATH.read_text(encoding="utf-8"))
        verifier = SymbolicCoverageVerifier(
            self.graph,
            self.bridge,
            self.registry,
        )
        result = verifier.check(self.case, require_trusted=True)

        self.assertEqual(artifact["coverage_signature"], result.coverage_signature)
        self.assertEqual(artifact["artifact_ref"], self.case.artifact_ref)
        self.assertEqual(
            artifact["candidate"]["signature"],
            result.candidate_signature,
        )
        self.assertEqual(artifact["candidate"]["formula"], self.case.formula)
        self.assertEqual(artifact["proof"]["baseline"]["solver_status"], "sat")
        self.assertEqual(artifact["proof"]["constrained"]["solver_status"], "unsat")
        self.assertTrue(artifact["result"]["machine_checked"])

    def test_symbolic_cover_yaml_output_contains_full_proof(self):
        args = SimpleNamespace(
            case=TARGET,
            coverage=str(COVERAGE_PATH),
            model=str(BRIDGE_PATH),
            assurance=str(ASSURANCE_PATH),
            allow_untrusted=False,
            write_artifact=False,
            format="yaml",
        )
        output = StringIO()
        with redirect_stdout(output):
            rc = _cmd_symbolic_cover(self.graph, args)

        rendered = output.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("candidate:", rendered)
        self.assertIn("trusted_constraints:", rendered)
        self.assertIn("proof:", rendered)
        self.assertIn("solver_status: sat", rendered)
        self.assertIn("solver_status: unsat", rendered)


if __name__ == "__main__":
    unittest.main()
