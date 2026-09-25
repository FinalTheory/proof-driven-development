from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from harness.model import Graph
from harness.symbolic.formal_schema import FORMAL_SCHEMAS
from harness.symbolic.formal_validation import (
    _load_yaml_mapping,
    validate_formal_layer,
)


ROOT = Path(__file__).resolve().parent


class FormalSchemaTests(unittest.TestCase):
    def test_all_formal_schemas_are_valid_draft_2020_12(self) -> None:
        for name, schema in FORMAL_SCHEMAS.items():
            with self.subTest(name=name):
                Draft202012Validator.check_schema(schema)

    def test_current_persisted_formal_yaml_matches_closed_schemas(self) -> None:
        cases = {
            "bridge": ROOT / "symbolic_models" / "bridge.yaml",
            "assurance": ROOT / "symbolic_models" / "assurance.yaml",
            "coverage": ROOT / "symbolic_models" / "coverage.yaml",
            "composition-artifact": ROOT
            / "symbolic_artifacts"
            / "C12_live_delivery_contains_only_accepted_changes.composition.yaml",
            "coverage-artifact": ROOT
            / "symbolic_artifacts"
            / "L108_acceptance_preserves_request_identity.coverage.yaml",
        }
        for name, path in cases.items():
            with self.subTest(name=name):
                _load_yaml_mapping(path, schema_name=name)

    def test_bridge_schema_rejects_unrecognized_function_metadata(self) -> None:
        raw = yaml.safe_load((ROOT / "symbolic_models" / "bridge.yaml").read_text())
        broken = copy.deepcopy(raw)
        function = next(iter(broken["functions"].values()))
        function["research_note"] = "must not become hidden persisted authority"
        errors = list(
            Draft202012Validator(FORMAL_SCHEMAS["bridge"]).iter_errors(broken)
        )
        self.assertTrue(errors)

    def test_composition_artifact_schema_rejects_non_entailed_result(self) -> None:
        path = (
            ROOT
            / "symbolic_artifacts"
            / "C12_live_delivery_contains_only_accepted_changes.composition.yaml"
        )
        raw = yaml.safe_load(path.read_text())
        broken = copy.deepcopy(raw)
        broken["result"]["solver_status"] = "sat"
        broken["result"]["verdict"] = "COUNTEREXAMPLE"
        errors = list(
            Draft202012Validator(
                FORMAL_SCHEMAS["composition-artifact"]
            ).iter_errors(broken)
        )
        self.assertTrue(errors)

    def test_coverage_artifact_schema_requires_non_vacuous_sat_to_unsat_proof(self) -> None:
        path = (
            ROOT
            / "symbolic_artifacts"
            / "L108_acceptance_preserves_request_identity.coverage.yaml"
        )
        raw = yaml.safe_load(path.read_text())
        broken = copy.deepcopy(raw)
        broken["proof"]["baseline"]["solver_status"] = "unsat"
        errors = list(
            Draft202012Validator(FORMAL_SCHEMAS["coverage-artifact"]).iter_errors(
                broken
            )
        )
        self.assertTrue(errors)

    def test_formal_preflight_ignores_non_symbolic_machine_artifact_refs(self) -> None:
        graph = Graph.load()
        doc = copy.deepcopy(graph.doc)
        doc["assurance"]["composition"][
            "C12_live_delivery_contains_only_accepted_changes"
        ]["artifact_refs"].append("ci://independent-model-checker/result")
        mixed = Graph(doc, repository_root=graph.repository_root)
        errors, warnings = validate_formal_layer(mixed)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_formal_preflight_is_reentrant_with_nonunique_sat_witness(self) -> None:
        graph = Graph.load()
        first_errors, first_warnings = validate_formal_layer(graph)
        second_errors, second_warnings = validate_formal_layer(graph)
        self.assertEqual(first_errors, [])
        self.assertEqual(first_warnings, [])
        self.assertEqual(second_errors, [])
        self.assertEqual(second_warnings, [])

    def test_repository_preflight_reports_formal_schema_drift(self) -> None:
        graph = Graph.load()
        bridge_path = ROOT / "symbolic_models" / "bridge.yaml"
        raw = yaml.safe_load(bridge_path.read_text())
        raw["functions"][next(iter(raw["functions"]))]["research_note"] = "illegal"

        with tempfile.TemporaryDirectory() as temp_dir:
            repository_root = Path(temp_dir)
            correctness_root = repository_root / "correctness"
            models = correctness_root / "symbolic_models"
            models.mkdir(parents=True)
            for name in ("assurance.yaml", "coverage.yaml"):
                (models / name).write_text(
                    (ROOT / "symbolic_models" / name).read_text(),
                    encoding="utf-8",
                )
            (models / "bridge.yaml").write_text(
                yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            synthetic = Graph(copy.deepcopy(graph.doc), repository_root=repository_root)
            errors, _ = validate_formal_layer(synthetic)
        self.assertTrue(any("invalid bridge schema" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
