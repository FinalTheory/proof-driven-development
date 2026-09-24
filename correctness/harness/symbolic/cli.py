from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

import yaml

from ..model import Graph, UniqueKeyLoader
from .assurance_registry import (
    SubjectTrustStatus,
    TranslationAssuranceRegistry,
)
from .bridge import SymbolicBridge, SymbolicBridgeError
from .composition import SymbolicCompositionError, SymbolicCompositionVerifier
from .coverage import (
    CoverageSpec,
    SymbolicCoverageError,
    SymbolicCoverageVerifier,
    build_coverage_artifact,
)
from .translation_assurance import TranslationReviewError
from .verification import SymbolicVerifier


DEFAULT_SYMBOLIC_MODEL = Path(__file__).resolve().parent.parent.parent / "symbolic_models" / "google_docs.poc.yaml"
DEFAULT_ASSURANCE_REGISTRY = Path(__file__).resolve().parent.parent.parent / "symbolic_models" / "assurance.poc.yaml"
DEFAULT_COVERAGE_MODEL = Path(__file__).resolve().parent.parent.parent / "symbolic_models" / "coverage.poc.yaml"


def add_symbolic_subparsers(sub: argparse._SubParsersAction) -> None:
    status = sub.add_parser(
        "symbolic-status",
        help="Report trust state for experimental symbolic contract/claim mappings.",
    )
    status.add_argument(
        "--model",
        default=str(DEFAULT_SYMBOLIC_MODEL),
        help="Symbolic sidecar YAML path.",
    )
    status.add_argument(
        "--assurance",
        default=str(DEFAULT_ASSURANCE_REGISTRY),
        help="Translation-assurance registry YAML path.",
    )
    status.add_argument(
        "--format",
        choices=["text", "yaml"],
        default="text",
    )

    check = sub.add_parser(
        "symbolic-check",
        help="Check one bad-state query against selected symbolic mappings.",
    )
    check.add_argument(
        "--kind",
        choices=["contract", "claim"],
        required=True,
        help="Whether --subject names semantic contracts or DAG claims.",
    )
    check.add_argument(
        "--subject",
        action="append",
        required=True,
        help="Mapped contract/claim ID. Repeatable.",
    )
    check.add_argument(
        "--query",
        required=True,
        help="YAML file containing exactly one top-level query: symbolic-expression mapping.",
    )
    check.add_argument(
        "--model",
        default=str(DEFAULT_SYMBOLIC_MODEL),
        help="Symbolic sidecar YAML path.",
    )
    check.add_argument(
        "--assurance",
        default=str(DEFAULT_ASSURANCE_REGISTRY),
        help="Translation-assurance registry YAML path.",
    )
    check.add_argument(
        "--allow-untrusted",
        action="store_true",
        help="Experimental only: run mapped formulas without requiring trusted translation assurance.",
    )
    check.add_argument(
        "--format",
        choices=["text", "yaml"],
        default="text",
    )


    cover = sub.add_parser(
        "symbolic-cover",
        help="Run one declarative symbolic bad-state coverage exclusion check.",
    )
    cover.add_argument("case", help="Coverage case ID from the coverage sidecar.")
    cover.add_argument(
        "--coverage",
        default=str(DEFAULT_COVERAGE_MODEL),
        help="Declarative symbolic coverage YAML path.",
    )
    cover.add_argument(
        "--model",
        default=str(DEFAULT_SYMBOLIC_MODEL),
        help="Symbolic sidecar YAML path.",
    )
    cover.add_argument(
        "--assurance",
        default=str(DEFAULT_ASSURANCE_REGISTRY),
        help="Translation-assurance registry YAML path.",
    )
    cover.add_argument(
        "--allow-untrusted",
        action="store_true",
        help="Experimental only: run coverage without requiring trusted translations.",
    )
    cover.add_argument(
        "--write-artifact",
        action="store_true",
        help="Persist the self-contained machine-check artifact declared by the coverage case.",
    )
    cover.add_argument(
        "--format",
        choices=["text", "yaml"],
        default="text",
    )


    compose = sub.add_parser(
        "symbolic-compose",
        help="Check whether a root/derived claim follows from its direct symbolic premises.",
    )
    compose.add_argument("node", help="Root/derived target claim ID.")
    compose.add_argument(
        "--model",
        default=str(DEFAULT_SYMBOLIC_MODEL),
        help="Symbolic sidecar YAML path.",
    )
    compose.add_argument(
        "--assurance",
        default=str(DEFAULT_ASSURANCE_REGISTRY),
        help="Translation-assurance registry YAML path.",
    )
    compose.add_argument(
        "--allow-untrusted",
        action="store_true",
        help="Experimental only: run composition without requiring trusted translations.",
    )
    compose.add_argument(
        "--format",
        choices=["text", "yaml"],
        default="text",
    )

    report = sub.add_parser(
        "symbolic-report",
        help="Render human-readable proof reports for machine-checked symbolic compositions.",
    )
    report.add_argument(
        "node",
        nargs="*",
        help=(
            "Optional machine-checked composition claim IDs. "
            "When omitted, report every currently machine-checked composition."
        ),
    )
    report.add_argument(
        "--model",
        default=str(DEFAULT_SYMBOLIC_MODEL),
        help="Symbolic sidecar YAML path.",
    )
    report.add_argument(
        "--assurance",
        default=str(DEFAULT_ASSURANCE_REGISTRY),
        help="Translation-assurance registry YAML path.",
    )
    report.add_argument(
        "--format",
        choices=["text", "yaml"],
        default="text",
    )


def handle_symbolic_command(graph: Graph, args: argparse.Namespace) -> int | None:
    try:
        if args.command == "symbolic-status":
            return _cmd_symbolic_status(graph, args)
        if args.command == "symbolic-check":
            return _cmd_symbolic_check(graph, args)
        if args.command == "symbolic-cover":
            return _cmd_symbolic_cover(graph, args)
        if args.command == "symbolic-compose":
            return _cmd_symbolic_compose(graph, args)
        if args.command == "symbolic-report":
            return _cmd_symbolic_report(graph, args)
        return None
    except (
        TranslationReviewError,
        SymbolicBridgeError,
        SymbolicCompositionError,
        SymbolicCoverageError,
    ) as exc:
        print(f"symbolic error: {exc}", file=sys.stderr)
        return 2


def _load_bridge(path: str) -> SymbolicBridge:
    return SymbolicBridge.load(Path(path))


def _load_registry(path: str) -> TranslationAssuranceRegistry:
    return TranslationAssuranceRegistry.load(Path(path))


def _cmd_symbolic_status(graph: Graph, args: argparse.Namespace) -> int:
    bridge = _load_bridge(args.model)
    bridge.validate_against_graph(graph)
    assurance_path = Path(args.assurance)
    if assurance_path.exists():
        registry = _load_registry(args.assurance)
    else:
        registry = TranslationAssuranceRegistry(version=1, records={})

    payload: dict[str, Any] = {
        "model": args.model,
        "assurance": args.assurance,
        "contracts": {},
        "claims": {},
    }
    for contract_id in sorted(bridge.contracts):
        result = registry.evaluate(
            graph,
            bridge,
            kind="contract",
            subject_id=contract_id,
        )
        payload["contracts"][contract_id] = {
            "status": result.status.value,
            "reason": result.reason,
            "signature": result.current_signature,
        }
    for claim_id in sorted(bridge.claims):
        result = registry.evaluate(
            graph,
            bridge,
            kind="claim",
            subject_id=claim_id,
        )
        payload["claims"][claim_id] = {
            "status": result.status.value,
            "reason": result.reason,
            "signature": result.current_signature,
        }

    if args.format == "yaml":
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).rstrip())
        return 0

    for kind_key in ("contracts", "claims"):
        print(f"{kind_key}:")
        entries = payload[kind_key]
        if not entries:
            print("  (none)")
            continue
        for subject_id, status in entries.items():
            print(f"  {subject_id}: {status['status']} — {status['reason']}")
    return 0


def _cmd_symbolic_check(graph: Graph, args: argparse.Namespace) -> int:
    bridge = _load_bridge(args.model)
    registry = _load_registry(args.assurance)
    verifier = SymbolicVerifier(graph, bridge, registry)

    query_doc = _read_query(Path(args.query))
    subjects = list(dict.fromkeys(args.subject))

    if args.kind == "contract":
        if args.allow_untrusted:
            result = verifier.exclusion_check(query_doc, contracts=subjects)
        else:
            result = verifier.trusted_exclusion_check(query_doc, contracts=subjects)
    else:
        if args.allow_untrusted:
            result = verifier.claim_exclusion_check(query_doc, claims=subjects)
        else:
            result = verifier.trusted_claim_exclusion_check(query_doc, claims=subjects)

    payload = {
        "kind": args.kind,
        "subjects": subjects,
        "trusted_required": not args.allow_untrusted,
        "baseline": {
            "status": result.baseline.status,
            "model": result.baseline.model,
        },
        "constrained": {
            "status": result.constrained.status,
            "model": result.constrained.model,
        },
        "closes_counterexample": result.closes_counterexample,
    }
    if args.format == "yaml":
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).rstrip())
    else:
        print(
            f"baseline={result.baseline.status} "
            f"constrained={result.constrained.status} "
            f"closes_counterexample={str(result.closes_counterexample).lower()}"
        )
        if result.constrained.status == "sat" and result.constrained.model:
            print("constrained_model:")
            print(result.constrained.model)
    return 0




def _cmd_symbolic_cover(graph: Graph, args: argparse.Namespace) -> int:
    bridge = _load_bridge(args.model)
    registry = _load_registry(args.assurance)
    spec = CoverageSpec.load(Path(args.coverage))
    case = spec.require_case(args.case)
    verifier = SymbolicCoverageVerifier(graph, bridge, registry)
    result = verifier.check(
        case,
        require_trusted=not args.allow_untrusted,
    )
    artifact = build_coverage_artifact(graph, bridge, registry, case, result)

    wrote_artifact = False
    if args.write_artifact:
        if args.allow_untrusted:
            raise SymbolicCoverageError(
                "refusing to persist a coverage artifact from untrusted translations"
            )
        if not result.machine_checked:
            raise SymbolicCoverageError(
                f"refusing to persist non-machine-checked coverage result: {result.verdict}"
            )
        correctness_root = Path(__file__).resolve().parent.parent.parent
        artifact_path = correctness_root / case.artifact_ref
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            yaml.safe_dump(artifact, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        wrote_artifact = True

    if args.format == "yaml":
        print(yaml.safe_dump(artifact, sort_keys=False, allow_unicode=True).rstrip())
        return 0

    print(
        f"case={case.case_id} verdict={result.verdict} "
        f"baseline={result.baseline.status} "
        f"constrained={result.constrained.status} "
        f"machine_checked={str(result.machine_checked).lower()}"
    )
    if result.baseline.status == "sat" and result.baseline.model:
        print("baseline_witness_model:")
        print(result.baseline.model)
    if result.constrained.status == "sat" and result.constrained.model:
        print("constrained_counterexample_model:")
        print(result.constrained.model)
    if wrote_artifact:
        print(f"artifact={case.artifact_ref}")
    return 0


def _cmd_symbolic_compose(graph: Graph, args: argparse.Namespace) -> int:
    bridge = _load_bridge(args.model)
    registry = _load_registry(args.assurance)
    verifier = SymbolicCompositionVerifier(graph, bridge, registry)
    result = verifier.check(
        args.node,
        require_trusted=not args.allow_untrusted,
    )
    payload = {
        "node": result.node_id,
        "premises": list(result.premise_ids),
        "trusted_required": not args.allow_untrusted,
        "composition_signature": result.composition_signature,
        "solver_status": result.solver_result.status,
        "verdict": result.verdict,
        "machine_checked": result.machine_checked,
        "counterexample_model": (
            result.solver_result.model
            if result.solver_result.status == "sat"
            else None
        ),
    }
    if args.format == "yaml":
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).rstrip())
    else:
        print(
            f"node={result.node_id} verdict={result.verdict} "
            f"solver={result.solver_result.status} "
            f"premises={','.join(result.premise_ids)} "
            f"trusted_required={str(not args.allow_untrusted).lower()}"
        )
        if result.solver_result.status == "sat" and result.solver_result.model:
            print("counterexample_model:")
            print(result.solver_result.model)
    return 0


def _cmd_symbolic_report(graph: Graph, args: argparse.Namespace) -> int:
    bridge = _load_bridge(args.model)
    registry = _load_registry(args.assurance)
    verifier = SymbolicCompositionVerifier(graph, bridge, registry)

    composition = graph.doc.get("assurance", {}).get("composition", {})
    if not isinstance(composition, dict):
        raise SymbolicCompositionError("canonical composition assurance is not a mapping")

    if args.node:
        node_ids = list(dict.fromkeys(args.node))
    else:
        node_ids = sorted(
            node_id
            for node_id, entry in composition.items()
            if isinstance(entry, dict) and entry.get("status") == "machine_checked"
        )

    reports: list[dict[str, Any]] = []
    for node_id in node_ids:
        node = graph.require_node(node_id)
        entry = composition.get(node_id)
        if not isinstance(entry, dict) or entry.get("status") != "machine_checked":
            raise SymbolicCompositionError(
                f"{node_id} is not currently recorded as machine_checked composition assurance"
            )

        result = verifier.check(node_id, require_trusted=True)
        artifact_refs = entry.get("artifact_refs", [])
        if not isinstance(artifact_refs, list) or not artifact_refs:
            raise SymbolicCompositionError(
                f"{node_id} is machine_checked but has no composition artifact_refs"
            )

        artifacts = []
        for artifact_ref in artifact_refs:
            if not isinstance(artifact_ref, str) or not artifact_ref:
                raise SymbolicCompositionError(
                    f"{node_id} has invalid composition artifact reference"
                )
            artifact_path = Path(__file__).resolve().parent.parent.parent / artifact_ref
            try:
                artifact = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise SymbolicCompositionError(
                    f"{node_id} composition artifact unavailable at {artifact_ref}: {exc}"
                ) from exc
            except yaml.YAMLError as exc:
                raise SymbolicCompositionError(
                    f"{node_id} composition artifact is invalid YAML at {artifact_ref}: {exc}"
                ) from exc
            if not isinstance(artifact, dict):
                raise SymbolicCompositionError(
                    f"{node_id} composition artifact must be a mapping: {artifact_ref}"
                )
            artifacts.append({"path": artifact_ref, "data": artifact})

        target_trust = registry.evaluate(
            graph,
            bridge,
            kind="claim",
            subject_id=node_id,
        )
        premise_reports = []
        for premise_id in result.premise_ids:
            premise = graph.require_node(premise_id)
            premise_trust = registry.evaluate(
                graph,
                bridge,
                kind="claim",
                subject_id=premise_id,
            )
            premise_reports.append(
                {
                    "id": premise_id,
                    "statement": premise.data.get("statement", ""),
                    "translation_status": premise_trust.status.value,
                }
            )

        mutation_tests: list[dict[str, Any]] = []
        for artifact in artifacts:
            data = artifact["data"]
            if isinstance(data.get("mutation_test"), dict):
                mutation_tests.append(data["mutation_test"])
            raw_mutations = data.get("mutation_tests")
            if isinstance(raw_mutations, list):
                mutation_tests.extend(
                    item for item in raw_mutations if isinstance(item, dict)
                )

        reports.append(
            {
                "node": node_id,
                "kind": node.data.get("kind"),
                "statement": node.data.get("statement", ""),
                "translation_status": target_trust.status.value,
                "premises": premise_reports,
                "query_semantics": (
                    " AND ".join(result.premise_ids) + f" AND NOT({node_id})"
                ),
                "solver_status": result.solver_result.status,
                "verdict": result.verdict,
                "meaning": (
                    "No counterexample model exists: all direct premises can be true "
                    "only if the target is also true."
                    if result.solver_result.status == "unsat"
                    else "A counterexample model exists under the current symbolic abstraction."
                ),
                "mutation_tests": mutation_tests,
                "artifact_refs": artifact_refs,
                "rationale": entry.get("rationale", ""),
                "composition_signature": result.composition_signature,
            }
        )

    if args.format == "yaml":
        print(yaml.safe_dump({"reports": reports}, sort_keys=False, allow_unicode=True).rstrip())
        return 0

    if not reports:
        print("No machine-checked symbolic compositions.")
        return 0

    for index, report in enumerate(reports):
        if index:
            print()
        print(f"{report['node']}  [MACHINE-CHECKED]")
        print(f"Target: {report['statement']}")
        print(f"Translation: {report['translation_status']}")
        print("Direct premises:")
        for premise in report["premises"]:
            print(
                f"  - {premise['id']} [{premise['translation_status']}]: "
                f"{premise['statement']}"
            )
        print(f"SMT counterexample query: {report['query_semantics']}")
        print(f"Solver: {report['solver_status'].upper()} → {report['verdict']}")
        print(f"Meaning: {report['meaning']}")
        if report["mutation_tests"]:
            print("Mutation sensitivity:")
            for mutation in report["mutation_tests"]:
                weakened = (
                    mutation.get("weakened_premise")
                    or mutation.get("description")
                    or "mutated premise"
                )
                status = str(mutation.get("solver_status", "unknown")).upper()
                verdict = mutation.get("verdict", "")
                print(
                    f"  - {weakened}: {status}"
                    + (f" → {verdict}" if verdict else "")
                )
        print("Evidence:")
        for artifact_ref in report["artifact_refs"]:
            print(f"  - {artifact_ref}")
        if report["rationale"]:
            print(f"Recorded rationale: {report['rationale']}")
    return 0


def _read_query(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except OSError as exc:
        raise TranslationReviewError(f"symbolic query unavailable at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TranslationReviewError(f"invalid symbolic query YAML: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"query"}:
        raise TranslationReviewError(
            "symbolic query file must contain exactly one top-level query field"
        )
    query = raw["query"]
    if not isinstance(query, dict):
        raise TranslationReviewError("symbolic query must be an expression mapping")
    return query
