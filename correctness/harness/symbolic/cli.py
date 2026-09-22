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
from .translation_assurance import TranslationReviewError
from .verification import SymbolicVerifier


DEFAULT_SYMBOLIC_MODEL = Path(__file__).resolve().parent.parent.parent / "symbolic_models" / "google_docs.poc.yaml"
DEFAULT_ASSURANCE_REGISTRY = Path(__file__).resolve().parent.parent.parent / "symbolic_models" / "assurance.poc.yaml"


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


def handle_symbolic_command(graph: Graph, args: argparse.Namespace) -> int | None:
    try:
        if args.command == "symbolic-status":
            return _cmd_symbolic_status(graph, args)
        if args.command == "symbolic-check":
            return _cmd_symbolic_check(graph, args)
        return None
    except (TranslationReviewError, SymbolicBridgeError) as exc:
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
