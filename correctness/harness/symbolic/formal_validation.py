from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from ..model import Graph, UniqueKeyLoader
from .assurance_registry import TranslationAssuranceRegistry
from .bridge import SymbolicBridge
from .composition import SymbolicCompositionVerifier
from .coverage import CoverageSpec, SymbolicCoverageVerifier, build_coverage_artifact
from .obligations import DesignObligationSpec, DesignObligationVerifier
from .formal_schema import FORMAL_SCHEMAS


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
    coverage_artifact_validator = Draft202012Validator(FORMAL_SCHEMAS["coverage-artifact"])
    for case in coverage.cases.values():
        try:
            result = coverage_verifier.check(case, require_trusted=True)
            generated = build_coverage_artifact(graph, bridge, registry, case, result)
            schema_errors = list(coverage_artifact_validator.iter_errors(generated))
            if schema_errors:
                errors.append(
                    f"coverage case {case.case_id}: generated artifact violates coverage-artifact schema: "
                    + "; ".join(error.message for error in schema_errors[:4])
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
        composition_verifier = SymbolicCompositionVerifier(graph, bridge, registry)
        for node_id, entry in composition.items():
            if not isinstance(entry, dict) or entry.get("status") != "machine_checked":
                continue
            try:
                result = composition_verifier.check(node_id, require_trusted=True)
                if not result.machine_checked:
                    errors.append(
                        f"machine_checked composition {node_id} no longer machine-checks: {result.verdict}"
                    )
                recorded_signature = entry.get("signature")
                if recorded_signature != result.composition_signature:
                    errors.append(
                        f"machine_checked composition {node_id} has stale signature: "
                        f"recorded={recorded_signature!r} current={result.composition_signature!r}"
                    )
            except Exception as exc:
                errors.append(f"machine_checked composition {node_id}: {exc}")

    return errors, warnings
