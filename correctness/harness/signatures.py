from __future__ import annotations

import hashlib
import json
from typing import Any

from graph_schema import PROOF_SEMANTICS_VERSION

from .contracts import AUDIT_INTERPRETATION_RULES
from .model import (
    Graph,
    _catalog_entry_payload,
    _catalog_namespace,
    _plain_data,
    _semantic_catalog_refs_for_node,
)

def _node_semantic_signature(
    graph: Graph, node_id: str, memo: dict[str, str] | None = None
) -> str:
    """Hash a node, its proof closure, and the semantic catalog definitions it relies on."""
    memo = memo if memo is not None else {}
    if node_id in memo:
        return memo[node_id]
    node = graph.require_node(node_id)
    deps = []
    for dep in node.dependencies:
        if dep not in graph.nodes:
            continue
        deps.append({"id": dep, "signature": _node_semantic_signature(graph, dep, memo)})
    semantic_data = dict(node.data)
    for field in ("source_refs",):
        semantic_data.pop(field, None)
    catalog_refs = _semantic_catalog_refs_for_node(graph, node)
    semantic_catalog: dict[str, Any] = {}
    for namespace, keys in catalog_refs.items():
        # semantic_contracts was added in schema 0.9. Keep old signatures byte-for-byte stable
        # for nodes that do not reference a contract; only referenced contracts extend semantics.
        if namespace in {"semantic_contracts", "scope_dimensions"} and not keys:
            continue
        entries = _catalog_namespace(graph, namespace)
        semantic_catalog[namespace] = {
            key: _catalog_entry_payload(entries[key]) for key in keys if key in entries
        }
    # Failure semantics constrain every execution, even when a claim does not name an event.
    semantic_catalog["failure_events"] = _plain_data(_catalog_namespace(graph, "failure_events"))
    system = graph.doc.get("system", {})
    scope = graph.doc.get("scope", {})
    global_semantics = {
        "proof_semantics_version": PROOF_SEMANTICS_VERSION,
        "audit_interpretation_rules": list(AUDIT_INTERPRETATION_RULES),
        "system_summary": system.get("summary") if isinstance(system, dict) else None,
        "liveness_boundary": system.get("liveness_boundary") if isinstance(system, dict) else None,
        "scope": _plain_data(scope) if isinstance(scope, dict) else scope,
    }
    if node.data.get("semantic_contracts"):
        # Contract-bearing claims need a fresh refinement audit whenever the realization
        # obligation itself changes. Non-contract claims keep byte-for-byte compatible
        # signatures so a tooling-only change does not reopen unrelated branches.
        global_semantics["contract_realization_semantics_version"] = (
            CONTRACT_REALIZATION_SEMANTICS_VERSION
        )
    payload = {
        "node_id": node_id,
        "node_type": node.node_type,
        "data": semantic_data,
        "semantic_catalog": semantic_catalog,
        "global_semantics": global_semantics,
        "dependencies": deps,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    memo[node_id] = digest
    return digest



ASSURANCE_SEMANTICS_VERSION = "1"
COVERAGE_AUDIT_SEMANTICS_VERSION = "7"
CONTRACT_REALIZATION_SEMANTICS_VERSION = "1"


def _node_proposition_signature(graph: Graph, node_id: str) -> str:
    """Hash one proposition and its interpretation context, excluding proof dependencies."""
    node = graph.require_node(node_id)
    semantic_data = dict(node.data)
    for field in ("source_refs", "depends_on"):
        semantic_data.pop(field, None)
    catalog_refs = _semantic_catalog_refs_for_node(graph, node)
    semantic_catalog: dict[str, Any] = {}
    for namespace, keys in catalog_refs.items():
        if namespace in {"semantic_contracts", "scope_dimensions"} and not keys:
            continue
        entries = _catalog_namespace(graph, namespace)
        semantic_catalog[namespace] = {
            key: _catalog_entry_payload(entries[key]) for key in keys if key in entries
        }
    semantic_catalog["failure_events"] = _plain_data(_catalog_namespace(graph, "failure_events"))
    system = graph.doc.get("system", {})
    scope = graph.doc.get("scope", {})
    payload = {
        "proof_semantics_version": PROOF_SEMANTICS_VERSION,
        "audit_interpretation_rules": list(AUDIT_INTERPRETATION_RULES),
        "node_id": node_id,
        "node_type": node.node_type,
        "data": semantic_data,
        "semantic_catalog": semantic_catalog,
        "global_semantics": {
            "system_summary": system.get("summary") if isinstance(system, dict) else None,
            "liveness_boundary": system.get("liveness_boundary") if isinstance(system, dict) else None,
            "scope": _plain_data(scope) if isinstance(scope, dict) else scope,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _composition_semantic_signature(graph: Graph, node_id: str) -> str:
    """Hash exactly the logical composition contract: direct premises imply target."""
    node = graph.require_node(node_id)
    if node.node_type != "claim" or node.data.get("kind") not in {"root", "derived"}:
        raise SystemExit("Composition signatures apply only to root/derived claims")
    payload = {
        "assurance_semantics_version": ASSURANCE_SEMANTICS_VERSION,
        "target": {"id": node_id, "signature": _node_proposition_signature(graph, node_id)},
        "direct_dependencies": [
            {"id": dep, "signature": _node_proposition_signature(graph, dep)}
            for dep in sorted(node.dependencies)
            if dep in graph.nodes
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()




def _canonical_design_digest(graph: Graph) -> dict[str, Any]:
    source = graph.doc.get("source", {})
    article = source.get("article") if isinstance(source, dict) else None
    payload: dict[str, Any] = {"article": article}
    if isinstance(article, str) and graph.repository_root is not None:
        path = graph.repository_root / article
        try:
            payload["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            payload["sha256"] = None
    else:
        payload["sha256"] = None
    return payload


def _model_semantic_signature(graph: Graph) -> str:
    """Hash the semantics reviewed by architecture-level root-coverage audits."""
    memo: dict[str, str] = {}
    system = graph.doc.get("system", {})
    scope = graph.doc.get("scope", {})
    catalog = graph.doc.get("catalog", {})
    failure_events = catalog.get("failure_events", {}) if isinstance(catalog, dict) else {}
    payload = {
        "assurance_semantics_version": ASSURANCE_SEMANTICS_VERSION,
        "coverage_audit_semantics_version": COVERAGE_AUDIT_SEMANTICS_VERSION,
        "proof_semantics_version": PROOF_SEMANTICS_VERSION,
        "roots": [
            {"id": root_id, "signature": _node_semantic_signature(graph, root_id, memo)}
            for root_id in sorted(graph.roots)
            if root_id in graph.claims
        ],
        "assumptions": _plain_data(graph.doc.get("assumptions", {})),
        "system_summary": system.get("summary") if isinstance(system, dict) else None,
        "liveness_boundary": system.get("liveness_boundary") if isinstance(system, dict) else None,
        "scope": _plain_data(scope) if isinstance(scope, dict) else scope,
        "failure_events": _plain_data(failure_events),
        "canonical_design": _canonical_design_digest(graph),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


