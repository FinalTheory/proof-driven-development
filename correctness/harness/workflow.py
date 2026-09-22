from __future__ import annotations

import collections
from typing import Any

from .model import Graph, _plain_data
from .signatures import (
    _composition_semantic_signature,
    _model_semantic_signature,
    _node_semantic_signature,
)

def _audit_task_priority(kind: Any) -> tuple[str, int]:
    if kind == "leaf":
        return "leaf_boundary_audit", 100
    if kind == "derived":
        return "proof_audit", 200
    if kind == "root":
        return "proof_audit", 300
    raise SystemExit(f"Unsupported claim kind: {kind}")



def _assurance_snapshot(graph: Graph) -> dict[str, Any]:
    """Compute effective orthogonal assurance state without changing refinement scheduling."""
    assurance = graph.doc.get("assurance", {})
    spec = assurance.get("specification_coverage", {}) if isinstance(assurance, dict) else {}
    composition = assurance.get("composition", {}) if isinstance(assurance, dict) else {}
    evidence = assurance.get("implementation_evidence", {}) if isinstance(assurance, dict) else {}

    model_signature = _model_semantic_signature(graph)
    spec_declared = spec.get("status", "unaudited") if isinstance(spec, dict) else "unaudited"
    if spec_declared == "unaudited":
        spec_effective = "unaudited"
    elif isinstance(spec, dict) and spec.get("signature") == model_signature:
        spec_effective = spec_declared
    else:
        spec_effective = "stale"

    node_signatures: dict[str, str] = {}
    composition_nodes: dict[str, Any] = {}
    evidence_nodes: dict[str, Any] = {}

    for node_id, claim in sorted(graph.claims.items()):
        kind = claim.get("kind")
        current_sig = _node_semantic_signature(graph, node_id, node_signatures)
        if kind in {"root", "derived"}:
            composition_sig = _composition_semantic_signature(graph, node_id)
            entry = composition.get(node_id, {}) if isinstance(composition, dict) else {}
            declared = entry.get("status", "unaudited") if isinstance(entry, dict) else "unaudited"
            effective = declared
            if declared != "unaudited" and entry.get("signature") != composition_sig:
                effective = "stale"
            composition_nodes[node_id] = {
                "declared": declared,
                "effective": effective,
                "current_signature": composition_sig,
            }
            if isinstance(entry, dict) and "signature" in entry:
                composition_nodes[node_id]["recorded_signature"] = entry["signature"]
            if isinstance(entry, dict):
                for key in ("auditor_count", "artifact_refs", "rationale"):
                    if key in entry:
                        composition_nodes[node_id][key] = _plain_data(entry[key])
        elif kind == "leaf":
            entry = evidence.get(node_id, {}) if isinstance(evidence, dict) else {}
            declared = entry.get("status", "planned") if isinstance(entry, dict) else "planned"
            effective = declared
            if declared != "planned" and entry.get("signature") != current_sig:
                effective = "stale"
            evidence_nodes[node_id] = {
                "declared": declared,
                "effective": effective,
                "current_signature": current_sig,
            }
            if isinstance(entry, dict) and "signature" in entry:
                evidence_nodes[node_id]["recorded_signature"] = entry["signature"]
            if isinstance(entry, dict):
                for key in ("implementation_revision", "artifact_refs", "rationale"):
                    if key in entry:
                        evidence_nodes[node_id][key] = _plain_data(entry[key])

    def counts(items: dict[str, Any]) -> dict[str, int]:
        result: dict[str, int] = collections.Counter(
            item["effective"] for item in items.values()
        )
        return dict(sorted(result.items()))

    root_summary: dict[str, Any] = {}
    for root_id in sorted(graph.roots):
        if root_id not in graph.claims:
            continue
        closure = graph.dependency_closure(root_id)
        leaves = sorted(
            node_id
            for node_id in closure
            if node_id in graph.claims and graph.claims[node_id].get("kind") == "leaf"
        )
        leaf_states = [evidence_nodes[node_id]["effective"] for node_id in leaves]
        composite_nodes = sorted(
            node_id
            for node_id in closure
            if node_id in graph.claims and graph.claims[node_id].get("kind") in {"root", "derived"}
        )
        composition_states = [composition_nodes[node_id]["effective"] for node_id in composite_nodes]
        root_summary[root_id] = {
            "root_composition": composition_nodes.get(root_id, {}).get("effective", "unaudited"),
            "composition_closure_counts": dict(sorted(collections.Counter(composition_states).items())),
            "composition_complete": bool(composite_nodes) and all(
                state in {"single_agent_audited", "multi_agent_audited", "machine_checked"}
                for state in composition_states
            ),
            "leaf_evidence_counts": dict(sorted(collections.Counter(leaf_states).items())),
            "evidence_complete": bool(leaves) and all(state == "passing" for state in leaf_states),
        }

    return {
        "model_signature": model_signature,
        "specification_coverage": {
            "declared": spec_declared,
            "effective": spec_effective,
            "current_signature": model_signature,
            **({"recorded_signature": spec["signature"]} if isinstance(spec, dict) and "signature" in spec else {}),
            **({key: _plain_data(spec[key]) for key in ("auditor_count", "rationale") if isinstance(spec, dict) and key in spec}),
        },
        "composition": composition_nodes,
        "implementation_evidence": evidence_nodes,
        "counts": {
            "composition": counts(composition_nodes),
            "implementation_evidence": counts(evidence_nodes),
        },
        "roots": root_summary,
    }


def _refinement_snapshot(graph: Graph) -> dict[str, Any]:
    """Compute effective refinement state and the next bottom-up audit frontier."""
    refinement = graph.doc.get("refinement")
    if not isinstance(refinement, dict):
        return {
            "state": "INVALID",
            "runnable": [],
            "blocked_frontier": [],
            "waiting": [],
            "stable": [],
            "waived": [],
            "stale": [],
            "open_tooling_blockers": [],
            "open_decisions": [],
            "decision_impacts": {},
        }

    node_state = refinement.get("nodes")
    decisions = refinement.get("decisions")
    tooling_blockers = refinement.get("tooling_blockers")
    if not isinstance(node_state, dict) or not isinstance(decisions, dict) or not isinstance(tooling_blockers, dict):
        return {
            "state": "INVALID",
            "runnable": [],
            "blocked_frontier": [],
            "waiting": [],
            "stable": [],
            "waived": [],
            "stale": [],
            "open_decisions": [],
            "open_tooling_blockers": [],
            "decision_impacts": {},
        }

    open_tooling_blockers = sorted(
        blocker_id
        for blocker_id, blocker in tooling_blockers.items()
        if isinstance(blocker, dict) and blocker.get("status") == "open"
    )
    open_decisions = {
        decision_id
        for decision_id, decision in decisions.items()
        if isinstance(decision, dict) and decision.get("status") == "open"
    }
    signatures: dict[str, str] = {}
    effective: dict[str, str] = {}
    stale: list[str] = []

    for node_id in graph.claims:
        entry = node_state.get(node_id, {})
        declared = entry.get("status") if isinstance(entry, dict) else None
        if declared == "waived":
            effective[node_id] = "waived"
            continue
        if declared == "stable":
            current = _node_semantic_signature(graph, node_id, signatures)
            if entry.get("signature") == current:
                effective[node_id] = "stable"
            else:
                effective[node_id] = "pending"
                stale.append(node_id)
        else:
            effective[node_id] = "pending"

    runnable: list[dict[str, Any]] = []
    blocked_frontier: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    stable_nodes: list[str] = []
    waived_nodes: list[str] = []

    for node_id in sorted(graph.claims):
        state = effective[node_id]
        if state == "stable":
            stable_nodes.append(node_id)
            continue
        if state == "waived":
            waived_nodes.append(node_id)
            continue

        entry = node_state[node_id]
        claim_deps = [
            dep for dep in graph.nodes[node_id].dependencies if dep in graph.claims
        ]
        unresolved_deps = [
            dep
            for dep in claim_deps
            if effective.get(dep) not in {"stable", "waived"}
        ]
        blockers = [
            decision_id
            for decision_id in entry.get("blocked_by", [])
            if decision_id in open_decisions
        ]
        kind = graph.claims[node_id].get("kind")
        task, priority = _audit_task_priority(kind)
        item = {
            "node": node_id,
            "task": task,
            "priority": priority,
            "signature": _node_semantic_signature(graph, node_id, signatures),
        }
        if unresolved_deps:
            item["waiting_on"] = sorted(unresolved_deps)
            if blockers:
                item["blocked_by"] = sorted(blockers)
            waiting.append(item)
        elif blockers:
            item["blocked_by"] = sorted(blockers)
            blocked_frontier.append(item)
        else:
            runnable.append(item)

    runnable.sort(key=lambda item: (item["priority"], item["node"]))
    blocked_frontier.sort(key=lambda item: (item["priority"], item["node"]))
    waiting.sort(key=lambda item: (item["priority"], item["node"]))

    pending_count = len(runnable) + len(blocked_frontier) + len(waiting)
    if open_tooling_blockers:
        state = "TOOLING_BLOCKED"
    elif runnable:
        state = "CONTINUE"
    elif pending_count == 0:
        state = "COMPLETE"
    elif open_decisions:
        state = "HUMAN_BLOCKED"
    else:
        state = "STALLED"

    pending_nodes = {
        node_id for node_id, node_state_value in effective.items()
        if node_state_value == "pending"
    }
    reverse = graph.reverse_dependencies()
    decision_impacts: dict[str, Any] = {}
    for decision_id in sorted(open_decisions):
        direct = {
            node_id
            for node_id in pending_nodes
            if decision_id in node_state[node_id].get("blocked_by", [])
        }
        affected = set(direct)
        queue = collections.deque(sorted(direct))
        while queue:
            current = queue.popleft()
            for dependent in reverse.get(current, []):
                if dependent in pending_nodes and dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)
        decision_impacts[decision_id] = {
            "direct_nodes": sorted(direct),
            "affected_pending_nodes": sorted(affected),
            "affected_pending_count": len(affected),
            "affected_roots": sorted(set(graph.roots) & affected),
        }

    return {
        "state": state,
        "runnable": runnable,
        "blocked_frontier": blocked_frontier,
        "waiting": waiting,
        "stable": stable_nodes,
        "waived": waived_nodes,
        "stale": sorted(stale),
        "open_decisions": sorted(open_decisions),
        "open_tooling_blockers": open_tooling_blockers,
        "decision_impacts": decision_impacts,
    }


