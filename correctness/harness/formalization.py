from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import Graph, _catalog_namespace


FORMALIZATION_KINDS = ("claim", "assumption", "semantic_contract")


@dataclass(frozen=True)
class FormalizationState:
    kind: str
    subject_id: str
    mode: str
    effective: str
    reason: str | None = None
    rationale: str | None = None
    translation_status: str | None = None
    symbolic_capability: str | None = None


def formalization_entry(graph: Graph, *, kind: str, subject_id: str) -> dict[str, Any]:
    registry = graph.doc.get("formalization", {})
    if not isinstance(registry, dict):
        raise ValueError("formalization registry is missing")
    namespace = {
        "claim": "claims",
        "assumption": "assumptions",
        "semantic_contract": "semantic_contracts",
    }.get(kind)
    if namespace is None:
        raise ValueError(f"unknown formalization kind {kind!r}")
    entries = registry.get(namespace)
    if not isinstance(entries, dict):
        raise ValueError(f"formalization.{namespace} is missing")
    entry = entries.get(subject_id)
    if not isinstance(entry, dict):
        raise ValueError(f"no formalization entry for {kind} {subject_id}")
    return entry


def _mapping_exists(bridge: Any, *, kind: str, subject_id: str) -> bool:
    if kind == "claim":
        return subject_id in bridge.claims
    if kind == "assumption":
        return subject_id in bridge.assumptions
    if kind == "semantic_contract":
        return subject_id in bridge.contracts
    raise ValueError(kind)


def _mapping_capability(bridge: Any, *, kind: str, subject_id: str) -> str:
    if kind == "claim":
        return bridge.claims[subject_id].capability
    if kind == "assumption":
        return bridge.assumptions[subject_id].capability
    if kind == "semantic_contract":
        return bridge.contracts[subject_id].capability
    raise ValueError(kind)


def subject_formalization_state(
    graph: Graph,
    bridge: Any,
    registry: Any,
    *,
    kind: str,
    subject_id: str,
) -> FormalizationState:
    entry = formalization_entry(graph, kind=kind, subject_id=subject_id)
    mode = entry.get("mode")
    if mode == "non_symbolic":
        return FormalizationState(
            kind=kind,
            subject_id=subject_id,
            mode=mode,
            effective="NON_SYMBOLIC",
            reason=entry.get("reason"),
            rationale=entry.get("rationale"),
        )
    if mode != "symbolic":
        return FormalizationState(
            kind=kind,
            subject_id=subject_id,
            mode=str(mode),
            effective="INVALID",
        )
    if not _mapping_exists(bridge, kind=kind, subject_id=subject_id):
        return FormalizationState(
            kind=kind,
            subject_id=subject_id,
            mode=mode,
            effective="MAPPING_MISSING",
        )
    capability = _mapping_capability(bridge, kind=kind, subject_id=subject_id)
    assurance_kind = "contract" if kind == "semantic_contract" else kind
    trust = registry.evaluate(
        graph,
        bridge,
        kind=assurance_kind,
        subject_id=subject_id,
    )
    if capability == "opaque":
        effective = "SYMBOLIC_OPAQUE"
    elif trust.status.value == "TRUSTED":
        effective = "SYMBOLIC_READY"
    else:
        effective = "TRANSLATION_" + trust.status.value
    return FormalizationState(
        kind=kind,
        subject_id=subject_id,
        mode=mode,
        effective=effective,
        translation_status=trust.status.value,
        symbolic_capability=capability,
    )


def formalization_snapshot(graph: Graph, bridge: Any, registry: Any) -> dict[str, Any]:
    subjects = {
        "claims": [("claim", subject_id) for subject_id in sorted(graph.claims)],
        "assumptions": [("assumption", subject_id) for subject_id in sorted(graph.assumptions)],
        "semantic_contracts": [
            ("semantic_contract", subject_id)
            for subject_id in sorted(_catalog_namespace(graph, "semantic_contracts"))
        ],
    }
    payload: dict[str, Any] = {"subjects": {}, "counts": {}}
    for namespace, entries in subjects.items():
        rendered: dict[str, Any] = {}
        counts: dict[str, int] = {}
        for kind, subject_id in entries:
            state = subject_formalization_state(
                graph, bridge, registry, kind=kind, subject_id=subject_id
            )
            rendered[subject_id] = {
                "mode": state.mode,
                "effective": state.effective,
                **({"translation_status": state.translation_status} if state.translation_status else {}),
                **({"symbolic_capability": state.symbolic_capability} if state.symbolic_capability else {}),
                **({"reason": state.reason} if state.reason else {}),
                **({"rationale": state.rationale} if state.rationale else {}),
            }
            counts[state.effective] = counts.get(state.effective, 0) + 1
        payload["subjects"][namespace] = rendered
        payload["counts"][namespace] = dict(sorted(counts.items()))
    return payload


def composition_formalization_route(
    graph: Graph,
    bridge: Any,
    registry: Any,
    node_id: str,
) -> dict[str, Any]:
    node = graph.require_node(node_id)
    if node.node_type != "claim" or node.data.get("kind") not in {"root", "derived"}:
        raise ValueError("composition routing applies only to root/derived claims")

    target = subject_formalization_state(
        graph, bridge, registry, kind="claim", subject_id=node_id
    )
    if target.mode == "non_symbolic":
        return {
            "backend": "non_symbolic",
            "state": "READY",
            "target": target.effective,
            "blocking": [],
        }

    blocking: list[dict[str, str]] = []
    if target.effective != "SYMBOLIC_READY":
        blocking.append({"subject": node_id, "state": target.effective})

    premises: list[dict[str, str]] = []
    for premise_id in node.dependencies:
        premise = graph.require_node(premise_id)
        if premise.node_type == "claim":
            kind = "claim"
        elif premise.node_type == "assumption":
            kind = "assumption"
        else:
            blocking.append({"subject": premise_id, "state": "UNSUPPORTED_NODE_TYPE"})
            continue
        state = subject_formalization_state(
            graph, bridge, registry, kind=kind, subject_id=premise_id
        )
        premises.append(
            {"subject": premise_id, "mode": state.mode, "effective": state.effective}
        )
        if state.mode == "non_symbolic":
            blocking.append({"subject": premise_id, "state": "NON_SYMBOLIC_PREMISE"})
        elif state.effective != "SYMBOLIC_READY":
            blocking.append({"subject": premise_id, "state": state.effective})

    return {
        "backend": "symbolic",
        "state": "READY" if not blocking else "BLOCKED",
        "target": target.effective,
        "premises": premises,
        "blocking": blocking,
    }
