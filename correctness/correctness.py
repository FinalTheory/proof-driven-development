#!/usr/bin/env python3
"""Utilities for validating and slicing the Google Docs correctness DAG.

The script treats correctness.yaml as the canonical source. Read commands provide
deterministic validation/slicing/scheduling; the mutate command is the only supported
writer and uses locking, optimistic semantic preconditions, validation, and atomic replace.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import fcntl

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised only without dependencies
    raise SystemExit(
        "PyYAML is required. Install with: python3 -m pip install -r requirements.txt"
    ) from exc

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover - exercised only without dependencies
    raise SystemExit(
        "jsonschema is required. Install with: python3 -m pip install -r requirements.txt"
    ) from exc

from graph_schema import (
    CANONICAL_GRAPH_SCHEMA,
    CLAIM_MUTABLE_FIELDS,
    COMPOSITION_ASSURANCE_STATUSES as SCHEMA_COMPOSITION_ASSURANCE_STATUSES,
    DECISION_STATUSES as SCHEMA_DECISION_STATUSES,
    EVIDENCE_ASSURANCE_STATUSES as SCHEMA_EVIDENCE_ASSURANCE_STATUSES,
    MUTATION_ASSUMPTION_BODY_SCHEMA,
    MUTATION_CLAIM_BODY_SCHEMA,
    MUTATION_CLAIM_UPDATE_SCHEMA,
    MUTATION_DECISION_BODY_SCHEMA,
    MUTATION_TOOLING_BLOCKER_BODY_SCHEMA,
    PROOF_SEMANTICS_VERSION,
    REFINEMENT_STATUSES as SCHEMA_REFINEMENT_STATUSES,
    SPECIFICATION_COVERAGE_STATUSES as SCHEMA_SPECIFICATION_COVERAGE_STATUSES,
    TOOLING_BLOCKER_STATUSES as SCHEMA_TOOLING_BLOCKER_STATUSES,
)

from harness.contracts import (
    AUDIT_INTERPRETATION_RULES,
    CATALOG_NAMESPACES,
    CLAIM_KINDS,
    COMPOSITION_ASSURANCE_STATUSES,
    DECISION_STATUSES,
    EVIDENCE_ASSURANCE_STATUSES,
    HARNESS_FEEDBACK_CONTRACT,
    LEAF_STOPPING_SIGNALS,
    MUTATION_PLAN_SCHEMA,
    NODE_ID_RE,
    REFINEMENT_STATUSES,
    SCHEMA_INDEX_END,
    SCHEMA_INDEX_LINE_RE,
    SCHEMA_INDEX_START,
    SCHEMA_REFERENCE_PATH,
    SEMANTIC_CATALOG_NAMESPACES,
    SNAKE_CASE_TERM_RE,
    SPECIFICATION_COVERAGE_STATUSES,
    TOOLING_BLOCKER_STATUSES,
    WORKFLOW_HELP,
    _CANONICAL_GRAPH_VALIDATOR,
    _MUTATION_PLAN_VALIDATOR,
    _render_leaf_stopping_signals,
)
from harness.model import (
    GRAPH_PATH,
    Graph,
    Node,
    UniqueKeyLoader,
    _as_mapping,
    _as_string_list,
    _catalog,
    _catalog_entry_payload,
    _catalog_namespace,
    _plain_data,
    _repository_root_for_graph_path,
    _semantic_catalog_refs_for_node,
    _slice_catalog_context,
)
from harness.signatures import (
    ASSURANCE_SEMANTICS_VERSION,
    COVERAGE_AUDIT_SEMANTICS_VERSION,
    _canonical_design_digest,
    _composition_semantic_signature,
    _model_semantic_signature,
    _node_proposition_signature,
    _node_semantic_signature,
)
from harness.workflow import (
    _assurance_snapshot,
    _audit_task_priority,
    _refinement_snapshot,
)
from harness.symbolic.cli import add_symbolic_subparsers, handle_symbolic_command
from harness.symbolic.formal_schema import FORMAL_SCHEMAS
from harness.validation import (
    _canonical_field_inventory,
    _condition_summary,
    _contains_exact_identifier,
    _format_schema_path,
    _markdown_h2_sections_by_heading,
    _print_validation,
    _presence_rules,
    _schema_constraint_summary,
    _schema_reference_contract_errors,
    _validate_source_traceability,
    validate,
)



def _ordered_slice(graph: Graph, target: str, stop_at: set[str]) -> list[str]:
    depths = graph.dependency_depths(target, stop_at)
    # Target first, then nearest proof obligations; deterministic tie-break by ID.
    return sorted(depths, key=lambda node_id: (depths[node_id], node_id))


def _slice_document(graph: Graph, target: str, stop_at: set[str]) -> dict[str, Any]:
    closure = graph.dependency_closure(target, stop_at)
    ordered = _ordered_slice(graph, target, stop_at)
    result: dict[str, Any] = {
        "target": target,
        "cut_points": sorted(stop_at & closure),
        "system_context": {
            "system": graph.doc.get("system", {}),
            "scope": graph.doc.get("scope", {}),
            "interpretation_rules": list(AUDIT_INTERPRETATION_RULES),
        },
        "catalog_context": _slice_catalog_context(graph, ordered),
        "assumptions": {},
        "claims": {},
    }
    for node_id in ordered:
        if node_id not in closure:
            continue
        node = graph.nodes[node_id]
        if node.node_type == "assumption":
            result["assumptions"][node_id] = node.data
        else:
            result["claims"][node_id] = node.data
    return result


def _emit_system_context(graph: Graph, node_ids: Iterable[str]) -> None:
    system = graph.doc.get("system", {})
    scope = graph.doc.get("scope", {})

    print("## Minimal system context")
    print()
    if isinstance(system, dict):
        system_id = system.get("id")
        if isinstance(system_id, str) and system_id.strip():
            print(f"System model: `{system_id.strip()}`")
            print()
        summary = system.get("summary")
        if isinstance(summary, str) and summary.strip():
            print(summary.strip())
            print()

    if isinstance(scope, dict):
        includes = scope.get("includes", [])
        excludes = scope.get("excludes", [])
        if isinstance(includes, list) and includes:
            print("### In scope")
            print()
            for item in includes:
                print(f"- {str(item).strip()}")
            print()
        if isinstance(excludes, list) and excludes:
            print("### Out of scope")
            print()
            for item in excludes:
                print(f"- {str(item).strip()}")
            print()

    selected = _slice_catalog_context(graph, node_ids)
    for namespace, title in (("terms", "Vocabulary"), ("state", "State vocabulary"), ("mechanisms", "Architecture mechanisms")):
        entries = selected.get(namespace, {})
        if isinstance(entries, dict) and entries:
            print(f"### {title}")
            print()
            for key, entry in entries.items():
                if isinstance(entry, dict):
                    description = entry.get("description", "")
                    suffix = ""
                    if namespace == "state":
                        suffix = f" [{entry.get('class')}]"
                    print(f"- **{key}**{suffix}: {description.strip()}")
            print()

    semantic_contracts = selected.get("semantic_contracts", {})
    if isinstance(semantic_contracts, dict) and semantic_contracts:
        print("### Semantic contracts")
        print()
        print("These entries are reusable property specifications: they define truth conditions and interpretation boundaries,")
        print("but do not assert that the system satisfies the property and are never proof premises by themselves.")
        print()
        for key, entry in semantic_contracts.items():
            if not isinstance(entry, dict):
                continue
            contract_class = entry.get("class", "?")
            print(f"- **{key}** [{contract_class}]: {str(entry.get('definition', '')).strip()}")
            if contract_class == "state_invariant":
                symbols = entry.get("state_symbols", [])
                print(f"  Observation scope: {entry.get('observation_scope', '?')}")
                if isinstance(symbols, list):
                    print("  State symbols: " + ", ".join(str(x) for x in symbols))
                print("  Audit semantics: treat this as an inductive invariant over the observation scope; establish it at entry/publication and preserve it across every in-scope transition that can mutate any listed symbol. Named transitions are not exhaustive unless explicitly excluded.")
            elif contract_class == "provenance_binding":
                sources = entry.get("source_symbols", [])
                bound = entry.get("bound_symbols", [])
                if isinstance(sources, list):
                    print("  Source symbols: " + ", ".join(str(x) for x in sources))
                if isinstance(bound, list):
                    print("  Bound symbols: " + ", ".join(str(x) for x in bound))
                print("  Audit semantics: trace producer/source/capture/binding through every consumer. Correct downstream processing of a bound value does not establish that the value came from the required source or was bound to the correct execution/state.")
            excludes = entry.get("excludes", [])
            if isinstance(excludes, list) and excludes:
                print("  Excludes:")
                for excluded in excludes:
                    print(f"  - {str(excluded).strip()}")
        print()

    failure_events = selected.get("failure_events", {})
    if isinstance(failure_events, dict) and failure_events:
        print("### Failure model")
        print()
        for cls, heading in (("allowed", "Allowed events"), ("excluded", "Excluded events")):
            rows = [(k, v) for k, v in failure_events.items() if isinstance(v, dict) and v.get("class") == cls]
            if rows:
                print(f"{heading}:")
                for key, entry in rows:
                    print(f"- **{key}**: {entry.get('description', '').strip()}")
                print()

    if isinstance(system, dict):
        boundary = system.get("liveness_boundary")
        if isinstance(boundary, str) and boundary.strip():
            print(f"Liveness boundary: {boundary.strip()}")
            print()

    print("### Interpretation rules")
    print()
    for rule in AUDIT_INTERPRETATION_RULES:
        print(f"- {rule}")
    print()

def _emit_slice_markdown(
    graph: Graph,
    target: str,
    stop_at: set[str],
    prompt: bool,
    include_verification_task: bool = True,
) -> None:
    closure = graph.dependency_closure(target, stop_at)
    depths = graph.dependency_depths(target, stop_at)
    target_node = graph.require_node(target)

    if prompt:
        print("# Correctness proof obligation")
        print()
        print("Reason only from the proof slice below. Treat explicit assumptions as accepted boundaries, ")
        print("but do not invent unstated premises. Try to refute the target before accepting it.")
        print()
        _emit_system_context(graph, _ordered_slice(graph, target, stop_at))
    else:
        print(f"# Proof slice: `{target}`")
        print()

    print("## Target")
    print()
    print(f"**{target}** — {target_node.data.get('statement', '').strip()}")
    print()
    if target_node.dependencies:
        print("Direct dependencies: " + ", ".join(f"`{x}`" for x in target_node.dependencies))
        print()

    assumptions = [
        node_id
        for node_id in closure
        if graph.nodes[node_id].node_type == "assumption"
    ]
    if assumptions:
        print("## Explicit assumptions")
        print()
        for node_id in sorted(assumptions, key=lambda x: (depths.get(x, 10**9), x)):
            node = graph.nodes[node_id]
            print(f"- **{node_id}**: {node.data.get('statement', '').strip()}")
        print()

    proof_claims = [
        node_id
        for node_id in _ordered_slice(graph, target, stop_at)
        if node_id != target and node_id in closure and graph.nodes[node_id].node_type == "claim"
    ]
    if proof_claims:
        print("## Proof obligations")
        print()
        for node_id in proof_claims:
            node = graph.nodes[node_id]
            data = node.data
            print(f"### `{node_id}` ({data.get('kind', 'claim')}, depth {depths[node_id]})")
            print()
            print(data.get("statement", "").strip())
            print()
            if node_id in stop_at:
                print("Cut point: this node is intentionally treated as opaque in this slice.")
                print()
                continue
            if node.dependencies:
                print("Depends on: " + ", ".join(f"`{x}`" for x in node.dependencies))
                print()
            if data.get("formal_intent"):
                print(f"Formal intent: `{data['formal_intent']}`")
                print()
            verification = data.get("verification")
            if isinstance(verification, dict):
                verifiers = verification.get("verifiers", [])
                if verifiers:
                    print("Planned evidence:")
                    for verifier in verifiers:
                        print(f"- `{verifier.get('kind', '?')}`: {verifier.get('intent', '')}")
                    print()

    if prompt and include_verification_task:
        print("## Verification task")
        print()
        print("Return one verdict: `SUPPORTED`, `REFUTED`, or `INCOMPLETE`.")
        print()
        print("- `SUPPORTED`: explain why the listed dependencies are sufficient for the target.")
        print("- `REFUTED`: give a concrete execution/counterexample consistent with the slice.")
        print("- `INCOMPLETE`: identify the smallest missing premise, edge, or proof obligation.")
        print("- Distinguish a false dependency from a missing dependency.")
        print("- Do not treat planned verifier descriptions as evidence that has already passed.")


def _cmd_slice(graph: Graph, args: argparse.Namespace) -> int:
    stop_at = set(args.stop_at or [])
    for node_id in stop_at:
        graph.require_node(node_id)
    graph.require_node(args.target)
    if args.format == "yaml":
        doc = _slice_document(graph, args.target, stop_at)
        print(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).rstrip())
    elif args.format == "prompt":
        _emit_slice_markdown(graph, args.target, stop_at, prompt=True)
    else:
        _emit_slice_markdown(graph, args.target, stop_at, prompt=False)
    return 0


def _cmd_invalidate(graph: Graph, args: argparse.Namespace) -> int:
    distance, via = graph.affected_by(args.changed)
    changed_set = set(args.changed)
    print("# Invalidation impact")
    print()
    print("Changed: " + ", ".join(f"`{x}`" for x in args.changed))
    print()
    print("This command is read-only: it computes candidates for re-verification and does not edit YAML statuses.")
    print()
    print("| Node | Distance | Reached via | Root? |")
    print("| --- | ---: | --- | --- |")
    for node_id in sorted(distance, key=lambda x: (distance[x], x)):
        parent = via[node_id]
        via_text = "direct change" if node_id in changed_set else f"`{parent}`"
        is_root = "yes" if node_id in graph.roots else ""
        print(f"| `{node_id}` | {distance[node_id]} | {via_text} | {is_root} |")
    impacted_roots = sorted(set(graph.roots) & set(distance))
    print()
    print("Impacted roots: " + (", ".join(f"`{x}`" for x in impacted_roots) or "none"))
    return 0



def _refinement_entry(graph: Graph, node_id: str) -> dict[str, Any]:
    refinement = graph.doc.get("refinement", {})
    if not isinstance(refinement, dict):
        raise SystemExit("refinement metadata is missing")
    nodes = refinement.get("nodes", {})
    if not isinstance(nodes, dict):
        raise SystemExit("refinement.nodes metadata is missing")
    entry = nodes.get(node_id)
    if not isinstance(entry, dict):
        raise SystemExit(f"No refinement metadata for claim: {node_id}")
    return entry


def _emit_non_normative_harness_feedback_contract() -> None:
    print()
    print(HARNESS_FEEDBACK_CONTRACT.rstrip())


def _emit_audit_prompt(graph: Graph, node_id: str) -> None:
    node = graph.require_node(node_id)
    if node.node_type != "claim":
        raise SystemExit("Only claim nodes can be audited by the refinement workflow")

    _refinement_entry(graph, node_id)  # ensure workflow metadata exists
    task, _priority = _audit_task_priority(graph.claims[node_id].get("kind"))

    print("# Clean-context correctness refinement audit")
    print()
    print(f"Assigned node: `{node_id}`")
    print(f"Task class: `{task}`")
    print()
    print("Isolation requirement: this audit must be performed in a fresh isolated sub-agent.")
    print("Do not use prior conversation context, earlier audit transcripts, or the reason this node was created.")
    print("This sub-agent is analysis-only: do not modify workspace files and do not invoke `mutate`.")
    print("Return reasoning/proposals to the orchestrator, which alone judges and compiles accepted changes.")
    print("Use only the generated proof slice below. Do not read correctness.yaml, README.md, the source")
    print("article, mutation history, prior audits, or sibling-agent results at any point in this audit.")
    print()

    # For a runnable proof audit, every direct claim dependency has already reached a scheduler
    # proof boundary (stable/waived). Treat those direct propositions as opaque premises instead
    # of recursively re-opening their proof closures. This keeps parent composition audits local
    # and avoids repeatedly paying for already-audited descendant reasoning.
    stop_at: set[str] = set()
    if task == "proof_audit":
        stop_at = {dep for dep in node.dependencies if dep in graph.claims}
    _emit_slice_markdown(graph, node_id, stop_at, prompt=True, include_verification_task=False)

    print()
    print("# Audit contract")
    print()
    print("This slice is the complete specification for the clean audit. Do not open the full DAG or source")
    print("article. The orchestrator handles graph reuse, invalidation analysis, and mutation compilation.")
    print()
    print("If you propose replacement proposition or verifier wording, write only the intended current semantics.")
    print("Do not embed audit/edit history such as 'previously', 'after the repair', or explanations of superseded wording.")
    print()

    contract_ids = node.data.get("semantic_contracts", []) or []
    if contract_ids:
        print("## Mandatory semantic-contract realization")
        print()
        print("A semantic contract is a reusable property specification, not a theorem or proof premise. Referencing")
        print("one in `semantic_contracts` means this target itself asserts a full realization of that property")
        print("across at least the contract's declared semantic scope; merely depending on another claim that")
        print("realizes the property is not sufficient.")
        print()
        print("For every semantic contract referenced by the target, explicitly decide whether the authoritative")
        print("target proposition fully realizes that property specification. `REALIZES` means the target proposition")
        print("entails the contract's complete truth condition across its declared class, minimum observation scope,")
        print("identity/provenance coordinates, and exclusions. The target may impose additional requirements; exact")
        print("natural-language equivalence is not required. `WEAKER` means there exists an execution allowed by the")
        print("target but forbidden by the contract. Use `MISMATCH` for another semantic incompatibility and")
        print("`INCOMPLETE` only when this generated slice is insufficient to decide.")
        print()
        print("For `state_invariant`, a path-local statement such as 'during reconnect' does NOT realize an")
        print("`all_reachable_states` contract unless all other reachable states are explicitly excluded. For")
        print("`provenance_binding`, locally correct processing does not realize the contract unless the required")
        print("source/capture/binding relation itself is guaranteed.")
        print()
        print("Any referenced contract whose judgment is not `REALIZES` is a refinement boundary defect: the")
        print("current node MUST NOT be certified stable as written. Report `CONTRACT_MISMATCH` unless the repair")
        print("requires a genuinely new product/architecture choice, in which case use `HUMAN_SEMANTIC_DECISION`.")
        print()

        repeated_carriers = []
        target_contracts = set(contract_ids)
        for dependency_id in node.dependencies:
            dependency = graph.claims.get(dependency_id)
            if not isinstance(dependency, dict):
                continue
            overlap = sorted(target_contracts & set(dependency.get("semantic_contracts", []) or []))
            for contract_id in overlap:
                repeated_carriers.append((contract_id, dependency_id))
        if repeated_carriers:
            print("## Repeated semantic-contract carrier diagnostic")
            print()
            print("The target and one or more direct dependency claims both declare realization of the same")
            print("semantic property. This is not automatically wrong: a root may intentionally restate the same")
            print("system property at a higher abstraction boundary. But semantic contracts must not be propagated")
            print("upward merely because the target consumes a premise that realizes them.")
            for contract_id, dependency_id in repeated_carriers:
                print(f"- `{contract_id}` is also realized by direct dependency `{dependency_id}`")
            print("For each repeated carrier, decide whether the target independently asserts the contract's full")
            print("truth condition/minimum scope, or merely uses the dependency's established property. In the latter")
            print("case, recommend removing the target's redundant semantic-contract association rather than")
            print("strengthening the target solely to preserve duplicated metadata.")
            print()

    if task == "leaf_boundary_audit":
        print("## Question")
        print()
        print("Decide whether this is already a local mechanical verification boundary. A leaf need not be")
        print("logically atomic: if one localized deterministic/mechanical verifier could establish the complete")
        print("proposition directly, default to GOOD_LEAF even when that verifier would check several assertions.")
        print("Canonical tool-level stopping signals are:")
        for key, description in LEAF_STOPPING_SIGNALS.items():
            print(f"- `{key}`: {description}")
        print("The eventual evidence may come directly from code/config analysis rather than LLM reasoning; that")
        print("is a reason to STOP decomposition, not continue it.")
        print("Try to make distinct parts fail independently, but independent falsifiability alone is insufficient.")
        print("Split only when separate nodes create materially distinct verifier/invalidation/ownership or reusable")
        print("proof boundaries; do not create one leaf per assertion or test scenario.")
        print("Verdicts: `GOOD_LEAF`, `SHOULD_DECOMPOSE`, `CONTRACT_MISMATCH`, `HUMAN_SEMANTIC_DECISION`,")
        print("`TOOLING_BLOCKED`, or `INCOMPLETE_CONTEXT`. For decomposition, explain the smallest useful children in prose.")
    else:
        print("## Question")
        print()
        print("Try to refute the target, then decide whether its direct dependency propositions are sufficient")
        print("and no stronger than necessary. Classify each direct dependency as `necessary`,")
        print("`useful_but_stronger`, `redundant`, or `unrelated`.")
        print("Verdicts: `SUPPORTED`, `REFUTED`, `INCOMPLETE`, `CONTRACT_MISMATCH`, `HUMAN_SEMANTIC_DECISION`, or `TOOLING_BLOCKED`.")

    print()
    print("Before requesting a human decision, distinguish a genuinely unspecified architecture/product choice from")
    print("ambiguous or over-broad target wording and from an artifact of over-decomposition. If the slice already")
    print("determines the intended semantics, report the specification defect precisely instead of inventing several")
    print("new product options.")
    print("Escalate instead of inventing semantics: use `human_decision` for root-guarantee, assumption,")
    print("failure-model, source-of-truth, horizon, or architecture/product choices not determined by the slice.")
    print("In particular, do not invent a new state variable, lock, phase, generation, queue, reservation, or")
    print("transaction primitive to make the proof close. Missing architecture mechanism => human decision.")
    print("Use `tooling_blocker` only when correctness tooling itself prevents faithful analysis/refinement.")
    print()
    print("## Response format")
    print()
    print("Reason freely in prose and lead with the strongest correctness argument/counterexample. Do not emit")
    print("mutation operations or allocated node IDs. At the very end, emit one small YAML verdict block:")
    print()
    print("```yaml")
    print(f"node: {node_id}")
    print(f"task: {task}")
    print("verdict: <allowed verdict>")
    print("summary: <concise main finding>")
    print("strongest_counterexample: <concise execution or null>")
    print("dependency_findings:")
    print("  - dependency: <node-id>")
    print("    judgment: <necessary|useful_but_stronger|redundant|unrelated>")
    print("contract_realizations:")
    print("  - contract: <semantic-contract-id>")
    print("    judgment: <REALIZES|WEAKER|MISMATCH|INCOMPLETE>")
    print("    reason: <concise entailment/counterexample rationale>")
    print("defects: []  # or relevant defect classes")
    print("human_decision:")
    print("  required: <true|false>")
    print("  question: <concise question or null>")
    print("  reason: <concise reason or null>")
    print("tooling_blocker:")
    print("  required: <true|false>")
    print("  issue: <concise issue or null>")
    print("  reason: <concise impact or null>")
    print("  suggested_change: <concise repair or null>")
    print("```")
    print()
    print("Use empty lists when applicable, including `contract_realizations: []` when the target references no")
    print("semantic contracts, and set both `required` flags explicitly. The orchestrator may compile a stable")
    print("mutation only when every currently referenced semantic contract is listed with judgment `REALIZES`.")
    _emit_non_normative_harness_feedback_contract()


def _cmd_audit_prompt(graph: Graph, args: argparse.Namespace) -> int:
    graph.require_node(args.node)
    snapshot = _refinement_snapshot(graph)
    runnable_ids = {item["node"] for item in snapshot["runnable"]}
    if snapshot.get("state") == "TOOLING_BLOCKED":
        raise SystemExit(
            "Automated campaign is TOOLING_BLOCKED by: "
            + ", ".join(snapshot.get("open_tooling_blockers", []))
            + ". Repair/resolve tooling before generating further audit tasks."
        )
    if args.node not in runnable_ids:
        blocked = next((x for x in snapshot["blocked_frontier"] if x["node"] == args.node), None)
        waiting = next((x for x in snapshot["waiting"] if x["node"] == args.node), None)
        if blocked:
            raise SystemExit(
                f"Node {args.node} is human-blocked by: {', '.join(blocked['blocked_by'])}. "
                "Do not audit it until the decision is resolved."
            )
        if waiting:
            raise SystemExit(
                f"Node {args.node} is not runnable; unresolved claim dependencies: "
                + ", ".join(waiting["waiting_on"])
            )
        entry = _refinement_entry(graph, args.node)
        status = entry.get("status")
        raise SystemExit(f"Node {args.node} is not runnable (refinement status={status})")
    _emit_audit_prompt(graph, args.node)
    return 0




COMPOSITION_AUDIT_FOCUS: dict[str, str] = {
    "general": (
        "Search broadly for any allowed execution in which every direct dependency is exactly true "
        "but the target proposition is false. Prefer the smallest concrete counterexample."
    ),
    "execution": (
        "Act as an execution counterexample generator. Concentrate on concurrency, delay, retry, crash, "
        "recovery, observer, and state-transition paths that keep every direct dependency true while "
        "falsifying the target."
    ),
    "quantifier": (
        "Act as a quantifier/boundary auditor. Concentrate on forall-vs-exists mismatches, same-vs-some "
        "request/key/document distinctions, observer boundaries, captured frontier/head mismatches, "
        "before/after timing, and per-attempt versus cross-attempt semantics."
    ),
    "premise": (
        "Act as a hidden-premise auditor. Look for facts used by the target implication that are not "
        "actually guaranteed by any direct dependency, including vacuous antecedents and identity/provenance "
        "bridges that the prose may be assuming implicitly."
    ),
}


def _recommended_composition_auditors(claim: dict[str, Any]) -> int:
    """Return the fixed independent-auditor count for semantic composition certification."""
    kind = claim.get("kind")
    if kind == "root":
        return 3
    if kind == "derived":
        return 2
    raise SystemExit("Composition assurance applies only to root/derived claims")


def _composition_campaign_snapshot(graph: Graph) -> dict[str, Any]:
    refinement = _refinement_snapshot(graph)
    assurance = _assurance_snapshot(graph)
    stable = set(refinement.get("stable", []))
    waived = set(refinement.get("waived", []))
    eligible = stable | waived

    items: list[dict[str, Any]] = []
    for node_id, claim in sorted(graph.claims.items()):
        if claim.get("kind") not in {"root", "derived"}:
            continue
        assurance_item = assurance["composition"].get(node_id, {})
        effective = assurance_item.get("effective", "unaudited")
        if effective not in {"unaudited", "stale", "single_agent_audited"}:
            continue
        if node_id not in eligible:
            continue
        items.append(
            {
                "node": node_id,
                "kind": claim.get("kind"),
                "effective": effective,
                "composition_signature": _composition_semantic_signature(graph, node_id),
                "recommended_auditors": _recommended_composition_auditors(claim),
                "suggested_focuses": (
                    ["execution", "quantifier"]
                    if _recommended_composition_auditors(claim) == 2
                    else ["execution", "quantifier", "premise"]
                    if _recommended_composition_auditors(claim) == 3
                    else ["execution", "quantifier", "premise", "general"]
                ),
                "priority": 300 if claim.get("kind") == "root" else 200,
            }
        )
    items.sort(key=lambda x: (x["priority"], x["node"]))

    if refinement.get("state") == "TOOLING_BLOCKED":
        state = "TOOLING_BLOCKED"
    elif refinement.get("state") != "COMPLETE":
        state = "REFINEMENT_INCOMPLETE"
    elif items:
        state = "CONTINUE"
    else:
        state = "COMPLETE"
    return {
        "state": state,
        "model_signature": assurance["model_signature"],
        "refinement_state": refinement.get("state"),
        "target_assurance": "multi_agent_audited_or_machine_checked",
        "runnable": items if state == "CONTINUE" else [],
        "counts": {
            "runnable": len(items) if state == "CONTINUE" else 0,
            "unaudited": sum(1 for x in assurance["composition"].values() if x.get("effective") == "unaudited"),
            "stale": sum(1 for x in assurance["composition"].values() if x.get("effective") == "stale"),
            "single_agent_audited": sum(1 for x in assurance["composition"].values() if x.get("effective") == "single_agent_audited"),
            "multi_agent_audited": sum(1 for x in assurance["composition"].values() if x.get("effective") == "multi_agent_audited"),
            "machine_checked": sum(1 for x in assurance["composition"].values() if x.get("effective") == "machine_checked"),
        },
    }


def _cmd_composition_status(graph: Graph, args: argparse.Namespace) -> int:
    snapshot = _composition_campaign_snapshot(graph)
    if args.format in {"yaml", "compact-yaml"}:
        print(yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    print(f"state={snapshot['state']} refinement_state={snapshot['refinement_state']}")
    counts = snapshot["counts"]
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    if snapshot["runnable"]:
        print("\nComposition certification frontier:")
        for item in snapshot["runnable"]:
            print(
                f"- {item['node']} kind={item['kind']} "
                f"effective={item['effective']} recommended_auditors={item['recommended_auditors']} "
                f"focuses={','.join(item['suggested_focuses'])} signature={item['composition_signature']}"
            )
    return 0


def _emit_composition_audit_prompt(graph: Graph, node_id: str, focus: str) -> None:
    node = graph.require_node(node_id)
    if node.node_type != "claim" or node.data.get("kind") not in {"root", "derived"}:
        raise SystemExit("Composition certification applies only to root/derived claims")

    direct = list(node.dependencies)
    print("# Clean-context proof-composition certification")
    print()
    print(f"Assigned node: `{node_id}`")
    print(f"Composition signature: `{_composition_semantic_signature(graph, node_id)}`")
    print(f"Focus: `{focus}`")
    print()
    print("Isolation requirement: perform this audit in a fresh context. Use only this generated contract.")
    print("Do not read the full DAG, source article, history, prior audits, sibling-agent results, or old chat context.")
    print("Do not audit whether the direct dependencies are themselves true and do not inspect their grandchildren.")
    print("Assume every direct dependency proposition below is exactly true, then try to make the target false.")
    print()
    _emit_system_context(graph, [node_id, *direct])
    print("## Target")
    print()
    print(node.data.get("statement", "").strip())
    print()
    print("## Direct premises — assume all are true")
    print()
    for dep_id in direct:
        dep = graph.require_node(dep_id)
        dep_type = "assumption" if dep.node_type == "assumption" else dep.data.get("kind", "claim")
        print(f"### `{dep_id}` ({dep_type})")
        print(dep.data.get("statement", "").strip())
        print()
    print("## Adversarial task")
    print()
    print(COMPOSITION_AUDIT_FOCUS[focus])
    print()
    print("The only soundness question is: can all direct premises remain true while the target is false?")
    print("A desirable stronger guarantee is not a defect. A counterexample is valid only if it stays within the")
    print("provided scope/failure model and preserves every direct premise exactly as written.")
    print("Do not repair the graph, invent architecture, or suggest decomposition unless needed to explain the missing premise.")
    print()
    print("If you name a missing proposition, phrase it as the required current-state semantics, not as edit history or a correction of superseded wording.")
    print("Verdicts: `ENTAILED`, `COUNTEREXAMPLE`, or `INCOMPLETE_CONTEXT`.")
    print()
    print("## Response format")
    print("```yaml")
    print(f"node: {node_id}")
    print("task: composition_certification")
    print(f"focus: {focus}")
    print("verdict: <ENTAILED|COUNTEREXAMPLE|INCOMPLETE_CONTEXT>")
    print("summary: <concise conclusion>")
    print("counterexample:")
    print("  initial_state: <state or null>")
    print("  execution: <execution or null>")
    print("  bad_state: <bad state or null>")
    print("why_direct_premises_still_hold:")
    print("  - premise: <node-id>")
    print("    reason: <why it remains true>")
    print("missing_premise: <smallest missing proposition or null>")
    print("```")
    _emit_non_normative_harness_feedback_contract()


def _cmd_composition_audit_prompt(graph: Graph, args: argparse.Namespace) -> int:
    refinement = _refinement_snapshot(graph)
    if refinement.get("state") == "TOOLING_BLOCKED":
        raise SystemExit("Composition certification is disabled while refinement tooling is TOOLING_BLOCKED")
    if refinement.get("state") != "COMPLETE":
        raise SystemExit(
            f"Composition certification requires a frozen refinement graph; current state={refinement.get('state')}"
        )
    node = graph.require_node(args.node)
    if node.node_type != "claim" or node.data.get("kind") not in {"root", "derived"}:
        raise SystemExit("Composition certification applies only to root/derived claims")
    _emit_composition_audit_prompt(graph, args.node, args.focus)
    return 0


def _emit_coverage_audit_prompt(graph: Graph) -> None:
    assurance = _assurance_snapshot(graph)
    refinement = _refinement_snapshot(graph)
    print("# Final root-coverage / specification-completeness audit")
    print()
    print(f"Model signature: `{assurance['model_signature']}`")
    print(f"Coverage-audit semantics: `{COVERAGE_AUDIT_SEMANTICS_VERSION}`")
    print(f"Refinement state: `{refinement['state']}`")
    print()
    print("You are the long-lived orchestrator for an architecture-level adversarial audit of the current model.")
    print("This is not proof refinement and not implementation verification. Make no repository modifications.")
    print("Use only the current canonical repository state; do not read history/, prior audit outputs, old chat context, or Git history.")
    print()
    print("## Central question")
    print()
    print("Assume every current claim, assumption, and declared dependency implication is true. Can the system still")
    print("produce a material, clearly incorrect outcome inside the explicit scope and allowed failure model?")
    print("Also check semantic alignment: have roots, scope exclusions, assumptions, or quantifiers silently weakened")
    print("a guarantee that the canonical design actually commits to?")
    print()
    print("A stronger but optional product guarantee is not a gap. If no concrete in-scope all-roots-true counterexample")
    print("survives, terminate with `FINAL VERDICT: CLOSED`; do not continue refinement for its own sake.")
    print()
    print("## Canonical inputs")
    print()
    repository_root = str(graph.repository_root) if graph.repository_root is not None else "<repository-root>"
    source = graph.doc.get("source", {})
    source_article = source.get("article", "<source.article>") if isinstance(source, dict) else "<source.article>"
    correctness_dir = str((graph.repository_root / "correctness")) if graph.repository_root is not None else "<repository-root>/correctness"
    print(f"Repository root: `{repository_root}`")
    print(f"Correctness directory: `{correctness_dir}`")
    print(f"Canonical design: `{source_article}`")
    print("Canonical model: `correctness/correctness.yaml`")
    print()
    print("Bootstrap with:")
    print("```bash")
    print(f"cd {correctness_dir}")
    print(".venv/bin/python3 correctness.py validate")
    print(".venv/bin/python3 correctness.py refinement-status --format compact-yaml")
    print(".venv/bin/python3 correctness.py assurance-status --format compact-yaml --omit-specification-coverage")
    print("```")
    print(f"Read `../{source_article}` for canonical intended semantics. For a root-local check, use")
    print("`.venv/bin/python3 correctness.py slice <ROOT_ID> --format prompt`; stable roots are intentionally inspected via slice,")
    print("not refinement `audit-prompt`.")
    print()
    print("## Current roots")
    print()
    for root_id in graph.roots:
        print(f"- `{root_id}` — {graph.claims[root_id].get('statement', '').strip()}")
    print()
    print("## Explicit assumptions")
    print()
    for assumption_id, assumption in sorted(graph.assumptions.items()):
        print(f"- `{assumption_id}` — {assumption.get('statement', '').strip()}")
    print()
    print("## Mandatory semantic-closure pass")
    print()
    print("Before free-form scouting, perform two structured completeness passes against the canonical design and typed model.")
    print("These passes search for missing propositions/contracts; they do not assume that existing claim wording is complete.")
    print()
    print("1. **Inductive state-invariant closure**")
    print("   - Inventory design-critical relations among authoritative, derived, speculative, and observer-visible state.")
    print("   - For each relation, identify its observation scope, establishment/publication boundary, and every in-scope transition class capable of mutating any participating state symbol.")
    print("   - If the intended property must hold at every state in that scope, require a `state_invariant` semantic contract plus a claim whose wording is genuinely inductive. A guarantee attached only to named events or frontier-advancing transitions is insufficient unless other mutators are explicitly excluded.")
    print("   - Look especially for paired-state coherence, visibility suppression, mode/state compatibility, and representation/frontier relations.")
    print()
    print("2. **Semantic provenance/binding closure**")
    print("   - Inventory every token/field/state whose downstream meaning depends on where or when it was produced (for example frontiers, epochs, request bases, identities, snapshot heads, accepted evidence).")
    print("   - For each one, identify producer, required source state, capture/linearization point, bound execution/identity/state, and downstream consumers.")
    print("   - For composite identities or semantic tuples, require tuple closure: every coordinate that distinguishes the entity must be bound to the same source/execution. Separately correct field-level provenance does not prove that `(document_id, key)`, `(document_id, frontier)`, or an authoring-state/request tuple refers to one semantic entity.")
    print("   - Require a `provenance_binding` semantic contract plus a claim when correctness depends on that origin/binding relation. Correct downstream processing of a submitted or captured value does not prove that the value came from the right source.")
    print("   - Search explicitly for stale, substituted, cross-bound, cached, or otherwise semantically valid-looking values with the wrong provenance.")
    print()
    print("A missing typed contract alone is not automatically a structural gap if the design does not require the relation, but any design-critical relation that current roots can leave false is a serious candidate for the normal checker/challenger gate below.")
    print()
    print("## Multi-agent campaign")
    print()
    print("Isolation is an execution requirement, not a role-playing convention. When Writer MCP exposes")
    print("`spawn_chatgpt_subagents`, the orchestrator MUST use it for every scout, checker, and challenger (use a one-element list when launching a single fresh agent).")
    print("If it is absent from the initially loaded tool subset, perform Writer-tool discovery before declaring")
    print("clean-context execution unavailable. Do not reuse this orchestrator context as a substitute for a fresh child conversation.")
    print("If all browser slots are occupied or a child task fails, treat that as transient capacity/execution failure:")
    print("finish, reap, or cancel existing child tasks as appropriate and retry when a slot is available.")
    print("Do not report missing clean-context capability merely because one spawn attempt could not run.")
    print("If required isolation still cannot be obtained, stop without a coverage certification and return")
    print("`FINAL VERDICT: EXECUTION INCOMPLETE`; do not record specification coverage from that run.")
    print()
    print("Spawn fresh isolated scouts with non-overlapping attack surfaces:")
    print("- Scout A — end-to-end semantic pipeline: submission → acceptance → delivery/reconnect → visible client state.")
    print("- Scout B — authoritative state/provenance: accepted history, identity, fencing, snapshot/recovery, compaction.")
    print("- Scout C — client observer semantics: reconnect, overlapping attempts, frontier continuity, optimistic identity reconciliation.")
    print("- Scout D — all-roots-true execution generator: deliberately search for executions where every root remains literally true.")
    print()
    print("Each candidate must include: initial state, concrete execution, material bad state, why every relevant root can")
    print("remain true, why the execution is in scope, and the nearest root(s). Do not accept node-name intuition.")
    print()
    print("For each serious candidate, run a fresh local checker against the nearest canonical root slice and classify it as")
    print("`COVERED`, `BOUNDARY_DEFECT`, or `ORTHOGONAL`. Then run a fresh challenger whose only job is to kill the")
    print("candidate by showing that it violates an existing claim/assumption, relies on an excluded failure, or asks for an")
    print("out-of-scope/stronger guarantee.")
    print()
    print("## Acceptance gate for a structural finding")
    print()
    print("Report `STRUCTURAL GAP FOUND` only if there is a concrete execution such that:")
    print("1. every current root and assumption can remain true;")
    print("2. the execution uses only allowed failures;")
    print("3. the bad outcome is inside the explicit scope and material;")
    print("4. the canonical design treats the outcome as incorrect;")
    print("5. the candidate survives a challenger.")
    print()
    print("Otherwise terminate with `FINAL VERDICT: CLOSED`.")
    print("If mandatory fresh-context execution could not be completed, issue neither CLOSED nor STRUCTURAL GAP FOUND;")
    print("terminate with `FINAL VERDICT: EXECUTION INCOMPLETE` and do not mutate specification-coverage assurance.")
    print()
    print("## Final response")
    print()
    print("Return the baseline validator/scheduler state, a compact scout/checker/challenger ledger, each surviving finding")
    print("with a minimal repair boundary, and exactly one final verdict. Include the model signature above so the result")
    print("can later be recorded through `set_specification_coverage` without ambiguity.")
    print("Describe proposed canonical wording as present-tense/current-state semantics only; do not encode audit history or superseded wording into claims/contracts.")
    _emit_non_normative_harness_feedback_contract()


def _cmd_coverage_audit_prompt(graph: Graph, args: argparse.Namespace) -> int:
    del args
    errors, warnings = validate(graph)
    if errors or warnings:
        raise SystemExit("Coverage audit requires VALID with 0 errors and 0 warnings")
    refinement = _refinement_snapshot(graph)
    if refinement.get("state") != "COMPLETE":
        raise SystemExit(
            f"Coverage audit requires refinement COMPLETE; current state={refinement.get('state')}"
        )
    _emit_coverage_audit_prompt(graph)
    return 0


def _cmd_workflow_help(_graph: Graph | None, args: argparse.Namespace) -> int:
    del args
    print(WORKFLOW_HELP.rstrip())
    return 0


def _cmd_catalog(graph: Graph, args: argparse.Namespace) -> int:
    entries = _catalog_namespace(graph, args.namespace)
    if args.key is not None:
        if args.key not in entries:
            raise SystemExit(f"Unknown catalog entry: {args.namespace}.{args.key}")
        payload: Any = {args.key: entries[args.key]}
    else:
        payload = entries
    if args.format == "yaml":
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    for key, entry in payload.items():
        description = entry.get("description", "") if isinstance(entry, dict) else str(entry)
        extra = ""
        if args.namespace == "state" and isinstance(entry, dict):
            extra = f" [{entry.get('class', '?')}]"
        if args.namespace == "mechanisms" and isinstance(entry, dict):
            extra = f" [automation_reusable={str(entry.get('automation_reusable')).lower()}]"
        if args.namespace == "semantic_contracts" and isinstance(entry, dict):
            description = entry.get("definition", "")
            extra = (
                f" [{entry.get('class', '?')}; "
                f"automation_reusable={str(entry.get('automation_reusable')).lower()}]"
            )
        if args.namespace == "sources" and isinstance(entry, dict):
            description = entry.get("heading", "")
        print(f"{key}{extra}: {description}")
    return 0


def _cmd_graph_schema(_graph: Graph | None, args: argparse.Namespace) -> int:
    if args.format == "json":
        print(json.dumps(CANONICAL_GRAPH_SCHEMA, indent=2, ensure_ascii=False, sort_keys=False))
    else:
        print(yaml.safe_dump(_plain_data(CANONICAL_GRAPH_SCHEMA), sort_keys=False, allow_unicode=True).rstrip())
    return 0


def _cmd_formal_schema(_graph: Graph | None, args: argparse.Namespace) -> int:
    schema = FORMAL_SCHEMAS[args.kind]
    if args.format == "json":
        print(json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=False))
    else:
        print(yaml.safe_dump(_plain_data(schema), sort_keys=False, allow_unicode=True).rstrip())
    return 0


def _cmd_schema_fields(_graph: Graph | None, args: argparse.Namespace) -> int:
    inventory = _canonical_field_inventory()
    if args.format == "yaml":
        print(yaml.safe_dump(inventory, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    for row in inventory:
        print(f"{row['path']} [{row['presence']}; {row['constraint']}] — {row['description']}")
    print()
    print(f"{len(inventory)} canonical field paths")
    return 0


def _cmd_mutation_schema(_graph: Graph | None, args: argparse.Namespace) -> int:
    if args.format == "json":
        print(json.dumps(MUTATION_PLAN_SCHEMA, indent=2, ensure_ascii=False, sort_keys=False))
    else:
        print(yaml.safe_dump(_plain_data(MUTATION_PLAN_SCHEMA), sort_keys=False, allow_unicode=True).rstrip())
    return 0


def _cmd_refinement_status(graph: Graph, args: argparse.Namespace) -> int:
    snapshot = _refinement_snapshot(graph)
    snapshot["counts"] = {
        "runnable": len(snapshot["runnable"]),
        "blocked_frontier": len(snapshot["blocked_frontier"]),
        "waiting": len(snapshot["waiting"]),
        "stable": len(snapshot["stable"]),
        "waived": len(snapshot["waived"]),
        "stale": len(snapshot["stale"]),
        "open_decisions": len(snapshot["open_decisions"]),
        "open_tooling_blockers": len(snapshot.get("open_tooling_blockers", [])),
    }
    if args.format == "yaml":
        print(yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    if args.format == "compact-yaml":
        compact = {
            "state": snapshot["state"],
            # Runnable items retain task/priority/signature because the orchestrator needs
            # them for dispatch and semantic-OCC mutation preconditions. Non-runnable
            # items keep only the fields needed to explain why they cannot run yet.
            "runnable": snapshot["runnable"],
            "blocked_frontier": [
                {"node": item["node"], "blocked_by": item.get("blocked_by", [])}
                for item in snapshot["blocked_frontier"]
            ],
            "waiting": [
                {"node": item["node"], "waiting_on": item.get("waiting_on", [])}
                for item in snapshot["waiting"]
            ],
            "stale": snapshot["stale"],
            "open_decisions": snapshot["open_decisions"],
            "open_tooling_blockers": snapshot.get("open_tooling_blockers", []),
            "decision_impacts": snapshot["decision_impacts"],
            "counts": snapshot["counts"],
        }
        print(yaml.safe_dump(compact, sort_keys=False, allow_unicode=True).rstrip())
        return 0

    print(f"state={snapshot['state']}")
    counts = snapshot["counts"]
    print(
        " ".join(
            f"{key}={counts[key]}"
            for key in (
                "runnable",
                "blocked_frontier",
                "waiting",
                "stable",
                "waived",
                "stale",
                "open_decisions",
                "open_tooling_blockers",
            )
        )
    )
    if snapshot["runnable"]:
        print("\nRunnable frontier:")
        for item in snapshot["runnable"]:
            print(
                f"- {item['node']} task={item['task']} priority={item['priority']} "
                f"signature={item['signature']}"
            )
    if snapshot["blocked_frontier"]:
        print("\nHuman-blocked frontier:")
        for item in snapshot["blocked_frontier"]:
            print(
                f"- {item['node']} blocked_by={','.join(item['blocked_by'])} "
                f"signature={item['signature']}"
            )
    if snapshot["waiting"]:
        print("\nWaiting on unresolved claim dependencies:")
        for item in snapshot["waiting"]:
            print(f"- {item['node']} waiting_on={','.join(item['waiting_on'])}")
    if snapshot["open_decisions"]:
        print("\nOpen human decisions: " + ", ".join(snapshot["open_decisions"]))
        for decision_id in snapshot["open_decisions"]:
            impact = snapshot["decision_impacts"].get(decision_id, {})
            print(
                f"  - {decision_id}: affected_pending={impact.get('affected_pending_count', 0)} "
                f"affected_roots={','.join(impact.get('affected_roots', [])) or 'none'}"
            )
    if snapshot.get("open_tooling_blockers"):
        print("\nOpen tooling blockers: " + ", ".join(snapshot["open_tooling_blockers"]))
        blockers = graph.doc.get("refinement", {}).get("tooling_blockers", {})
        for blocker_id in snapshot["open_tooling_blockers"]:
            blocker = blockers.get(blocker_id, {}) if isinstance(blockers, dict) else {}
            print(f"  - {blocker_id}: {blocker.get('issue', '')}")
    if snapshot["stale"]:
        print("\nStale stable audits automatically reopened: " + ", ".join(snapshot["stale"]))
    return 0


def _cmd_assurance_status(graph: Graph, args: argparse.Namespace) -> int:
    snapshot = _assurance_snapshot(graph)
    omit_specification_coverage = bool(getattr(args, "omit_specification_coverage", False))
    if args.format == "yaml":
        rendered = dict(snapshot)
        if omit_specification_coverage:
            rendered.pop("specification_coverage", None)
        print(yaml.safe_dump(rendered, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    if args.format == "compact-yaml":
        compact = {
            "model_signature": snapshot["model_signature"],
            **({} if omit_specification_coverage else {"specification_coverage": snapshot["specification_coverage"]}),
            "counts": snapshot["counts"],
            "stale_composition": sorted(
                node_id for node_id, item in snapshot["composition"].items()
                if item["effective"] == "stale"
            ),
            "stale_implementation_evidence": sorted(
                node_id for node_id, item in snapshot["implementation_evidence"].items()
                if item["effective"] == "stale"
            ),
            "roots": snapshot["roots"],
        }
        print(yaml.safe_dump(compact, sort_keys=False, allow_unicode=True).rstrip())
        return 0

    spec = snapshot["specification_coverage"]
    print(f"model_signature={snapshot['model_signature']}")
    if not omit_specification_coverage:
        print(
            "specification_coverage "
            f"declared={spec['declared']} effective={spec['effective']}"
        )
    print("composition " + " ".join(
        f"{key}={value}" for key, value in snapshot["counts"]["composition"].items()
    ))
    print("implementation_evidence " + " ".join(
        f"{key}={value}" for key, value in snapshot["counts"]["implementation_evidence"].items()
    ))
    print("\nRoot assurance summary:")
    for root_id, item in snapshot["roots"].items():
        evidence = ",".join(f"{k}:{v}" for k, v in item["leaf_evidence_counts"].items()) or "none"
        print(
            f"- {root_id}: root_composition={item['root_composition']} "
            f"composition_complete={str(item['composition_complete']).lower()} "
            f"leaf_evidence={evidence} evidence_complete={str(item['evidence_complete']).lower()}"
        )
    return 0


def _cmd_refinement_signature(graph: Graph, args: argparse.Namespace) -> int:
    node = graph.require_node(args.node)
    if node.node_type != "claim":
        raise SystemExit("Refinement signatures are tracked only for claims")
    print(_node_semantic_signature(graph, args.node))
    return 0




@contextmanager
def _exclusive_graph_lock():
    """Serialize all correctness.yaml writers across processes."""
    lock_path = GRAPH_PATH.with_suffix(GRAPH_PATH.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _rt_yaml():
    try:
        from ruamel.yaml import YAML
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "ruamel.yaml is required for mutation. Install with: "
            "python3 -m pip install -r requirements.txt"
        ) from exc
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 100
    rt.indent(mapping=2, sequence=4, offset=2)
    return rt


def _format_jsonschema_path(parts: Iterable[Any]) -> str:
    rendered = "$"
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += "." + str(part)
    return rendered


def _validate_mutation_plan_schema(plan: dict[str, Any]) -> None:
    errors = sorted(
        _MUTATION_PLAN_VALIDATOR.iter_errors(plan),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if not errors:
        return
    details: list[str] = []
    for error in errors[:20]:
        path = _format_jsonschema_path(error.absolute_path)
        details.append(f"{path}: {error.message}")
    if len(errors) > 20:
        details.append(f"... {len(errors) - 20} additional schema error(s)")
    raise SystemExit(
        "MUTATION_SCHEMA_REJECTED: mutation plan does not match canonical schema:\n- "
        + "\n- ".join(details)
    )


def _load_mutation_plan(path_text: str) -> dict[str, Any]:
    if path_text == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path_text).read_text(encoding="utf-8")
    try:
        plan = yaml.load(raw, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid mutation plan YAML:\n{exc}") from exc
    if not isinstance(plan, dict):
        raise SystemExit("Mutation plan must be a YAML mapping")
    _validate_mutation_plan_schema(plan)
    return plan


def _mutation_prefix_for_kind(kind: str) -> str:
    mapping = {"root": "G", "derived": "C", "leaf": "L"}
    try:
        return mapping[kind]
    except KeyError as exc:
        raise SystemExit(f"Unsupported claim kind for allocation: {kind}") from exc


def _validate_slug(slug: Any) -> str:
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", slug):
        raise SystemExit(
            f"Invalid slug {slug!r}; use lower snake_case without a G/C/L/A/D/T prefix or sequence"
        )
    return slug


def _allocate_id(doc: dict[str, Any], prefix: str, slug: str) -> str:
    allocator = doc.get("id_allocator")
    if not isinstance(allocator, dict) or not isinstance(allocator.get("next_sequence"), dict):
        raise SystemExit("id_allocator.next_sequence is missing; cannot allocate IDs")
    counters = allocator["next_sequence"]
    sequence = counters.get(prefix)
    if not isinstance(sequence, int) or sequence < 1:
        raise SystemExit(f"Invalid next_sequence for prefix {prefix}")
    node_id = f"{prefix}{sequence}_{_validate_slug(slug)}"
    counters[prefix] = sequence + 1
    return node_id


def _resolve_ref(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("@"):
        alias = value[1:]
        if alias not in aliases:
            raise SystemExit(f"Unknown mutation alias: {value}")
        return aliases[alias]
    if isinstance(value, list):
        return [_resolve_ref(v, aliases) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_ref(v, aliases) for k, v in value.items()}
    return value


def _refinement_defaults(kind: str) -> dict[str, Any]:
    _audit_task_priority(kind)  # validate the role; task/priority themselves are derived at read time
    return {"status": "pending", "blocked_by": []}


def _assurance_default_for_kind(kind: str) -> tuple[str, dict[str, Any]]:
    if kind == "leaf":
        return "implementation_evidence", {"status": "planned"}
    if kind in {"root", "derived"}:
        return "composition", {"status": "unaudited"}
    raise SystemExit(f"Unsupported claim kind for assurance: {kind}")


def _rewrite_claim_reference(doc: dict[str, Any], old_id: str, new_id: str) -> None:
    claims = doc.get("claims", {})
    if isinstance(claims, dict):
        for claim in claims.values():
            if not isinstance(claim, dict):
                continue
            deps = claim.get("depends_on")
            if isinstance(deps, list):
                claim["depends_on"] = [new_id if dep == old_id else dep for dep in deps]
    roots = doc.get("roots")
    if isinstance(roots, list):
        doc["roots"] = [new_id if node_id == old_id else node_id for node_id in roots]


def _assert_automation_reusable_semantic_refs(
    doc: dict[str, Any],
    body: dict[str, Any],
    *,
    previous: dict[str, Any] | None = None,
    operation_label: str,
) -> None:
    """Reject automation-introduced architecture/semantic references that were not human-approved."""
    previous = previous or {}
    catalog = doc.get("catalog", {}) if isinstance(doc.get("catalog"), dict) else {}
    for field, namespace, noun in (
        ("mechanisms", "mechanisms", "mechanism"),
        ("semantic_contracts", "semantic_contracts", "semantic contract"),
    ):
        current_refs = body.get(field, []) or []
        previous_refs = set(previous.get(field, []) or [])
        if not _as_string_list(current_refs):
            raise SystemExit(f"{operation_label}: {field} must be a list of catalog IDs")
        entries = catalog.get(namespace, {}) if isinstance(catalog, dict) else {}
        for ref in current_refs:
            if ref in previous_refs:
                continue
            entry = entries.get(ref) if isinstance(entries, dict) else None
            if not isinstance(entry, dict):
                raise SystemExit(f"ARCHITECTURE_SYNTHESIS_REQUIRED: unknown {noun} {ref}")
            if entry.get("automation_reusable") is not True:
                raise SystemExit(
                    f"ARCHITECTURE_SYNTHESIS_REQUIRED: {noun} {ref} is not approved for automated reuse"
                )


def _apply_mutation_plan(
    doc: dict[str, Any],
    plan: dict[str, Any],
    *,
    repository_root: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    if plan.get("mutation_version") != 1:
        raise SystemExit("mutation_version must be 1")
    authority = plan.get("authority")
    if authority not in {"automation", "human"}:
        raise SystemExit("mutation plan authority must be automation or human")
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise SystemExit("mutation plan operations must be a non-empty list")

    aliases: dict[str, str] = {}
    stable_later: list[str] = []
    composition_later: list[str] = []
    evidence_later: list[str] = []
    specification_later = False
    claims = doc.setdefault("claims", {})
    roots = doc.setdefault("roots", [])
    refinement = doc.setdefault("refinement", {})
    refinement_nodes = refinement.setdefault("nodes", {})
    decisions = refinement.setdefault("decisions", {})
    tooling_blockers = refinement.setdefault("tooling_blockers", {})
    assurance = doc.setdefault("assurance", {})
    specification_coverage = assurance.setdefault("specification_coverage", {"status": "unaudited"})
    composition_assurance = assurance.setdefault("composition", {})
    evidence_assurance = assurance.setdefault("implementation_evidence", {})

    for index, raw_op in enumerate(operations):
        if not isinstance(raw_op, dict):
            raise SystemExit(f"operation[{index}] must be a mapping")
        op = raw_op.get("op")

        if op == "add_claim":
            kind = raw_op.get("kind")
            if kind not in CLAIM_KINDS:
                raise SystemExit(f"operation[{index}]: invalid claim kind {kind!r}")
            if authority == "automation" and kind == "root":
                raise SystemExit("automation may not add root guarantees; create a human decision")
            body = raw_op.get("body")
            if not isinstance(body, dict):
                raise SystemExit(f"operation[{index}]: add_claim.body must be a mapping")
            node_id = _allocate_id(doc, _mutation_prefix_for_kind(kind), raw_op.get("slug"))
            if node_id in claims:
                raise SystemExit(f"Allocated duplicate claim ID: {node_id}")
            body = _resolve_ref(body, aliases)
            if authority == "automation":
                mechanism_refs = body.get("mechanisms", [])
                if kind == "leaf" and not mechanism_refs:
                    raise SystemExit("automation-added leaf claims must declare existing catalog mechanisms")
                _assert_automation_reusable_semantic_refs(
                    doc, body, operation_label=f"operation[{index}]"
                )
            body["kind"] = kind
            claims[node_id] = body
            if kind == "root":
                roots.append(node_id)
            refinement_nodes[node_id] = _refinement_defaults(kind)
            assurance_namespace, assurance_entry = _assurance_default_for_kind(kind)
            if assurance_namespace == "composition":
                composition_assurance[node_id] = assurance_entry
            else:
                evidence_assurance[node_id] = assurance_entry
            alias = raw_op.get("alias")
            if alias is not None:
                _validate_slug(alias)
                if alias in aliases:
                    raise SystemExit(f"Duplicate mutation alias: {alias}")
                aliases[alias] = node_id

        elif op == "reclassify_claim":
            old_id = _resolve_ref(raw_op.get("node"), aliases)
            if old_id not in claims:
                raise SystemExit(f"operation[{index}]: unknown claim {old_id}")
            old_kind = claims[old_id].get("kind")
            new_kind = raw_op.get("new_kind")
            if new_kind not in CLAIM_KINDS:
                raise SystemExit(f"operation[{index}]: invalid new_kind {new_kind!r}")
            if authority == "automation" and (old_kind == "root" or new_kind == "root"):
                raise SystemExit("automation may not reclassify root guarantees")
            slug = raw_op.get("slug")
            if slug is None:
                match = re.match(r"^[AGCL]\d+_(.+)$", old_id)
                slug = match.group(1) if match else None
            new_id = _allocate_id(doc, _mutation_prefix_for_kind(new_kind), slug)
            body = claims.pop(old_id)
            previous_body = dict(body)
            body["kind"] = new_kind
            updates = raw_op.get("set", {})
            if updates:
                if not isinstance(updates, dict):
                    raise SystemExit(f"operation[{index}]: reclassify_claim.set must be a mapping")
                body.update(_resolve_ref(updates, aliases))
            if authority == "automation":
                if "semantic_contracts" in updates:
                    raise SystemExit(
                        "automation may not change semantic-contract associations while reclassifying an existing claim; "
                        "use human authority"
                    )
                _assert_automation_reusable_semantic_refs(
                    doc,
                    body,
                    previous=previous_body,
                    operation_label=f"operation[{index}]",
                )
            claims[new_id] = body
            _rewrite_claim_reference(doc, old_id, new_id)
            old_ref = refinement_nodes.pop(old_id, {})
            new_ref = _refinement_defaults(new_kind)
            if isinstance(old_ref, dict):
                new_ref["blocked_by"] = list(old_ref.get("blocked_by", []))
            refinement_nodes[new_id] = new_ref
            composition_assurance.pop(old_id, None)
            evidence_assurance.pop(old_id, None)
            assurance_namespace, assurance_entry = _assurance_default_for_kind(new_kind)
            if assurance_namespace == "composition":
                composition_assurance[new_id] = assurance_entry
            else:
                evidence_assurance[new_id] = assurance_entry
            alias = raw_op.get("alias")
            if alias is not None:
                _validate_slug(alias)
                aliases[alias] = new_id

        elif op == "update_claim":
            node_id = _resolve_ref(raw_op.get("node"), aliases)
            if node_id not in claims:
                raise SystemExit(f"operation[{index}]: unknown claim {node_id}")
            updates = raw_op.get("set", {})
            if not isinstance(updates, dict):
                raise SystemExit(f"operation[{index}]: update_claim.set must be a mapping")
            if "kind" in updates:
                raise SystemExit("Changing kind requires reclassify_claim so the ID prefix remains correct")
            unset_fields = set(raw_op.get("unset", []) or [])
            if authority == "automation" and "semantic_contracts" in (set(updates) | unset_fields):
                raise SystemExit(
                    "automation may not change semantic-contract associations on an existing claim; "
                    "use human authority because this changes the proposition's interpretation boundary"
                )
            if authority == "automation" and claims[node_id].get("kind") == "root":
                root_semantic_fields = {"statement", "formal_intent"}
                if root_semantic_fields & (set(updates) | unset_fields):
                    raise SystemExit(
                        "automation may not change root proposition semantics "
                        "(statement/formal_intent); create a human decision"
                    )
            resolved_updates = _resolve_ref(updates, aliases)
            if authority == "automation" and "mechanisms" in resolved_updates:
                candidate_body = dict(claims[node_id])
                candidate_body.update(resolved_updates)
                _assert_automation_reusable_semantic_refs(
                    doc,
                    candidate_body,
                    previous=claims[node_id],
                    operation_label=f"operation[{index}]",
                )
            claims[node_id].update(resolved_updates)
            for field in raw_op.get("unset", []) or []:
                claims[node_id].pop(field, None)
            ref = refinement_nodes.get(node_id)
            if isinstance(ref, dict):
                ref["status"] = "pending"
                ref.pop("signature", None)

        elif op in {"add_dependency", "remove_dependency"}:
            node_id = _resolve_ref(raw_op.get("node"), aliases)
            dep_id = _resolve_ref(raw_op.get("dependency"), aliases)
            if node_id not in claims:
                raise SystemExit(f"operation[{index}]: unknown claim {node_id}")
            deps = claims[node_id].setdefault("depends_on", [])
            if not isinstance(deps, list):
                raise SystemExit(f"operation[{index}]: {node_id}.depends_on is not a list")
            if op == "add_dependency":
                if dep_id not in deps:
                    deps.append(dep_id)
            else:
                if dep_id not in deps:
                    raise SystemExit(f"operation[{index}]: dependency {dep_id} not present on {node_id}")
                deps.remove(dep_id)
            ref = refinement_nodes.get(node_id)
            if isinstance(ref, dict):
                ref["status"] = "pending"
                ref.pop("signature", None)

        elif op == "remove_claim":
            node_id = _resolve_ref(raw_op.get("node"), aliases)
            if node_id not in claims:
                raise SystemExit(f"operation[{index}]: unknown claim {node_id}")
            if authority == "automation" and claims[node_id].get("kind") == "root":
                raise SystemExit("automation may not remove a root guarantee")
            claims.pop(node_id)
            refinement_nodes.pop(node_id, None)
            composition_assurance.pop(node_id, None)
            evidence_assurance.pop(node_id, None)
            if node_id in roots:
                roots.remove(node_id)

        elif op == "set_refinement":
            node_id = _resolve_ref(raw_op.get("node"), aliases)
            if node_id not in claims or node_id not in refinement_nodes:
                raise SystemExit(f"operation[{index}]: unknown refinement claim {node_id}")
            if raw_op.get("status") == "stable":
                if "rationale" in raw_op:
                    raise SystemExit(
                        "stable refinement state may not persist rationale; canonical YAML records current state, "
                        "while audit history belongs outside the model"
                    )
                expected_contracts = claims[node_id].get("semantic_contracts", []) or []
                provided_contracts = raw_op.get("contract_realizations")
                if (
                    not isinstance(expected_contracts, list)
                    or not isinstance(provided_contracts, list)
                    or len(provided_contracts) != len(set(provided_contracts))
                    or set(provided_contracts) != set(expected_contracts)
                ):
                    raise SystemExit(
                        f"CONTRACT_REALIZATION_REQUIRED: stable refinement for {node_id} must explicitly "
                        f"certify exactly its referenced semantic contracts; expected={sorted(expected_contracts)!r} "
                        f"provided={sorted(provided_contracts) if isinstance(provided_contracts, list) else provided_contracts!r}"
                    )
            entry = refinement_nodes[node_id]
            for field in ("status", "blocked_by", "rationale"):
                if field in raw_op:
                    entry[field] = _resolve_ref(raw_op[field], aliases)
            if entry.get("status") == "stable":
                stable_later.append(node_id)
                entry.pop("signature", None)
                entry.pop("rationale", None)
            else:
                entry.pop("signature", None)

        elif op == "set_specification_coverage":
            status = raw_op.get("status")
            if status not in SPECIFICATION_COVERAGE_STATUSES:
                raise SystemExit(f"operation[{index}]: invalid specification coverage status {status!r}")
            specification_coverage.clear()
            specification_coverage["status"] = status
            for field in ("auditor_count", "rationale"):
                if field in raw_op:
                    specification_coverage[field] = _resolve_ref(raw_op[field], aliases)
            if status != "unaudited":
                specification_later = True

        elif op == "set_composition_assurance":
            node_id = _resolve_ref(raw_op.get("node"), aliases)
            if node_id not in claims or claims[node_id].get("kind") not in {"root", "derived"}:
                raise SystemExit(f"operation[{index}]: composition assurance requires a root/derived claim")
            status = raw_op.get("status")
            if status not in COMPOSITION_ASSURANCE_STATUSES:
                raise SystemExit(f"operation[{index}]: invalid composition assurance status {status!r}")
            entry = {"status": status}
            for field in ("auditor_count", "artifact_refs", "rationale"):
                if field in raw_op:
                    entry[field] = _resolve_ref(raw_op[field], aliases)
            composition_assurance[node_id] = entry
            if status != "unaudited":
                composition_later.append(node_id)

        elif op == "set_evidence_assurance":
            node_id = _resolve_ref(raw_op.get("node"), aliases)
            if node_id not in claims or claims[node_id].get("kind") != "leaf":
                raise SystemExit(f"operation[{index}]: implementation evidence assurance requires a leaf claim")
            status = raw_op.get("status")
            if status not in EVIDENCE_ASSURANCE_STATUSES:
                raise SystemExit(f"operation[{index}]: invalid evidence assurance status {status!r}")
            entry = {"status": status}
            for field in ("implementation_revision", "artifact_refs", "rationale"):
                if field in raw_op:
                    entry[field] = _resolve_ref(raw_op[field], aliases)
            evidence_assurance[node_id] = entry
            if status != "planned":
                evidence_later.append(node_id)

        elif op == "add_decision":
            if authority != "automation" and authority != "human":
                raise AssertionError(authority)
            decision_id = _allocate_id(doc, "D", raw_op.get("slug"))
            body = raw_op.get("body")
            if not isinstance(body, dict):
                raise SystemExit(f"operation[{index}]: add_decision.body must be a mapping")
            body = _resolve_ref(body, aliases)
            body.setdefault("status", "open")
            decisions[decision_id] = body
            alias = raw_op.get("alias")
            if alias is not None:
                _validate_slug(alias)
                aliases[alias] = decision_id

        elif op == "resolve_decision":
            decision_id = _resolve_ref(raw_op.get("decision"), aliases)
            if decision_id not in decisions:
                raise SystemExit(f"operation[{index}]: unknown decision {decision_id}")
            resolution = raw_op.get("resolution")
            if not isinstance(resolution, str) or not resolution.strip():
                raise SystemExit(f"operation[{index}]: resolution must be non-empty")
            decisions[decision_id]["status"] = "resolved"
            decisions[decision_id]["resolution"] = resolution

        elif op == "add_tooling_blocker":
            blocker_id = _allocate_id(doc, "T", raw_op.get("slug"))
            body = raw_op.get("body")
            if not isinstance(body, dict):
                raise SystemExit(f"operation[{index}]: add_tooling_blocker.body must be a mapping")
            body = _resolve_ref(body, aliases)
            body.setdefault("status", "open")
            tooling_blockers[blocker_id] = body
            alias = raw_op.get("alias")
            if alias is not None:
                _validate_slug(alias)
                aliases[alias] = blocker_id

        elif op == "resolve_tooling_blocker":
            if authority != "human":
                raise SystemExit("only human authority may resolve a tooling blocker after tool repair")
            blocker_id = _resolve_ref(raw_op.get("blocker"), aliases)
            if blocker_id not in tooling_blockers:
                raise SystemExit(f"operation[{index}]: unknown tooling blocker {blocker_id}")
            resolution = raw_op.get("resolution")
            if not isinstance(resolution, str) or not resolution.strip():
                raise SystemExit(f"operation[{index}]: resolution must be non-empty")
            tooling_blockers[blocker_id]["status"] = "resolved"
            tooling_blockers[blocker_id]["resolution"] = resolution

        elif op == "block_node":
            node_id = _resolve_ref(raw_op.get("node"), aliases)
            decision_id = _resolve_ref(raw_op.get("decision"), aliases)
            if node_id not in refinement_nodes or decision_id not in decisions:
                raise SystemExit(f"operation[{index}]: unknown node/decision")
            entry = refinement_nodes[node_id]
            blockers = entry.setdefault("blocked_by", [])
            if decision_id not in blockers:
                blockers.append(decision_id)
            entry["status"] = "pending"
            entry.pop("signature", None)

        elif op == "unblock_node":
            node_id = _resolve_ref(raw_op.get("node"), aliases)
            decision_id = _resolve_ref(raw_op.get("decision"), aliases)
            if node_id not in refinement_nodes:
                raise SystemExit(f"operation[{index}]: unknown node {node_id}")
            blockers = refinement_nodes[node_id].setdefault("blocked_by", [])
            if decision_id in blockers:
                blockers.remove(decision_id)

        elif op == "add_assumption":
            if authority != "human":
                raise SystemExit("automation may not add assumptions; create a human decision")
            body = raw_op.get("body")
            if not isinstance(body, dict):
                raise SystemExit(f"operation[{index}]: add_assumption.body must be a mapping")
            node_id = _allocate_id(doc, "A", raw_op.get("slug"))
            doc.setdefault("assumptions", {})[node_id] = _resolve_ref(body, aliases)
            alias = raw_op.get("alias")
            if alias is not None:
                _validate_slug(alias)
                aliases[alias] = node_id

        elif op == "set_value":
            if authority != "human":
                raise SystemExit("set_value is human-only because it may change system specification")
            path = raw_op.get("path")
            if not isinstance(path, list) or not path or not all(isinstance(x, str) for x in path):
                raise SystemExit(f"operation[{index}]: set_value.path must be a non-empty list of strings")
            cursor = doc
            for key in path[:-1]:
                child = cursor.get(key)
                if not isinstance(child, dict):
                    child = {}
                    cursor[key] = child
                cursor = child
            cursor[path[-1]] = _resolve_ref(raw_op.get("value"), aliases)

        elif op == "unset_value":
            if authority != "human":
                raise SystemExit("unset_value is human-only because it may change system specification")
            path = raw_op.get("path")
            if not isinstance(path, list) or not path or not all(isinstance(x, str) for x in path):
                raise SystemExit(f"operation[{index}]: unset_value.path must be a non-empty list of strings")
            cursor = doc
            for key in path[:-1]:
                child = cursor.get(key)
                if not isinstance(child, dict):
                    raise SystemExit(f"operation[{index}]: unset_value path does not exist")
                cursor = child
            if path[-1] not in cursor:
                raise SystemExit(f"operation[{index}]: unset_value path does not exist")
            cursor.pop(path[-1])

        else:
            raise SystemExit(f"operation[{index}]: unsupported op {op!r}")

    # Compute stable signatures only after every semantic operation has been applied.
    candidate_graph = Graph(_plain_data(doc), repository_root=repository_root)
    for node_id in stable_later:
        if node_id not in candidate_graph.claims:
            raise SystemExit(f"Cannot mark removed claim stable: {node_id}")
        refinement_nodes[node_id]["signature"] = _node_semantic_signature(candidate_graph, node_id)
    for node_id in composition_later:
        if node_id not in candidate_graph.claims:
            raise SystemExit(f"Cannot record composition assurance for removed claim: {node_id}")
        composition_assurance[node_id]["signature"] = _composition_semantic_signature(candidate_graph, node_id)
    for node_id in evidence_later:
        if node_id not in candidate_graph.claims:
            raise SystemExit(f"Cannot record evidence assurance for removed claim: {node_id}")
        evidence_assurance[node_id]["signature"] = _node_semantic_signature(candidate_graph, node_id)
    if specification_later:
        specification_coverage["signature"] = _model_semantic_signature(candidate_graph)
    return aliases, stable_later


def _check_mutation_preconditions(graph: Graph, plan: dict[str, Any]) -> None:
    authority = plan.get("authority")
    preconditions = plan.get("preconditions", {})
    if not isinstance(preconditions, dict):
        raise SystemExit("preconditions must be a mapping")
    signatures = preconditions.get("semantic_signatures", {})
    if not isinstance(signatures, dict):
        raise SystemExit("preconditions.semantic_signatures must be a mapping")
    model_signature = preconditions.get("model_signature")
    if model_signature is not None and not isinstance(model_signature, str):
        raise SystemExit("preconditions.model_signature must be a string")
    if authority == "automation" and not signatures and not model_signature:
        raise SystemExit(
            "automation mutation requires at least one semantic_signatures entry or model_signature precondition"
        )
    if model_signature is not None:
        actual_model_signature = _model_semantic_signature(graph)
        if model_signature != actual_model_signature:
            raise SystemExit(
                f"STALE_MUTATION: model semantic signature changed; expected {model_signature}, actual {actual_model_signature}"
            )
    for node_id, expected in signatures.items():
        if node_id not in graph.claims:
            raise SystemExit(f"Precondition node no longer exists: {node_id}")
        actual = _node_semantic_signature(graph, node_id)
        if expected != actual:
            raise SystemExit(
                f"STALE_MUTATION: {node_id} semantic signature changed; expected {expected}, actual {actual}"
            )


def _atomic_write_roundtrip(doc: Any) -> None:
    rt = _rt_yaml()
    temp_path: Path | None = None
    try:
        buffer = io.StringIO()
        rt.dump(doc, buffer)
        rendered = buffer.getvalue()
        rendered = "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=GRAPH_PATH.parent, prefix=GRAPH_PATH.name + ".", suffix=".tmp", delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(rendered)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, GRAPH_PATH)
        # Best-effort directory fsync so rename durability matches the atomic-write intent.
        try:
            dir_fd = os.open(GRAPH_PATH.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _cmd_mutate(_graph: Graph, args: argparse.Namespace) -> int:
    plan = _load_mutation_plan(args.plan)
    with _exclusive_graph_lock():
        rt = _rt_yaml()
        with GRAPH_PATH.open("r", encoding="utf-8") as stream:
            doc = rt.load(stream)
        if not isinstance(doc, dict):
            raise SystemExit("correctness.yaml must be a mapping")

        current_graph = Graph(_plain_data(doc), repository_root=_repository_root_for_graph_path(GRAPH_PATH))
        current_errors, current_warnings = validate(current_graph)
        is_human_schema_migration = (
            plan.get("authority") == "human"
            and any(
                isinstance(op, dict)
                and op.get("op") == "set_value"
                and op.get("path") == ["schema_version"]
                for op in plan.get("operations", [])
            )
        )
        if current_errors and not is_human_schema_migration:
            raise SystemExit(
                "Refusing mutation because current graph is invalid:\n- " + "\n- ".join(current_errors)
            )
        current_refinement = _refinement_snapshot(current_graph)
        if current_refinement.get("state") == "TOOLING_BLOCKED" and plan.get("authority") != "human":
            raise SystemExit(
                "TOOLING_BLOCKED: automation mutations are disabled until a human repairs the tool "
                "and resolves all open tooling blockers"
            )
        _check_mutation_preconditions(current_graph, plan)

        aliases, _stable = _apply_mutation_plan(
            doc, plan, repository_root=_repository_root_for_graph_path(GRAPH_PATH)
        )
        candidate_graph = Graph(_plain_data(doc), repository_root=_repository_root_for_graph_path(GRAPH_PATH))
        errors, warnings = validate(candidate_graph)
        if errors:
            raise SystemExit(
                "MUTATION_REJECTED: candidate graph failed validation:\n- " + "\n- ".join(errors)
            )
        if warnings and args.reject_warnings:
            raise SystemExit(
                "MUTATION_REJECTED: candidate graph produced warnings:\n- " + "\n- ".join(warnings)
            )

        if not args.dry_run:
            _atomic_write_roundtrip(doc)

    result = {
        "status": "DRY_RUN_VALID" if args.dry_run else "COMMITTED",
        "aliases": aliases,
        "warnings": warnings,
        "nodes": len(candidate_graph.nodes),
        "claims": len(candidate_graph.claims),
        "roots": len(candidate_graph.roots),
    }
    print(yaml.safe_dump(result, sort_keys=False, allow_unicode=True).rstrip())
    return 0


def _escape_mermaid(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def _cmd_render(graph: Graph, args: argparse.Namespace) -> int:
    selected: set[str]
    if args.target:
        selected = graph.dependency_closure(args.target)
    else:
        selected = set(graph.nodes)
    print("```mermaid")
    print("graph TD")
    for node_id in sorted(selected):
        node = graph.nodes[node_id]
        statement = " ".join(node.data.get("statement", "").split())
        short = statement if len(statement) <= 70 else statement[:67] + "..."
        label = _escape_mermaid(f"{node_id}: {short}")
        if node.node_type == "assumption":
            print(f'    {node_id}[/"{label}"/]')
        elif node.data.get("kind") == "root":
            print(f'    {node_id}[["{label}"]]')
        else:
            print(f'    {node_id}["{label}"]')
    for node_id in sorted(selected):
        node = graph.nodes[node_id]
        for dep in node.dependencies:
            if dep in selected:
                # dependency -> dependent, matching invalidation propagation direction
                print(f"    {dep} --> {node_id}")
    print("```")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Correctness DAG validator, proof slicer, and automated refinement control plane.",
        epilog=(
            "Agents: run `.venv/bin/python3 correctness.py workflow-help` before participating in correctness work. "
            "Drive refinement from `refinement-status --format compact-yaml`; after refinement freezes, "
            "drive specification coverage via `coverage-audit-prompt` and proof composition via "
            "`composition-status` / `composition-audit-prompt`. Load `mutation-schema` once per tooling "
            "revision before compiling mutation plans, apply graph/assurance changes only through `mutate`, "
            "and keep every clean auditor isolated to its generated contract."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add_symbolic_subparsers(sub)

    sub.add_parser("workflow-help", help="Print the canonical automated-refinement agent protocol.")
    catalog_parser = sub.add_parser(
        "catalog",
        help="Inspect one typed catalog namespace or entry.",
    )
    catalog_parser.add_argument("namespace", choices=CATALOG_NAMESPACES)
    catalog_parser.add_argument("key", nargs="?", help="Optional catalog entry ID.")
    catalog_parser.add_argument("--format", choices=["text", "yaml"], default="text")

    graph_schema_parser = sub.add_parser(
        "graph-schema",
        help="Print the canonical machine-readable correctness-graph JSON Schema.",
    )
    graph_schema_parser.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Render the canonical graph schema as YAML or JSON.",
    )

    formal_schema_parser = sub.add_parser(
        "formal-schema",
        help="Print a machine-readable JSON Schema for one persisted symbolic sidecar/artifact kind.",
    )
    formal_schema_parser.add_argument(
        "kind",
        choices=sorted(FORMAL_SCHEMAS),
        help="Formal YAML kind to describe.",
    )
    formal_schema_parser.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Render the formal schema as YAML or JSON.",
    )

    schema_fields_parser = sub.add_parser(
        "schema-fields",
        help="List every canonical YAML field path, its structural constraint, and its semantic description.",
    )
    schema_fields_parser.add_argument(
        "--format", choices=["text", "yaml"], default="text",
        help="Render the deduplicated field inventory as text or YAML.",
    )

    mutation_schema_parser = sub.add_parser(
        "mutation-schema",
        help="Print the canonical machine-readable mutation-plan JSON Schema.",
    )
    mutation_schema_parser.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Render the canonical schema as YAML or JSON.",
    )
    sub.add_parser(
        "validate",
        help="Validate canonical graph invariants plus any persisted formal symbolic assurance layer.",
    )

    slice_parser = sub.add_parser(
        "slice", help="Extract the recursive dependency closure for one proof obligation."
    )
    slice_parser.add_argument("target", help="Target claim/assumption node ID.")
    slice_parser.add_argument(
        "--stop-at",
        action="append",
        default=[],
        metavar="NODE",
        help="Treat NODE as an opaque cut point instead of recursively expanding it. Repeatable.",
    )
    slice_parser.add_argument(
        "--format",
        choices=["md", "yaml", "prompt"],
        default="md",
        help="Output human slice, machine-readable YAML, or an adversarial LLM prompt.",
    )

    invalidation_parser = sub.add_parser(
        "invalidate",
        help="Compute transitive dependent claims that need re-verification after a change.",
    )
    invalidation_parser.add_argument("changed", nargs="+", help="Changed node ID(s).")

    audit_prompt_parser = sub.add_parser(
        "audit-prompt",
        help="Generate the complete clean-context audit contract for one runnable claim.",
    )
    audit_prompt_parser.add_argument(
        "node",
        help="Runnable claim node ID from refinement-status; non-runnable nodes are rejected.",
    )

    sub.add_parser(
        "coverage-audit-prompt",
        help="Generate the canonical final root-coverage/specification-completeness audit campaign prompt.",
    )

    composition_status_parser = sub.add_parser(
        "composition-status",
        help="Compute the frozen-DAG proof-composition certification frontier.",
    )
    composition_status_parser.add_argument(
        "--format",
        choices=["text", "yaml", "compact-yaml"],
        default="text",
        help="Output concise text or machine-readable campaign state.",
    )

    composition_prompt_parser = sub.add_parser(
        "composition-audit-prompt",
        help="Generate a clean-context direct-premises-imply-target certification prompt for one non-leaf claim.",
    )
    composition_prompt_parser.add_argument("node", help="Root/derived claim ID to certify.")
    composition_prompt_parser.add_argument(
        "--focus",
        choices=sorted(COMPOSITION_AUDIT_FOCUS),
        default="general",
        help="Adversarial specialization for an independent composition auditor.",
    )

    mutate_parser = sub.add_parser(
        "mutate",
        help="Apply a locked, validated, atomic structured mutation plan to correctness.yaml.",
    )
    mutate_parser.add_argument(
        "--plan",
        default="-",
        help="Mutation-plan YAML path, or - for stdin (default).",
    )
    mutate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Optional preview: apply and validate in memory without writing correctness.yaml; not required before ordinary commits.",
    )
    mutate_parser.add_argument(
        "--reject-warnings",
        action="store_true",
        help="Reject a candidate that produces validator warnings as well as errors.",
    )

    refinement_status_parser = sub.add_parser(
        "refinement-status",
        help="Compute the bottom-up automated audit frontier and global stop state.",
    )
    refinement_status_parser.add_argument(
        "--format",
        choices=["text", "yaml", "compact-yaml"],
        default="text",
        help="Output concise text, full diagnostic YAML, or compact orchestrator YAML.",
    )

    assurance_status_parser = sub.add_parser(
        "assurance-status",
        help="Report orthogonal specification, proof-composition, and implementation-evidence assurance.",
    )
    assurance_status_parser.add_argument(
        "--format",
        choices=["text", "yaml", "compact-yaml"],
        default="text",
        help="Output concise text, full per-node YAML, or compact assurance summary YAML.",
    )
    assurance_status_parser.add_argument(
        "--omit-specification-coverage",
        action="store_true",
        help="Omit prior specification-coverage status/provenance; intended for fresh coverage-audit bootstrap isolation.",
    )

    refinement_signature_parser = sub.add_parser(
        "refinement-signature",
        help="Print the recursive semantic signature for one claim's proof closure.",
    )
    refinement_signature_parser.add_argument("node", help="Claim node ID.")

    render_parser = sub.add_parser("render", help="Render the DAG or one proof slice as Mermaid.")
    render_parser.add_argument(
        "target",
        nargs="?",
        help="Optional target node; when set, render only its dependency closure.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "workflow-help":
        return _cmd_workflow_help(None, args)
    if args.command == "graph-schema":
        return _cmd_graph_schema(None, args)
    if args.command == "mutation-schema":
        return _cmd_mutation_schema(None, args)
    if args.command == "formal-schema":
        return _cmd_formal_schema(None, args)
    if args.command == "schema-fields":
        return _cmd_schema_fields(None, args)

    graph = Graph.load()
    symbolic_result = handle_symbolic_command(graph, args)
    if symbolic_result is not None:
        return symbolic_result
    if args.command == "catalog":
        return _cmd_catalog(graph, args)
    if args.command == "validate":
        return _print_validation(graph)
    if args.command == "slice":
        return _cmd_slice(graph, args)
    if args.command == "invalidate":
        return _cmd_invalidate(graph, args)
    if args.command == "audit-prompt":
        return _cmd_audit_prompt(graph, args)
    if args.command == "coverage-audit-prompt":
        return _cmd_coverage_audit_prompt(graph, args)
    if args.command == "composition-status":
        return _cmd_composition_status(graph, args)
    if args.command == "composition-audit-prompt":
        return _cmd_composition_audit_prompt(graph, args)
    if args.command == "mutate":
        return _cmd_mutate(graph, args)
    if args.command == "refinement-status":
        return _cmd_refinement_status(graph, args)
    if args.command == "assurance-status":
        return _cmd_assurance_status(graph, args)
    if args.command == "refinement-signature":
        return _cmd_refinement_signature(graph, args)
    if args.command == "render":
        return _cmd_render(graph, args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    sys.exit(main())
