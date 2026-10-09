from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from ..model import Graph, UniqueKeyLoader
from .assurance_registry import SubjectTrustStatus, TranslationAssuranceRegistry
from .bridge import SymbolicBridge
from .composition import SymbolicCompositionVerifier
from .coverage import CoverageSpec, SymbolicCoverageVerifier, build_coverage_artifact
from .obligations import DesignObligationSpec, DesignObligationVerifier
from .formal_schema import FORMAL_SCHEMAS
from .translation_assurance import (
    build_claim_translation_subject,
    build_assumption_translation_subject,
    translation_subject_signature,
)


def _load_yaml_mapping(path: Path, *, schema_name: str | None = None) -> dict[str, Any]:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except OSError as exc:
        raise ValueError(f"formal artifact unavailable at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"formal artifact must be a mapping: {path}")
    if schema_name is not None:
        validator = Draft202012Validator(FORMAL_SCHEMAS[schema_name])
        schema_errors = sorted(
            validator.iter_errors(raw),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if schema_errors:
            rendered = []
            for error in schema_errors[:8]:
                location = ".".join(str(part) for part in error.absolute_path) or "<root>"
                rendered.append(f"{location}: {error.message}")
            if len(schema_errors) > 8:
                rendered.append(f"... {len(schema_errors) - 8} more schema error(s)")
            raise ValueError(
                f"{path}: invalid {schema_name} schema: " + "; ".join(rendered)
            )
    return raw


def _coverage_artifact_matches_fresh_check(
    artifact: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    """Compare coverage evidence while treating a SAT witness as diagnostic.

    Z3 may return different satisfying models for the same baseline formula on
    successive checks in one process. Fresh validation therefore verifies the
    baseline SAT status and every semantic/signature field, but does not require
    the persisted witness text to match the particular model returned this run.
    The closed artifact schema still requires a non-empty persisted witness.
    """
    persisted = copy.deepcopy(artifact)
    fresh = copy.deepcopy(expected)
    for payload in (persisted, fresh):
        payload["proof"]["baseline"].pop("witness_model", None)
    return persisted == fresh


def _composition_artifact_errors(
    graph: Graph,
    bridge: SymbolicBridge,
    registry: TranslationAssuranceRegistry,
    node_id: str,
    artifact_ref: str,
    *,
    correctness_root: Path,
) -> list[str]:
    errors: list[str] = []
    path = correctness_root / artifact_ref
    try:
        artifact = _load_yaml_mapping(path, schema_name="composition-artifact")
    except ValueError as exc:
        return [str(exc)]

    common = {
        "version",
        "kind",
        "node",
        "direct_premises",
        "composition_signature",
        "translation_assurance",
        "query_semantics",
        "solver",
        "result",
    }
    mutation_keys = {"mutation_test", "mutation_tests"} & set(artifact)
    expected_top = common | mutation_keys
    if set(artifact) != expected_top or len(mutation_keys) != 1:
        errors.append(
            f"{artifact_ref}: composition artifact must contain exactly the canonical fields "
            "and exactly one of mutation_test/mutation_tests"
        )
        return errors

    verifier = SymbolicCompositionVerifier(graph, bridge, registry)
    try:
        result = verifier.check(node_id, require_trusted=True)
    except Exception as exc:
        return [f"{artifact_ref}: fresh symbolic composition check failed: {exc}"]

    expected_scalars = {
        "version": 1,
        "kind": "symbolic_composition_machine_check",
        "node": node_id,
        "direct_premises": list(result.premise_ids),
        "composition_signature": result.composition_signature,
        "query_semantics": "AND(all direct premise formulas) AND NOT(target formula)",
    }
    for field, expected in expected_scalars.items():
        if artifact.get(field) != expected:
            errors.append(
                f"{artifact_ref}: {field} is stale/invalid: "
                f"expected={expected!r} actual={artifact.get(field)!r}"
            )

    solver = artifact.get("solver")
    if not isinstance(solver, dict) or set(solver) != {"name", "version"} or solver.get("name") != "z3":
        errors.append(f"{artifact_ref}: solver must be exactly name/version for z3")

    expected_result = {
        "solver_status": result.solver_result.status,
        "verdict": result.verdict,
        "counterexample_model": (
            result.solver_result.model if result.solver_result.status == "sat" else None
        ),
    }
    if artifact.get("result") != expected_result:
        errors.append(f"{artifact_ref}: result does not match a fresh symbolic composition check")

    assurance = artifact.get("translation_assurance")
    if not isinstance(assurance, dict) or set(assurance) != {"target", "premises"}:
        errors.append(f"{artifact_ref}: translation_assurance must contain target and premises")
    else:
        target = assurance.get("target")
        target_trust = registry.evaluate(graph, bridge, kind="claim", subject_id=node_id)
        target_sig = translation_subject_signature(
            build_claim_translation_subject(graph, bridge, node_id)
        )
        expected_target = {
            "status": target_trust.status.value,
            "subject_signature": target_sig,
        }
        if target != expected_target or target_trust.status != SubjectTrustStatus.TRUSTED:
            errors.append(f"{artifact_ref}: target translation assurance is stale or untrusted")

        premises = assurance.get("premises")
        if not isinstance(premises, dict) or set(premises) != set(result.premise_ids):
            errors.append(f"{artifact_ref}: premise translation assurance set is stale")
        else:
            for premise_id in result.premise_ids:
                premise = graph.require_node(premise_id)
                premise_kind = "claim" if premise.node_type == "claim" else "assumption"
                trust = registry.evaluate(
                    graph, bridge, kind=premise_kind, subject_id=premise_id
                )
                premise_subject = (
                    build_claim_translation_subject(graph, bridge, premise_id)
                    if premise_kind == "claim"
                    else build_assumption_translation_subject(graph, bridge, premise_id)
                )
                sig = translation_subject_signature(premise_subject)
                expected = {
                    "status": trust.status.value,
                    "subject_signature": sig,
                }
                if premises.get(premise_id) != expected or trust.status != SubjectTrustStatus.TRUSTED:
                    errors.append(
                        f"{artifact_ref}: premise {premise_id} translation assurance is stale or untrusted"
                    )

    raw_mutations = (
        [artifact["mutation_test"]]
        if "mutation_test" in artifact
        else artifact.get("mutation_tests")
    )
    if not isinstance(raw_mutations, list) or not raw_mutations:
        errors.append(f"{artifact_ref}: at least one mutation sensitivity check is required")
    else:
        replayed_premises: set[str] = set()
        for index, mutation in enumerate(raw_mutations):
            if not isinstance(mutation, dict):
                errors.append(f"{artifact_ref}: mutation test {index} must be a mapping")
                continue
            weakened_premise = mutation.get("weakened_premise")
            if weakened_premise not in result.premise_ids:
                errors.append(
                    f"{artifact_ref}: mutation test {index} weakens non-direct premise "
                    f"{weakened_premise!r}"
                )
                continue
            if weakened_premise in replayed_premises:
                errors.append(
                    f"{artifact_ref}: mutation test {index} duplicates weakened premise "
                    f"{weakened_premise}"
                )
                continue
            replayed_premises.add(weakened_premise)
            if mutation.get("solver_status") != "sat" or mutation.get("verdict") != "COUNTEREXAMPLE":
                errors.append(
                    f"{artifact_ref}: mutation test {index} must produce SAT / COUNTEREXAMPLE"
                )
            if not isinstance(mutation.get("counterexample_model"), str) or not mutation[
                "counterexample_model"
            ].strip():
                errors.append(
                    f"{artifact_ref}: mutation test {index} must persist a counterexample model"
                )
            if mutation.get("operation") != "replace_formula":
                errors.append(
                    f"{artifact_ref}: mutation test {index} has unsupported operation"
                )
                continue
            override = mutation.get("premise_formula_override")
            if not isinstance(override, dict):
                errors.append(
                    f"{artifact_ref}: mutation test {index} must persist a formula override"
                )
                continue
            try:
                mutation_result = verifier.check_with_premise_overrides(
                    node_id,
                    premise_formula_overrides={weakened_premise: override},
                )
            except Exception as exc:
                errors.append(
                    f"{artifact_ref}: fresh mutation sensitivity check {index} failed: {exc}"
                )
                continue
            if (
                mutation_result.solver_result.status != "sat"
                or mutation_result.verdict != "COUNTEREXAMPLE"
            ):
                errors.append(
                    f"{artifact_ref}: fresh mutation sensitivity check {index} does not "
                    "reproduce SAT / COUNTEREXAMPLE"
                )
    return errors


def _formalization_bridge_errors(graph: Graph, bridge: SymbolicBridge) -> list[str]:
    errors: list[str] = []
    formalization = graph.doc.get("formalization", {})
    if not isinstance(formalization, dict):
        return errors
    namespaces = {
        "claims": set(bridge.claims),
        "assumptions": set(bridge.assumptions),
        "semantic_contracts": set(bridge.contracts),
    }
    for namespace, mapped_ids in namespaces.items():
        entries = formalization.get(namespace)
        if not isinstance(entries, dict):
            continue
        for subject_id, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("mode") == "non_symbolic" and subject_id in mapped_ids:
                errors.append(
                    f"formalization.{namespace}.{subject_id} is non_symbolic but still has a symbolic mapping"
                )
        for subject_id in sorted(mapped_ids - set(entries)):
            errors.append(
                f"symbolic mapping {namespace}.{subject_id} has no canonical formalization declaration"
            )
        for subject_id in sorted(mapped_ids & set(entries)):
            entry = entries.get(subject_id)
            if isinstance(entry, dict) and entry.get("mode") != "symbolic":
                errors.append(
                    f"symbolic mapping {namespace}.{subject_id} requires mode=symbolic"
                )
    return errors


def validate_formal_layer(graph: Graph) -> tuple[list[str], list[str]]:
    """Validate all persisted symbolic sidecars/artifacts as one formal assurance layer."""
    errors: list[str] = []
    warnings: list[str] = []

    if graph.repository_root is None:
        return errors, warnings
    correctness_root = graph.repository_root / "correctness"
    models_root = correctness_root / "symbolic_models"
    artifacts_root = correctness_root / "symbolic_artifacts"
    model_schemas = {
        "bridge.yaml": "bridge",
        "assurance.yaml": "assurance",
        "coverage.yaml": "coverage",
        "design_obligations.yaml": "design-obligations",
    }
    bridge_path = models_root / "bridge.yaml"
    assurance_path = models_root / "assurance.yaml"
    coverage_path = models_root / "coverage.yaml"
    obligations_path = models_root / "design_obligations.yaml"
    paths = (bridge_path, assurance_path, coverage_path, obligations_path)
    present = [path.exists() for path in paths]
    if not any(present):
        return errors, warnings
    if not all(present):
        missing = [
            str(path.relative_to(correctness_root))
            for path in paths
            if not path.exists()
        ]
        return [f"formal symbolic layer is incomplete; missing {missing}"], warnings

    # Persisted formal YAML is a closed namespace. Adding a new sidecar or artifact
    # requires registering a schema first; otherwise the repository would silently
    # acquire an unconstrained source of formal metadata.
    discovered_models = {
        path.name
        for path in models_root.iterdir()
        if path.is_file() and path.suffix in {".yaml", ".yml"}
    }
    unknown_models = sorted(discovered_models - set(model_schemas))
    if unknown_models:
        errors.append(
            "unregistered symbolic_models YAML files have no declared formal schema: "
            + ", ".join(unknown_models)
        )

    if artifacts_root.exists():
        for path in sorted(artifacts_root.iterdir()):
            if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
                continue
            if path.name.endswith(".composition.yaml"):
                schema_name = "composition-artifact"
            elif path.name.endswith(".coverage.yaml"):
                schema_name = "coverage-artifact"
            else:
                errors.append(
                    f"unregistered symbolic_artifacts YAML file has no declared formal schema: {path.name}"
                )
                continue
            try:
                _load_yaml_mapping(path, schema_name=schema_name)
            except Exception as exc:
                errors.append(f"{path.relative_to(correctness_root)}: {exc}")

    try:
        bridge_raw = _load_yaml_mapping(bridge_path, schema_name="bridge")
        assurance_raw = _load_yaml_mapping(assurance_path, schema_name="assurance")
        coverage_raw = _load_yaml_mapping(coverage_path, schema_name="coverage")
        obligations_raw = _load_yaml_mapping(obligations_path, schema_name="design-obligations")
        bridge = SymbolicBridge.from_data(bridge_raw)
        bridge.validate_against_graph(graph)
        registry = TranslationAssuranceRegistry.from_data(assurance_raw)
        coverage = CoverageSpec.from_data(coverage_raw)
        obligations = DesignObligationSpec.from_data(obligations_raw)
        errors.extend(_formalization_bridge_errors(graph, bridge))
    except Exception as exc:
        return [f"formal symbolic sidecar validation failed: {exc}"], warnings

    coverage_verifier = SymbolicCoverageVerifier(graph, bridge, registry)
    for case in coverage.cases.values():
        try:
            result = coverage_verifier.check(case, require_trusted=True)
            expected = build_coverage_artifact(
                graph, bridge, registry, case, result
            )
            artifact = _load_yaml_mapping(
                correctness_root / case.artifact_ref,
                schema_name="coverage-artifact",
            )
            if not _coverage_artifact_matches_fresh_check(artifact, expected):
                errors.append(
                    f"{case.artifact_ref}: persisted coverage artifact does not match a fresh trusted check"
                )
        except Exception as exc:
            errors.append(f"coverage case {case.case_id}: {exc}")

    # Preflight compiles every Design Obligation and replays its declared proof boundary
    # in exploratory mode so model/bridge drift fails closed without treating an intentional
    # GAP or incomplete translation assurance as repository corruption. Canonical coverage
    # certification is a separate trust gate and requires current TRUSTED translations.
    obligation_verifier = DesignObligationVerifier(graph, bridge, registry)
    for obligation in obligations.obligations.values():
        try:
            obligation_verifier.check(obligation, require_trusted=False)
        except Exception as exc:
            errors.append(f"design obligation {obligation.obligation_id}: {exc}")

    composition = graph.doc.get("assurance", {}).get("composition", {})
    if isinstance(composition, dict):
        for node_id, entry in composition.items():
            if not isinstance(entry, dict) or entry.get("status") != "machine_checked":
                continue
            refs = entry.get("artifact_refs", [])
            if not isinstance(refs, list) or not refs:
                errors.append(f"machine_checked composition {node_id} has no artifact_refs")
                continue
            for artifact_ref in refs:
                if not isinstance(artifact_ref, str):
                    errors.append(f"machine_checked composition {node_id} has invalid artifact_ref")
                    continue
                if not (
                    artifact_ref.startswith("symbolic_artifacts/")
                    and artifact_ref.endswith(".composition.yaml")
                ):
                    continue
                errors.extend(
                    _composition_artifact_errors(
                        graph,
                        bridge,
                        registry,
                        node_id,
                        artifact_ref,
                        correctness_root=correctness_root,
                    )
                )

    return errors, warnings
