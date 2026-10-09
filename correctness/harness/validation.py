from __future__ import annotations

import collections
import re
from pathlib import Path
from typing import Any

from graph_schema import CANONICAL_GRAPH_SCHEMA

from .contracts import (
    CATALOG_NAMESPACES,
    CLAIM_KINDS,
    DECISION_STATUSES,
    EVIDENCE_ASSURANCE_STATUSES,
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
    COMPOSITION_ASSURANCE_STATUSES,
    _CANONICAL_GRAPH_VALIDATOR,
)
from .model import (
    Graph,
    _as_mapping,
    _as_string_list,
    _catalog,
    _catalog_namespace,
    _plain_data,
    _semantic_catalog_refs_for_node,
)
from .signatures import _node_semantic_signature

def _format_schema_path(error: Any) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _markdown_h2_sections_by_heading(text: str) -> dict[str, list[str]]:
    """Return H2 sections keyed by semantic heading, ignoring numeric display prefixes.

    `## 6. Owner failover` and `## Owner failover` both resolve to `Owner failover`.
    Values are lists so duplicate semantic headings can be rejected as ambiguous.
    """
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    sections: dict[str, list[str]] = collections.defaultdict(list)
    for index, match in enumerate(matches):
        raw_heading = match.group(1).strip()
        heading = re.sub(r"^[0-9]+\.\s+", "", raw_heading).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[heading].append(text[match.start():end])
    return dict(sections)


def _contains_exact_identifier(text: str, identifier: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])",
        text,
    ) is not None


def _validate_source_traceability(graph: Graph, errors: list[str]) -> None:
    """Bind semantic-contract references to explicit literals in claim text and source sections.

    The in-claim check always runs. The source-document check runs when the graph was loaded with
    repository context (normal CLI/mutation paths); synthetic in-memory unit fixtures may omit it.
    """
    catalog = graph.doc.get("catalog", {})
    contracts = catalog.get("semantic_contracts", {}) if isinstance(catalog, dict) else {}
    sources = catalog.get("sources", {}) if isinstance(catalog, dict) else {}

    for node_id, claim in sorted(graph.claims.items()):
        refs = claim.get("semantic_contracts", [])
        if not isinstance(refs, list):
            continue
        claim_text = "\n".join(
            value for value in (claim.get("statement"), claim.get("formal_intent"))
            if isinstance(value, str)
        )
        for contract_id in refs:
            if isinstance(contract_id, str) and not _contains_exact_identifier(claim_text, contract_id):
                errors.append(
                    f"{node_id}: semantic contract {contract_id} must appear as an exact identifier "
                    "in statement or formal_intent"
                )
        if isinstance(contracts, dict):
            ref_set = {ref for ref in refs if isinstance(ref, str)}
            for contract_id in contracts:
                if (
                    isinstance(contract_id, str)
                    and contract_id not in ref_set
                    and _contains_exact_identifier(claim_text, contract_id)
                ):
                    errors.append(
                        f"{node_id}: statement or formal_intent names semantic contract {contract_id} "
                        "but semantic_contracts does not declare it"
                    )

    if graph.repository_root is None:
        return

    source = graph.doc.get("source", {})
    article_ref = source.get("article") if isinstance(source, dict) else None
    if not isinstance(article_ref, str) or not article_ref.strip():
        return  # structural schema validation reports this separately
    article_path = graph.repository_root / article_ref
    try:
        article_text = article_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append(f"source.article does not exist: {article_ref}")
        return
    except OSError as exc:
        errors.append(f"source.article cannot be read: {article_ref}: {exc}")
        return

    sections = _markdown_h2_sections_by_heading(article_text)
    source_sections: dict[str, str] = {}
    if isinstance(sources, dict):
        for source_id, entry in sources.items():
            if not isinstance(entry, dict):
                continue
            heading = entry.get("heading")
            if not isinstance(heading, str) or not heading.strip():
                continue
            matches = sections.get(heading, [])
            if not matches:
                errors.append(
                    f"catalog.sources.{source_id}: source article missing H2 heading {heading!r}"
                )
                continue
            if len(matches) != 1:
                errors.append(
                    f"catalog.sources.{source_id}: source article H2 heading {heading!r} is ambiguous "
                    f"({len(matches)} matches)"
                )
                continue
            source_sections[source_id] = matches[0]

    for node_id, claim in sorted(graph.claims.items()):
        contract_refs = claim.get("semantic_contracts", [])
        source_refs = claim.get("source_refs", [])
        if not isinstance(contract_refs, list) or not isinstance(source_refs, list):
            continue
        relevant_sections = [source_sections[ref] for ref in source_refs if ref in source_sections]
        for contract_id in contract_refs:
            if contract_id not in contracts or not isinstance(contract_id, str):
                continue  # foreign-key validation reports unknown contracts separately
            if not any(_contains_exact_identifier(section, contract_id) for section in relevant_sections):
                errors.append(
                    f"{node_id}: semantic contract {contract_id} must appear as an exact identifier "
                    "in at least one source_ref article section"
                )


def _validate_formalization_registry(graph: Graph, errors: list[str]) -> None:
    formalization = graph.doc.get("formalization")
    if not isinstance(formalization, dict):
        return  # closed schema reports structural absence/type errors

    expected = {
        "claims": set(graph.claims),
        "assumptions": set(graph.assumptions),
        "semantic_contracts": set(_catalog_namespace(graph, "semantic_contracts")),
    }
    for namespace, expected_ids in expected.items():
        entries = formalization.get(namespace)
        if not isinstance(entries, dict):
            continue
        actual_ids = set(entries)
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        if missing:
            errors.append(
                f"formalization.{namespace} must classify every canonical subject; missing={missing}"
            )
        if extra:
            errors.append(
                f"formalization.{namespace} contains unknown subjects; extra={extra}"
            )


def validate(graph: Graph) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    doc = graph.doc

    schema_errors = sorted(
        _CANONICAL_GRAPH_VALIDATOR.iter_errors(_plain_data(doc)),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    for error in schema_errors:
        errors.append(f"schema {_format_schema_path(error)}: {error.message}")

    errors.extend(_schema_reference_contract_errors())
    _validate_formalization_registry(graph, errors)

    required_top_level = {
        "schema_version",
        "system",
        "source",
        "scope",
        "catalog",
        "id_allocator",
        "refinement",
        "assurance",
        "assumptions",
        "roots",
        "claims",
    }
    for key in sorted(required_top_level - set(doc)):
        errors.append(f"missing top-level field: {key}")

    if not _as_mapping(doc.get("assumptions")):
        errors.append("assumptions must be a mapping")
    if not _as_mapping(doc.get("claims")):
        errors.append("claims must be a mapping")
    if not _as_string_list(doc.get("roots")):
        errors.append("roots must be a list of node IDs")

    refinement = doc.get("refinement")
    if not isinstance(refinement, dict):
        errors.append("refinement must be a mapping")
    else:
        decisions = refinement.get("decisions")
        tooling_blockers = refinement.get("tooling_blockers")
        nodes = refinement.get("nodes")
        if not isinstance(decisions, dict):
            errors.append("refinement.decisions must be a mapping")
            decisions = {}
        else:
            for decision_id, decision in decisions.items():
                if not isinstance(decision_id, str) or not NODE_ID_RE.match(decision_id):
                    errors.append(f"invalid refinement decision ID: {decision_id!r}")
                    continue
                if not isinstance(decision, dict):
                    errors.append(f"refinement.decisions.{decision_id} must be a mapping")
                    continue
                status = decision.get("status")
                if status not in DECISION_STATUSES:
                    errors.append(
                        f"refinement.decisions.{decision_id}.status must be one of {sorted(DECISION_STATUSES)}"
                    )
                question = decision.get("question")
                if not isinstance(question, str) or not question.strip():
                    errors.append(
                        f"refinement.decisions.{decision_id}.question must be a non-empty string"
                    )
                if status == "resolved":
                    resolution = decision.get("resolution")
                    if not isinstance(resolution, str) or not resolution.strip():
                        errors.append(
                            f"refinement.decisions.{decision_id}: resolved decision requires non-empty resolution"
                        )

        if not isinstance(tooling_blockers, dict):
            errors.append("refinement.tooling_blockers must be a mapping")
            tooling_blockers = {}
        else:
            for blocker_id, blocker in tooling_blockers.items():
                if not isinstance(blocker_id, str) or not NODE_ID_RE.match(blocker_id):
                    errors.append(f"invalid tooling blocker ID: {blocker_id!r}")
                    continue
                if not isinstance(blocker, dict):
                    errors.append(f"refinement.tooling_blockers.{blocker_id} must be a mapping")
                    continue
                status = blocker.get("status")
                if status not in TOOLING_BLOCKER_STATUSES:
                    errors.append(
                        f"refinement.tooling_blockers.{blocker_id}.status must be one of {sorted(TOOLING_BLOCKER_STATUSES)}"
                    )
                issue = blocker.get("issue")
                reason = blocker.get("reason")
                if not isinstance(issue, str) or not issue.strip():
                    errors.append(
                        f"refinement.tooling_blockers.{blocker_id}.issue must be a non-empty string"
                    )
                if not isinstance(reason, str) or not reason.strip():
                    errors.append(
                        f"refinement.tooling_blockers.{blocker_id}.reason must be a non-empty string"
                    )
                if status == "resolved":
                    resolution = blocker.get("resolution")
                    if not isinstance(resolution, str) or not resolution.strip():
                        errors.append(
                            f"refinement.tooling_blockers.{blocker_id}: resolved blocker requires non-empty resolution"
                        )

        if not isinstance(nodes, dict):
            errors.append("refinement.nodes must be a mapping")
            nodes = {}
        else:
            missing_refinement = set(graph.claims) - set(nodes)
            extra_refinement = set(nodes) - set(graph.claims)
            for node_id in sorted(missing_refinement):
                errors.append(f"refinement.nodes missing claim {node_id}")
            for node_id in sorted(extra_refinement):
                errors.append(f"refinement.nodes references unknown/non-claim node {node_id}")

            open_decision_refs: set[str] = set()
            for node_id, entry in nodes.items():
                if node_id not in graph.claims or not isinstance(entry, dict):
                    if not isinstance(entry, dict):
                        errors.append(f"refinement.nodes.{node_id} must be a mapping")
                    continue
                status = entry.get("status")
                if status not in REFINEMENT_STATUSES:
                    errors.append(
                        f"refinement.nodes.{node_id}.status must be one of {sorted(REFINEMENT_STATUSES)}"
                    )
                blocked_by = entry.get("blocked_by", [])
                if not _as_string_list(blocked_by):
                    errors.append(
                        f"refinement.nodes.{node_id}.blocked_by must be a list of decision IDs"
                    )
                    blocked_by = []
                if len(blocked_by) != len(set(blocked_by)):
                    errors.append(f"refinement.nodes.{node_id}.blocked_by contains duplicates")
                if blocked_by and status != "pending":
                    errors.append(
                        f"refinement.nodes.{node_id}: only pending nodes may declare blocked_by"
                    )
                for decision_id in blocked_by:
                    if decision_id not in decisions:
                        errors.append(
                            f"refinement.nodes.{node_id}: unknown blocker decision {decision_id}"
                        )
                    elif isinstance(decisions[decision_id], dict) and decisions[decision_id].get("status") == "open":
                        open_decision_refs.add(decision_id)
                signature = entry.get("signature")
                if status == "stable":
                    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
                        errors.append(
                            f"refinement.nodes.{node_id}: stable node requires a 64-hex signature"
                        )
                if status == "waived":
                    rationale = entry.get("rationale")
                    if not isinstance(rationale, str) or not rationale.strip():
                        errors.append(
                            f"refinement.nodes.{node_id}: waived node requires non-empty rationale"
                        )

            if isinstance(decisions, dict):
                for decision_id, decision in decisions.items():
                    if (
                        isinstance(decision, dict)
                        and decision.get("status") == "open"
                        and decision_id not in open_decision_refs
                    ):
                        errors.append(
                            f"refinement.decisions.{decision_id}: open decision does not block any current claim"
                        )

    assurance = doc.get("assurance")
    if not isinstance(assurance, dict):
        errors.append("assurance must be a mapping")
    else:
        specification = assurance.get("specification_coverage")
        composition = assurance.get("composition")
        evidence = assurance.get("implementation_evidence")
        if not isinstance(specification, dict):
            errors.append("assurance.specification_coverage must be a mapping")
        elif specification.get("status") not in SPECIFICATION_COVERAGE_STATUSES:
            errors.append(
                "assurance.specification_coverage.status must be one of "
                f"{sorted(SPECIFICATION_COVERAGE_STATUSES)}"
            )

        expected_composition = {
            node_id for node_id, claim in graph.claims.items()
            if claim.get("kind") in {"root", "derived"}
        }
        expected_evidence = {
            node_id for node_id, claim in graph.claims.items()
            if claim.get("kind") == "leaf"
        }
        if not isinstance(composition, dict):
            errors.append("assurance.composition must be a mapping")
            composition = {}
        else:
            for node_id in sorted(expected_composition - set(composition)):
                errors.append(f"assurance.composition missing non-leaf claim {node_id}")
            for node_id in sorted(set(composition) - expected_composition):
                errors.append(f"assurance.composition references leaf/unknown claim {node_id}")
            for node_id, entry in composition.items():
                if node_id not in expected_composition or not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if status not in COMPOSITION_ASSURANCE_STATUSES:
                    errors.append(
                        f"assurance.composition.{node_id}.status must be one of "
                        f"{sorted(COMPOSITION_ASSURANCE_STATUSES)}"
                    )

        if not isinstance(evidence, dict):
            errors.append("assurance.implementation_evidence must be a mapping")
            evidence = {}
        else:
            for node_id in sorted(expected_evidence - set(evidence)):
                errors.append(f"assurance.implementation_evidence missing leaf claim {node_id}")
            for node_id in sorted(set(evidence) - expected_evidence):
                errors.append(f"assurance.implementation_evidence references non-leaf/unknown claim {node_id}")
            for node_id, entry in evidence.items():
                if node_id not in expected_evidence or not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if status not in EVIDENCE_ASSURANCE_STATUSES:
                    errors.append(
                        f"assurance.implementation_evidence.{node_id}.status must be one of "
                        f"{sorted(EVIDENCE_ASSURANCE_STATUSES)}"
                    )

    if "reasoning_context" in doc:
        errors.append("reasoning_context is legacy in schema 0.7; system-specific context belongs in system/scope and tool interpretation rules live in correctness.py")
    if "state_model" in doc:
        errors.append("state_model is legacy; move state definitions into catalog.state")

    catalog = doc.get("catalog")
    if not isinstance(catalog, dict):
        errors.append("catalog must be a mapping")
        catalog = {}
    for namespace in CATALOG_NAMESPACES:
        entries = catalog.get(namespace) if isinstance(catalog, dict) else None
        if namespace == "scope_dimensions" and entries is None:
            continue
        if not isinstance(entries, dict) or not entries:
            errors.append(f"catalog.{namespace} must be a non-empty mapping")
            continue
        for key, entry in entries.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", key):
                errors.append(f"catalog.{namespace} key {key!r} must be lower snake_case")
            if not isinstance(entry, dict):
                errors.append(f"catalog.{namespace}.{key} must be a mapping")
                continue
            if namespace == "semantic_contracts":
                definition = entry.get("definition")
                if not isinstance(definition, str) or not definition.strip():
                    errors.append(f"catalog.semantic_contracts.{key}.definition must be non-empty")
                if entry.get("class") not in {
                    "safety_contract",
                    "equivalence_relation",
                    "state_invariant",
                    "provenance_binding",
                }:
                    errors.append(
                        f"catalog.semantic_contracts.{key}.class must be safety_contract, equivalence_relation, state_invariant, or provenance_binding"
                    )
                excludes = entry.get("excludes")
                if not _as_string_list(excludes):
                    errors.append(f"catalog.semantic_contracts.{key}.excludes must be a string list")
                if not isinstance(entry.get("automation_reusable"), bool):
                    errors.append(f"catalog.semantic_contracts.{key}.automation_reusable must be boolean")
            elif namespace != "sources":
                description = entry.get("description")
                if not isinstance(description, str) or not description.strip():
                    errors.append(f"catalog.{namespace}.{key}.description must be non-empty")
            if namespace == "state" and entry.get("class") not in {"authoritative", "derived", "speculative", "control"}:
                errors.append(f"catalog.state.{key}.class must be authoritative, derived, speculative, or control")
            if namespace == "mechanisms" and not isinstance(entry.get("automation_reusable"), bool):
                errors.append(f"catalog.mechanisms.{key}.automation_reusable must be boolean")
            if namespace == "failure_events" and entry.get("class") not in {"allowed", "excluded"}:
                errors.append(f"catalog.failure_events.{key}.class must be allowed or excluded")
            if namespace == "sources":
                if re.fullmatch(r"section_[0-9]+", key):
                    errors.append(
                        f"catalog.sources.{key}: positional source IDs are forbidden; use a stable semantic ID"
                    )
                if not isinstance(entry.get("heading"), str) or not entry.get("heading", "").strip():
                    errors.append(f"catalog.sources.{key}.heading must be non-empty")

    semantic_symbols = set(_catalog_namespace(graph, "terms")) | set(_catalog_namespace(graph, "state"))
    symbolic_dimensions = _catalog_namespace(graph, "symbolic_dimensions")
    scope_dimensions = _catalog_namespace(graph, "scope_dimensions")
    state_catalog = _catalog_namespace(graph, "state")
    term_catalog = _catalog_namespace(graph, "terms")
    for state_id, entry in state_catalog.items():
        if not isinstance(entry, dict):
            continue
        dimensions = entry.get("symbolic_dimensions")
        if isinstance(dimensions, list):
            for dimension in dimensions:
                if isinstance(dimension, str) and dimension not in symbolic_dimensions:
                    errors.append(
                        f"catalog.state.{state_id}.symbolic_dimensions references unknown "
                        f"catalog.symbolic_dimensions entry {dimension!r}"
                    )

    for namespace, entries in (("terms", term_catalog), ("state", state_catalog)):
        for symbol_id, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            dimensions = entry.get("scope_dimensions")
            if not isinstance(dimensions, list):
                continue
            for dimension in dimensions:
                if isinstance(dimension, str) and dimension not in scope_dimensions:
                    errors.append(
                        f"catalog.{namespace}.{symbol_id}.scope_dimensions references unknown "
                        f"catalog.scope_dimensions entry {dimension!r}"
                    )

    def semantic_symbol_scopes(symbol: str) -> set[str]:
        # A semantic identifier may intentionally exist in both vocabulary and state namespaces.
        # Scope is semantic, so preserve the union rather than letting one namespace shadow the other.
        result: set[str] = set()
        for entries in (term_catalog, state_catalog):
            entry = entries.get(symbol)
            if not isinstance(entry, dict):
                continue
            dimensions = entry.get("scope_dimensions", [])
            if isinstance(dimensions, list):
                result.update(d for d in dimensions if isinstance(d, str))
        return result

    contracts = _catalog_namespace(graph, "semantic_contracts")
    for contract_id, entry in contracts.items():
        if not isinstance(entry, dict):
            continue
        contract_class = entry.get("class")
        if contract_class == "state_invariant":
            for symbol in entry.get("state_symbols", []):
                if isinstance(symbol, str) and symbol not in semantic_symbols:
                    errors.append(
                        f"catalog.semantic_contracts.{contract_id}.state_symbols references unknown semantic symbol {symbol!r}"
                    )
        elif contract_class == "provenance_binding":
            for field in ("source_symbols", "bound_symbols"):
                for symbol in entry.get(field, []):
                    if isinstance(symbol, str) and symbol not in semantic_symbols:
                        errors.append(
                            f"catalog.semantic_contracts.{contract_id}.{field} references unknown semantic symbol {symbol!r}"
                        )
            source_symbols = {
                symbol for symbol in entry.get("source_symbols", []) if isinstance(symbol, str)
            }
            bound_symbols = {
                symbol for symbol in entry.get("bound_symbols", []) if isinstance(symbol, str)
            }
            overlap = sorted(source_symbols & bound_symbols)
            if overlap:
                errors.append(
                    f"catalog.semantic_contracts.{contract_id} provenance source_symbols and "
                    f"bound_symbols must be disjoint; overlap={overlap}"
                )

            shared_scope_dimensions = set()
            source_by_dimension: dict[str, set[str]] = collections.defaultdict(set)
            bound_by_dimension: dict[str, set[str]] = collections.defaultdict(set)
            for symbol in source_symbols:
                for dimension in semantic_symbol_scopes(symbol):
                    source_by_dimension[dimension].add(symbol)
            for symbol in bound_symbols:
                for dimension in semantic_symbol_scopes(symbol):
                    bound_by_dimension[dimension].add(symbol)
            shared_scope_dimensions = set(source_by_dimension) & set(bound_by_dimension)

            bindings = entry.get("scope_bindings", [])
            if bindings is None:
                bindings = []
            seen_dimensions: set[str] = set()
            if isinstance(bindings, list):
                for index, binding in enumerate(bindings):
                    if not isinstance(binding, dict):
                        continue
                    dimension = binding.get("dimension")
                    if not isinstance(dimension, str):
                        continue
                    if dimension not in scope_dimensions:
                        errors.append(
                            f"catalog.semantic_contracts.{contract_id}.scope_bindings[{index}] references "
                            f"unknown catalog.scope_dimensions entry {dimension!r}"
                        )
                    if dimension in seen_dimensions:
                        errors.append(
                            f"catalog.semantic_contracts.{contract_id}.scope_bindings repeats dimension {dimension!r}"
                        )
                    seen_dimensions.add(dimension)
                    declared_sources = {
                        symbol for symbol in binding.get("source_symbols", []) if isinstance(symbol, str)
                    }
                    declared_bounds = {
                        symbol for symbol in binding.get("bound_symbols", []) if isinstance(symbol, str)
                    }
                    if not declared_sources <= source_symbols:
                        errors.append(
                            f"catalog.semantic_contracts.{contract_id}.scope_bindings[{index}].source_symbols "
                            "must be a subset of provenance source_symbols"
                        )
                    if not declared_bounds <= bound_symbols:
                        errors.append(
                            f"catalog.semantic_contracts.{contract_id}.scope_bindings[{index}].bound_symbols "
                            "must be a subset of provenance bound_symbols"
                        )
                    expected_sources = source_by_dimension.get(dimension, set())
                    expected_bounds = bound_by_dimension.get(dimension, set())
                    if declared_sources != expected_sources:
                        errors.append(
                            f"catalog.semantic_contracts.{contract_id}.scope_bindings[{index}] must enumerate "
                            f"all {dimension!r}-scoped source symbols; expected={sorted(expected_sources)!r} "
                            f"actual={sorted(declared_sources)!r}"
                        )
                    if declared_bounds != expected_bounds:
                        errors.append(
                            f"catalog.semantic_contracts.{contract_id}.scope_bindings[{index}] must enumerate "
                            f"all {dimension!r}-scoped bound symbols; expected={sorted(expected_bounds)!r} "
                            f"actual={sorted(declared_bounds)!r}"
                        )

            missing_scope_bindings = sorted(shared_scope_dimensions - seen_dimensions)
            for dimension in missing_scope_bindings:
                errors.append(
                    f"catalog.semantic_contracts.{contract_id} provenance crosses shared scope dimension "
                    f"{dimension!r} but scope_bindings does not preserve it"
                )
            extra_scope_bindings = sorted(seen_dimensions - shared_scope_dimensions)
            for dimension in extra_scope_bindings:
                errors.append(
                    f"catalog.semantic_contracts.{contract_id} scope_bindings declares dimension {dimension!r} "
                    "that is not present on both provenance source and bound sides"
                )

    if graph.repository_root is not None:
        referenced_contracts: set[str] = set()
        for claim in graph.claims.values():
            refs = claim.get("semantic_contracts", [])
            if isinstance(refs, list):
                referenced_contracts.update(ref for ref in refs if isinstance(ref, str))
        for contract_id in sorted(set(contracts) - referenced_contracts):
            warnings.append(
                f"catalog.semantic_contracts.{contract_id} is not referenced by any claim; "
                "verify that the relation is intentionally unused rather than a missing correctness proposition"
            )

    id_allocator = doc.get("id_allocator")
    next_sequence = id_allocator.get("next_sequence") if isinstance(id_allocator, dict) else None
    prefixes = ("A", "G", "C", "L", "D", "T")
    if not isinstance(next_sequence, dict):
        errors.append("id_allocator.next_sequence must be a mapping")
        next_sequence = {}

    strict_re = re.compile(r"^([AGCL])(\d+)_([a-z][a-z0-9]*(?:_[a-z0-9]+)*)$")
    observed_max = {"A": 0, "G": 0, "C": 0, "L": 0}
    for node_id, node in graph.nodes.items():
        match = strict_re.match(node_id)
        if not match:
            errors.append(f"{node_id}: node ID must follow <A|G|C|L><sequence>_<snake_case_slug>")
            continue
        prefix, seq_text, _slug = match.groups()
        observed_max[prefix] = max(observed_max[prefix], int(seq_text))
        expected_prefix = "A" if node.node_type == "assumption" else {
            "root": "G",
            "derived": "C",
            "leaf": "L",
        }.get(node.data.get("kind"))
        if expected_prefix and prefix != expected_prefix:
            errors.append(
                f"{node_id}: prefix {prefix} does not match node role; expected {expected_prefix}"
            )
    for prefix, maximum in observed_max.items():
        value = next_sequence.get(prefix) if isinstance(next_sequence, dict) else None
        if isinstance(value, int) and value <= maximum:
            errors.append(
                f"id_allocator.next_sequence.{prefix}={value} must be greater than existing max {maximum}"
            )

    decisions_for_ids = doc.get("refinement", {}).get("decisions", {}) if isinstance(doc.get("refinement"), dict) else {}
    decision_re = re.compile(r"^D(\d+)_([a-z][a-z0-9]*(?:_[a-z0-9]+)*)$")
    decision_max = 0
    if isinstance(decisions_for_ids, dict):
        for decision_id in decisions_for_ids:
            match = decision_re.match(decision_id)
            if not match:
                errors.append(f"{decision_id}: decision ID must follow D<sequence>_<snake_case_slug>")
                continue
            decision_max = max(decision_max, int(match.group(1)))
    value = next_sequence.get("D") if isinstance(next_sequence, dict) else None
    if isinstance(value, int) and value <= decision_max:
        errors.append(
            f"id_allocator.next_sequence.D={value} must be greater than existing max {decision_max}"
        )

    tooling_for_ids = doc.get("refinement", {}).get("tooling_blockers", {}) if isinstance(doc.get("refinement"), dict) else {}
    tooling_re = re.compile(r"^T(\d+)_([a-z][a-z0-9]*(?:_[a-z0-9]+)*)$")
    tooling_max = 0
    if isinstance(tooling_for_ids, dict):
        for blocker_id in tooling_for_ids:
            match = tooling_re.match(blocker_id)
            if not match:
                errors.append(f"{blocker_id}: tooling blocker ID must follow T<sequence>_<snake_case_slug>")
                continue
            tooling_max = max(tooling_max, int(match.group(1)))
    value = next_sequence.get("T") if isinstance(next_sequence, dict) else None
    if isinstance(value, int) and value <= tooling_max:
        errors.append(
            f"id_allocator.next_sequence.T={value} must be greater than existing max {tooling_max}"
        )

    overlap = set(graph.assumptions) & set(graph.claims)
    for node_id in sorted(overlap):
        errors.append(f"node ID exists as both claim and assumption: {node_id}")

    if len(graph.roots) != len(set(graph.roots)):
        errors.append("roots contains duplicate node IDs")

    for node_id, node in sorted(graph.nodes.items()):
        if not NODE_ID_RE.match(node_id):
            errors.append(f"{node_id}: invalid node ID format")
        if not isinstance(node.data, dict):
            errors.append(f"{node_id}: node body must be a mapping")
            continue
        statement = node.data.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            errors.append(f"{node_id}: missing non-empty statement")
        if "source_sections" in node.data:
            errors.append(f"{node_id}: source_sections is legacy; use source_refs")
        source_refs = node.data.get("source_refs", [])
        if not _as_string_list(source_refs) or not source_refs:
            errors.append(f"{node_id}: source_refs must be a non-empty list of catalog source IDs")
        else:
            known_sources = _catalog_namespace(graph, "sources")
            for ref in source_refs:
                if ref not in known_sources:
                    errors.append(f"{node_id}: unknown source ref {ref}")

        mechanisms = node.data.get("mechanisms", [])
        if node.node_type == "claim" and node.data.get("kind") == "leaf" and not mechanisms:
            errors.append(f"{node_id}: leaf claim must declare at least one mechanism")
        if mechanisms is not None:
            if not _as_string_list(mechanisms):
                errors.append(f"{node_id}: mechanisms must be a list of catalog mechanism IDs")
            else:
                known_mechanisms = _catalog_namespace(graph, "mechanisms")
                for mechanism in mechanisms:
                    if mechanism not in known_mechanisms:
                        errors.append(f"{node_id}: unknown mechanism {mechanism}")

        semantic_contracts = node.data.get("semantic_contracts", [])
        if semantic_contracts is not None:
            if not _as_string_list(semantic_contracts):
                errors.append(f"{node_id}: semantic_contracts must be a list of catalog semantic-contract IDs")
            else:
                known_contracts = _catalog_namespace(graph, "semantic_contracts")
                for contract in semantic_contracts:
                    if contract not in known_contracts:
                        errors.append(f"{node_id}: unknown semantic contract {contract}")

        known_symbols = (
            set(_catalog_namespace(graph, "terms"))
            | set(_catalog_namespace(graph, "state"))
            | set(_catalog_namespace(graph, "mechanisms"))
            | set(_catalog_namespace(graph, "semantic_contracts"))
        )
        for field in ("statement", "formal_intent"):
            value = node.data.get(field)
            if not isinstance(value, str):
                continue
            for token in sorted(set(SNAKE_CASE_TERM_RE.findall(value))):
                if token not in known_symbols:
                    errors.append(f"{node_id}: undefined snake_case term {token!r}; define it in catalog before using it")

        deps_raw = node.data.get("depends_on", [])
        if not _as_string_list(deps_raw):
            errors.append(f"{node_id}: depends_on must be a list of node IDs")
            continue
        if len(deps_raw) != len(set(deps_raw)):
            errors.append(f"{node_id}: depends_on contains duplicates")
        if node_id in deps_raw:
            errors.append(f"{node_id}: self-dependency is not allowed")
        for dep in deps_raw:
            if dep not in graph.nodes:
                errors.append(f"{node_id}: dangling dependency {dep}")

        if node.node_type == "assumption":
            if deps_raw:
                errors.append(
                    f"{node_id}: assumptions are terminal boundaries; convert it to a claim if it must be decomposed"
                )
            status = node.data.get("status")
            if not isinstance(status, str) or not status:
                errors.append(f"{node_id}: assumption must declare status")
            continue

        kind = node.data.get("kind")
        if kind not in CLAIM_KINDS:
            errors.append(f"{node_id}: claim kind must be one of {sorted(CLAIM_KINDS)}")
            continue

        if kind in {"root", "derived"}:
            if not deps_raw:
                errors.append(f"{node_id}: {kind} claim must declare dependencies")

        if kind == "leaf":
            if deps_raw:
                errors.append(f"{node_id}: leaf claims are terminal proof boundaries and may not declare depends_on")
            verification = node.data.get("verification")
            if not isinstance(verification, dict):
                errors.append(f"{node_id}: leaf claim must declare verification")
            else:
                verifiers = verification.get("verifiers")
                if not isinstance(verifiers, list) or not verifiers:
                    errors.append(f"{node_id}: leaf claim must declare at least one verifier")
                else:
                    for index, verifier in enumerate(verifiers):
                        if not isinstance(verifier, dict):
                            errors.append(f"{node_id}: verifier[{index}] must be a mapping")
                            continue
                        if "type" in verifier:
                            errors.append(f"{node_id}: verifier[{index}].type is legacy; use kind")
                        kind_name = verifier.get("kind")
                        if not isinstance(kind_name, str) or not kind_name:
                            errors.append(f"{node_id}: verifier[{index}] missing kind")
                        elif kind_name not in _catalog_namespace(graph, "verifier_kinds"):
                            errors.append(f"{node_id}: verifier[{index}] unknown kind {kind_name}")
                        intent = verifier.get("intent")
                        if not isinstance(intent, str) or not intent:
                            errors.append(f"{node_id}: verifier[{index}] missing intent")
                        else:
                            for token in sorted(set(SNAKE_CASE_TERM_RE.findall(intent))):
                                if token not in known_symbols:
                                    errors.append(
                                        f"{node_id}: verifier[{index}] uses undefined snake_case term {token!r}; define it in catalog first"
                                    )

    _validate_source_traceability(graph, errors)

    root_set = set(graph.roots)
    for root in graph.roots:
        if root not in graph.claims:
            errors.append(f"root {root} is not a claim")
        elif graph.claims[root].get("kind") != "root":
            errors.append(f"root {root} does not have kind: root")
    for node_id, claim in graph.claims.items():
        if claim.get("kind") == "root" and node_id not in root_set:
            errors.append(f"{node_id}: kind root but missing from roots list")

    # Detect cycles with explicit DFS path reporting.
    color: dict[str, int] = {node_id: 0 for node_id in graph.nodes}
    stack: list[str] = []

    def dfs(node_id: str):
        color[node_id] = 1
        stack.append(node_id)
        for dep in graph.nodes[node_id].dependencies:
            if dep not in graph.nodes:
                continue
            if color[dep] == 0:
                dfs(dep)
            elif color[dep] == 1:
                start = stack.index(dep)
                cycle = stack[start:] + [dep]
                errors.append("dependency cycle: " + " -> ".join(cycle))
        stack.pop()
        color[node_id] = 2

    for node_id in sorted(graph.nodes):
        if color[node_id] == 0:
            dfs(node_id)

    # Validation must keep collecting diagnostics even when dangling edges exist.
    def safe_closure(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            node_id = stack.pop()
            if node_id in seen or node_id not in graph.nodes:
                continue
            seen.add(node_id)
            stack.extend(graph.nodes[node_id].dependencies)
        return seen

    # Every claim should contribute to at least one root argument.
    reachable: set[str] = set()
    for root in graph.roots:
        if root in graph.nodes:
            reachable |= safe_closure(root)
    for node_id in sorted(set(graph.claims) - reachable):
        errors.append(f"{node_id}: claim is unreachable from every root")
    for node_id in sorted(set(graph.assumptions) - reachable):
        warnings.append(f"{node_id}: assumption is currently unused by every root")

    # A root closure must terminate in explicit proof/evidence boundaries.
    for root in graph.roots:
        if root not in graph.nodes:
            continue
        closure = safe_closure(root)
        boundaries = [
            node_id
            for node_id in closure
            if graph.nodes[node_id].node_type == "assumption"
            or graph.nodes[node_id].data.get("kind") == "leaf"
        ]
        if not boundaries:
            errors.append(f"{root}: root closure has no leaf or assumption boundary")

    if graph.repository_root is not None:
        from .symbolic.formal_validation import validate_formal_layer

        formal_errors, formal_warnings = validate_formal_layer(graph)
        errors.extend(f"formal: {error}" for error in formal_errors)
        warnings.extend(f"formal: {warning}" for warning in formal_warnings)

    return errors, warnings


def _print_validation(graph: Graph) -> int:
    errors, warnings = validate(graph)
    edge_count = sum(len(node.dependencies) for node in graph.nodes.values())
    leaf_count = sum(
        1 for node in graph.claims.values() if node.get("kind") == "leaf"
    )
    print(
        f"nodes={len(graph.nodes)} claims={len(graph.claims)} assumptions={len(graph.assumptions)} "
        f"roots={len(graph.roots)} leaves={leaf_count} edges={edge_count}"
    )
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        print(f"INVALID: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"VALID: 0 errors, {len(warnings)} warning(s)")
    return 0



def _schema_constraint_summary(schema: dict[str, Any]) -> str:
    if "const" in schema:
        return f"const={schema['const']!r}"
    if "enum" in schema:
        return "enum=" + "|".join(str(v) for v in schema["enum"])
    schema_type = schema.get("type")
    if schema_type == "string":
        parts = ["string"]
        if schema.get("minLength") is not None:
            parts.append(f"minLength={schema['minLength']}")
        if schema.get("pattern"):
            parts.append(f"pattern={schema['pattern']}")
        return ", ".join(parts)
    if schema_type == "integer":
        parts = ["integer"]
        if schema.get("minimum") is not None:
            parts.append(f"minimum={schema['minimum']}")
        return ", ".join(parts)
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "array":
        parts = ["array"]
        if schema.get("minItems") is not None:
            parts.append(f"minItems={schema['minItems']}")
        if schema.get("uniqueItems"):
            parts.append("uniqueItems=true")
        item = schema.get("items")
        if isinstance(item, dict):
            if item.get("type"):
                parts.append(f"items={item['type']}")
            if item.get("pattern"):
                parts.append(f"itemPattern={item['pattern']}")
        return ", ".join(parts)
    if schema_type == "object":
        if schema.get("patternProperties"):
            patterns = "|".join(schema["patternProperties"].keys())
            return f"closed object; dynamic keys match {patterns}"
        return "closed object"
    return "composite constraint"


def _condition_summary(condition: Any) -> str:
    if not isinstance(condition, dict):
        return "condition"
    properties = condition.get("properties")
    if not isinstance(properties, dict):
        return "condition"
    parts: list[str] = []
    for field, constraint in properties.items():
        if not isinstance(constraint, dict):
            continue
        if "const" in constraint:
            parts.append(f"{field}={constraint['const']}")
        elif "enum" in constraint:
            parts.append(f"{field} in {'|'.join(str(v) for v in constraint['enum'])}")
    return " and ".join(parts) or "condition"


def _presence_rules(schema: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    required_when: dict[str, list[str]] = {}
    forbidden_when: dict[str, list[str]] = {}
    for clause in schema.get("allOf", []) or []:
        if not isinstance(clause, dict):
            continue
        condition = _condition_summary(clause.get("if"))
        then = clause.get("then")
        if not isinstance(then, dict):
            continue
        for field in then.get("required", []) or []:
            required_when.setdefault(str(field), []).append(condition)
        not_schema = then.get("not")
        if isinstance(not_schema, dict):
            for field in not_schema.get("required", []) or []:
                forbidden_when.setdefault(str(field), []).append(condition)
    return required_when, forbidden_when


def _canonical_field_inventory() -> list[dict[str, Any]]:
    """Return one deduplicated row per canonical YAML field path, derived only from the graph schema."""
    rows: dict[str, dict[str, Any]] = {}

    def walk(schema: Any, path: tuple[str, ...]) -> None:
        if not isinstance(schema, dict):
            return
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        required_when, forbidden_when = _presence_rules(schema)
        if isinstance(properties, dict):
            for name, child in properties.items():
                field_path = path + (name,)
                rendered = ".".join(field_path)
                if isinstance(child, dict):
                    if name in required:
                        presence = "required"
                    elif name in required_when:
                        presence = "required when " + " or ".join(required_when[name])
                    else:
                        presence = "optional"
                    if name in forbidden_when:
                        presence += "; forbidden when " + " or ".join(forbidden_when[name])
                    rows.setdefault(
                        rendered,
                        {
                            "path": rendered,
                            "presence": presence,
                            "constraint": _schema_constraint_summary(child),
                            "description": child.get("description", ""),
                        },
                    )
                    walk(child, field_path)
        patterns = schema.get("patternProperties", {})
        if isinstance(patterns, dict):
            for pattern, child in patterns.items():
                dynamic = f"<key:{pattern}>"
                walk(child, path + (dynamic,))
        items = schema.get("items")
        if isinstance(items, dict):
            walk(items, path + ("[]",))

    walk(CANONICAL_GRAPH_SCHEMA, ())
    return [rows[key] for key in sorted(rows)]


def _schema_reference_contract_errors(path: Path = SCHEMA_REFERENCE_PATH) -> list[str]:
    """Check SCHEMA.md's compact machine index against the canonical graph schema.

    The prose remains intentionally human-maintained. This only guarantees that every
    canonical field path appears exactly once and that its presence/structural constraint
    summary has not drifted from graph_schema.py.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"schema reference unavailable at {path}: {exc}"]

    if text.count(SCHEMA_INDEX_START) != 1 or text.count(SCHEMA_INDEX_END) != 1:
        return [
            "SCHEMA.md must contain exactly one canonical schema index bounded by "
            f"{SCHEMA_INDEX_START!r} and {SCHEMA_INDEX_END!r}"
        ]

    remainder = text.split(SCHEMA_INDEX_START, 1)[1]
    index_text = remainder.split(SCHEMA_INDEX_END, 1)[0]

    documented: dict[str, list[str]] = collections.defaultdict(list)
    malformed_lines: list[str] = []
    for raw_line in index_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = SCHEMA_INDEX_LINE_RE.fullmatch(line)
        if match is None:
            malformed_lines.append(line)
            continue
        documented[match.group(1)].append(match.group(2).strip())

    errors: list[str] = []
    for line in malformed_lines:
        errors.append(f"SCHEMA.md canonical schema index has malformed line: {line}")

    expected = {
        row["path"]: f"{row['presence']}; {row['constraint']}"
        for row in _canonical_field_inventory()
    }
    documented_paths = set(documented)
    expected_paths = set(expected)

    for field_path in sorted(expected_paths - documented_paths):
        errors.append(f"SCHEMA.md canonical schema index missing field: {field_path}")
    for field_path in sorted(documented_paths - expected_paths):
        errors.append(f"SCHEMA.md canonical schema index has stale/unknown field: {field_path}")
    for field_path in sorted(expected_paths & documented_paths):
        values = documented[field_path]
        if len(values) != 1:
            errors.append(
                f"SCHEMA.md canonical schema index must document {field_path} exactly once; found {len(values)}"
            )
            continue
        if values[0] != expected[field_path]:
            errors.append(
                f"SCHEMA.md canonical schema constraint drift for {field_path}: "
                f"documented={values[0]!r}, expected={expected[field_path]!r}"
            )
    return errors


