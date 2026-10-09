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


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "symbolic_models" / "bridge.yaml"
ASSURANCE_PATH = Path(__file__).resolve().parents[1] / "symbolic_models" / "assurance.yaml"
COVERAGE_PATH = Path(__file__).resolve().parents[1] / "symbolic_models" / "coverage.yaml"
TARGET = "L108_acceptance_preserves_request_identity"

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

    def test_generated_coverage_artifact_is_current_and_has_no_source_path(self):
        verifier = SymbolicCoverageVerifier(
            self.graph,
            self.bridge,
            self.registry,
        )
        result = verifier.check(self.case, require_trusted=True)
        artifact = build_coverage_artifact(
            self.graph, self.bridge, self.registry, self.case, result
        )

        self.assertEqual(artifact["coverage_signature"], result.coverage_signature)
        self.assertNotIn("artifact_ref", artifact)
        self.assertEqual(artifact["candidate"]["signature"], result.candidate_signature)
        self.assertEqual(artifact["candidate"]["formula"], self.case.formula)
        self.assertEqual(artifact["proof"]["baseline"]["solver_status"], "sat")
        self.assertEqual(artifact["proof"]["constrained"]["solver_status"], "unsat")
        self.assertTrue(artifact["result"]["machine_checked"])

    def test_write_artifact_goes_only_to_ignored_temp(self):
        repository_root = Path(__file__).resolve().parents[2]
        artifact_path = repository_root / "temp" / "symbolic_artifacts" / f"{TARGET}.coverage.yaml"
        artifact_path.unlink(missing_ok=True)
        args = SimpleNamespace(
            case=TARGET,
            coverage=str(COVERAGE_PATH),
            model=str(BRIDGE_PATH),
            assurance=str(ASSURANCE_PATH),
            allow_untrusted=False,
            write_artifact=True,
            format="text",
        )
        output = StringIO()
        try:
            with redirect_stdout(output):
                rc = _cmd_symbolic_cover(self.graph, args)
            self.assertEqual(rc, 0)
            self.assertTrue(artifact_path.exists())
            self.assertIn("artifact=temp/symbolic_artifacts/", output.getvalue())
        finally:
            artifact_path.unlink(missing_ok=True)

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
