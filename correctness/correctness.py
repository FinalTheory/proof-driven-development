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


GRAPH_PATH = Path(__file__).with_name("correctness.yaml")


def _repository_root_for_graph_path(path: Path) -> Path:
    """Resolve repository root for canonical and isolated-test graph layouts."""
    return path.parent.parent if path.parent.name == "correctness" else path.parent

NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
CLAIM_KINDS = {"root", "derived", "leaf"}
CATALOG_NAMESPACES = (
    "terms", "state", "mechanisms", "semantic_contracts", "failure_events", "surfaces", "verifier_kinds", "sources"
)
SEMANTIC_CATALOG_NAMESPACES = ("terms", "state", "mechanisms", "semantic_contracts")
SNAKE_CASE_TERM_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

AUDIT_INTERPRETATION_RULES = (
    "Node IDs are navigation labels; the node statement is the authoritative proposition and must not be strengthened from the ID wording.",
    "Catalog term/state/mechanism entries define vocabulary and architecture primitives, not premises that a correctness property already holds.",
    "Every snake_case protocol/state shorthand used in a proposition must resolve to the typed catalog; undefined identifiers have no inferred semantics.",
    "Automation may reuse only approved catalog mechanisms. If a proof hole requires a new architecture mechanism or system-semantic choice, escalate to a human decision.",
    "Verifier descriptions are planned future evidence, not evidence that the proposition has already been established; acceptable evidence must observe the claim independently rather than merely restate the mechanism under test.",
)

# Tool-level proof-boundary taxonomy. These are not system semantics and therefore do not
# belong in correctness.yaml. workflow-help and audit-prompt render this same registry.
LEAF_STOPPING_SIGNALS: dict[str, str] = {
    "deterministic_transition": "The complete proposition is a pure-function or deterministic state-transition property with explicit inputs and a mechanically checkable output relation.",
    "schema_constraint": "A database/schema/type constraint can directly establish the complete proposition from authoritative implementation metadata.",
    "control_flow": "A localized control-flow dominance, reachability, or ordering analysis can decide the complete proposition.",
    "static_dataflow": "A localized static dataflow, provenance, or taint analysis can decide the complete proposition.",
    "bounded_state_machine": "A bounded state-machine/model checker can decide the complete proposition without reconstructing the parent protocol.",
    "symbolic_execution": "A localized symbolic execution or equivalent path proof can decide the complete proposition.",
    "differential_property": "A localized differential/property checker can decide the complete proposition over its implementation surface.",
}


def _render_leaf_stopping_signals(*, indent: str = "") -> str:
    return "\n".join(f"{indent}- `{key}`: {description}" for key, description in LEAF_STOPPING_SIGNALS.items())


MUTATION_PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:google-docs-correctness:mutation-plan:v1",
    "title": "Correctness DAG mutation plan",
    "type": "object",
    "additionalProperties": False,
    "required": ["mutation_version", "authority", "operations"],
    "properties": {
        "mutation_version": {"const": 1},
        "authority": {"enum": ["automation", "human"]},
        "preconditions": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "semantic_signatures": {
                    "type": "object",
                    "propertyNames": {"pattern": "^[GCL][0-9]+_[a-z][a-z0-9_]*$"},
                    "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                "model_signature": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
        "operations": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/operation"},
        },
    },
    "allOf": [
        {
            "if": {"properties": {"authority": {"const": "automation"}}},
            "then": {
                "required": ["preconditions"],
                "properties": {
                    "preconditions": {
                        "anyOf": [
                            {"required": ["semantic_signatures"], "properties": {"semantic_signatures": {"minProperties": 1}}},
                            {"required": ["model_signature"]},
                        ]
                    }
                },
            },
        }
    ],
    "$defs": {
        "slug": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        },
        "alias": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        },
        "ref": {"type": "string", "minLength": 1},
        "nonempty_string": {"type": "string", "minLength": 1},
        "string_list": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "claim_body": MUTATION_CLAIM_BODY_SCHEMA,
        "claim_update": MUTATION_CLAIM_UPDATE_SCHEMA,
        "claim_field_list": {
            "type": "array",
            "items": {"enum": list(CLAIM_MUTABLE_FIELDS)},
            "uniqueItems": True,
        },
        "operation": {
            "oneOf": [
                {"$ref": "#/$defs/add_claim"},
                {"$ref": "#/$defs/reclassify_claim"},
                {"$ref": "#/$defs/update_claim"},
                {"$ref": "#/$defs/add_dependency"},
                {"$ref": "#/$defs/remove_dependency"},
                {"$ref": "#/$defs/remove_claim"},
                {"$ref": "#/$defs/set_refinement"},
                {"$ref": "#/$defs/set_specification_coverage"},
                {"$ref": "#/$defs/set_composition_assurance"},
                {"$ref": "#/$defs/set_evidence_assurance"},
                {"$ref": "#/$defs/add_decision"},
                {"$ref": "#/$defs/resolve_decision"},
                {"$ref": "#/$defs/block_node"},
                {"$ref": "#/$defs/unblock_node"},
                {"$ref": "#/$defs/add_tooling_blocker"},
                {"$ref": "#/$defs/resolve_tooling_blocker"},
                {"$ref": "#/$defs/add_assumption"},
                {"$ref": "#/$defs/set_value"},
                {"$ref": "#/$defs/unset_value"},
            ]
        },
        "add_claim": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "kind", "slug", "body"],
            "properties": {
                "op": {"const": "add_claim"},
                "alias": {"$ref": "#/$defs/alias"},
                "kind": {"enum": ["root", "derived", "leaf"]},
                "slug": {"$ref": "#/$defs/slug"},
                "body": {"$ref": "#/$defs/claim_body"},
            },
        },
        "reclassify_claim": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node", "new_kind"],
            "properties": {
                "op": {"const": "reclassify_claim"},
                "node": {"$ref": "#/$defs/ref"},
                "new_kind": {"enum": ["root", "derived", "leaf"]},
                "slug": {"$ref": "#/$defs/slug"},
                "set": {"$ref": "#/$defs/claim_update"},
                "alias": {"$ref": "#/$defs/alias"},
            },
        },
        "update_claim": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node"],
            "anyOf": [{"required": ["set"]}, {"required": ["unset"]}],
            "properties": {
                "op": {"const": "update_claim"},
                "node": {"$ref": "#/$defs/ref"},
                "set": {"$ref": "#/$defs/claim_update"},
                "unset": {"$ref": "#/$defs/claim_field_list"},
            },
        },
        "add_dependency": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node", "dependency"],
            "properties": {
                "op": {"const": "add_dependency"},
                "node": {"$ref": "#/$defs/ref"},
                "dependency": {"$ref": "#/$defs/ref"},
            },
        },
        "remove_dependency": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node", "dependency"],
            "properties": {
                "op": {"const": "remove_dependency"},
                "node": {"$ref": "#/$defs/ref"},
                "dependency": {"$ref": "#/$defs/ref"},
            },
        },
        "remove_claim": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node"],
            "properties": {
                "op": {"const": "remove_claim"},
                "node": {"$ref": "#/$defs/ref"},
            },
        },
        "set_refinement": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node"],
            "anyOf": [
                {"required": ["status"]}, {"required": ["blocked_by"]}, {"required": ["rationale"]}
            ],
            "properties": {
                "op": {"const": "set_refinement"},
                "node": {"$ref": "#/$defs/ref"},
                "status": {"enum": ["pending", "stable", "waived"]},
                "blocked_by": {"type": "array", "items": {"$ref": "#/$defs/ref"}, "uniqueItems": True},
                "rationale": {"$ref": "#/$defs/nonempty_string"},
            },
        },
        "set_specification_coverage": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "status"],
            "properties": {
                "op": {"const": "set_specification_coverage"},
                "status": {"enum": ["unaudited", "gap_found", "closed"]},
                "auditor_count": {"type": "integer", "minimum": 1},
                "rationale": {"$ref": "#/$defs/nonempty_string"},
            },
        },
        "set_composition_assurance": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node", "status"],
            "properties": {
                "op": {"const": "set_composition_assurance"},
                "node": {"$ref": "#/$defs/ref"},
                "status": {"enum": ["unaudited", "single_agent_audited", "multi_agent_audited", "machine_checked"]},
                "auditor_count": {"type": "integer", "minimum": 1},
                "artifact_refs": {"$ref": "#/$defs/string_list"},
                "rationale": {"$ref": "#/$defs/nonempty_string"},
            },
        },
        "set_evidence_assurance": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node", "status"],
            "properties": {
                "op": {"const": "set_evidence_assurance"},
                "node": {"$ref": "#/$defs/ref"},
                "status": {"enum": ["planned", "implemented", "passing", "failing"]},
                "implementation_revision": {"$ref": "#/$defs/nonempty_string"},
                "artifact_refs": {"$ref": "#/$defs/string_list"},
                "rationale": {"$ref": "#/$defs/nonempty_string"},
            },
        },
        "add_decision": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "slug", "body"],
            "properties": {
                "op": {"const": "add_decision"},
                "alias": {"$ref": "#/$defs/alias"},
                "slug": {"$ref": "#/$defs/slug"},
                "body": MUTATION_DECISION_BODY_SCHEMA,
            },
        },
        "resolve_decision": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "decision", "resolution"],
            "properties": {
                "op": {"const": "resolve_decision"},
                "decision": {"$ref": "#/$defs/ref"},
                "resolution": {"$ref": "#/$defs/nonempty_string"},
            },
        },
        "block_node": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node", "decision"],
            "properties": {
                "op": {"const": "block_node"},
                "node": {"$ref": "#/$defs/ref"},
                "decision": {"$ref": "#/$defs/ref"},
            },
        },
        "unblock_node": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "node", "decision"],
            "properties": {
                "op": {"const": "unblock_node"},
                "node": {"$ref": "#/$defs/ref"},
                "decision": {"$ref": "#/$defs/ref"},
            },
        },
        "add_tooling_blocker": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "slug", "body"],
            "properties": {
                "op": {"const": "add_tooling_blocker"},
                "alias": {"$ref": "#/$defs/alias"},
                "slug": {"$ref": "#/$defs/slug"},
                "body": MUTATION_TOOLING_BLOCKER_BODY_SCHEMA,
            },
        },
        "resolve_tooling_blocker": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "blocker", "resolution"],
            "properties": {
                "op": {"const": "resolve_tooling_blocker"},
                "blocker": {"$ref": "#/$defs/ref"},
                "resolution": {"$ref": "#/$defs/nonempty_string"},
            },
        },
        "add_assumption": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "slug", "body"],
            "properties": {
                "op": {"const": "add_assumption"},
                "alias": {"$ref": "#/$defs/alias"},
                "slug": {"$ref": "#/$defs/slug"},
                "body": MUTATION_ASSUMPTION_BODY_SCHEMA,
            },
        },
        "set_value": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "path", "value"],
            "properties": {
                "op": {"const": "set_value"},
                "path": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                "value": {},
            },
        },
        "unset_value": {
            "type": "object", "additionalProperties": False,
            "required": ["op", "path"],
            "properties": {
                "op": {"const": "unset_value"},
                "path": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            },
        },
    },
}

Draft202012Validator.check_schema(CANONICAL_GRAPH_SCHEMA)
_CANONICAL_GRAPH_VALIDATOR = Draft202012Validator(CANONICAL_GRAPH_SCHEMA)
Draft202012Validator.check_schema(MUTATION_PLAN_SCHEMA)
_MUTATION_PLAN_VALIDATOR = Draft202012Validator(MUTATION_PLAN_SCHEMA)


WORKFLOW_HELP = r"""# Automated Correctness Refinement Workflow

This tool is the canonical control interface for adversarial refinement of
`correctness.yaml`. Agents should not rely on prior chat context or old audit results.

## 1. Canonical artifacts

- `correctness.yaml`: correctness DAG + refinement control state + orthogonal assurance state.
- `correctness.py`: deterministic validator, slicer, scheduler, signature calculator,
  and agent-task generator.
- Source article: declared by `source.article` in `correctness.yaml`.

Correctness propositions live under `claims` / `assumptions`.
Workflow state lives under `refinement`. Assurance state lives separately under `assurance`.
Do not encode either into claim statements, and never treat refinement/assurance metadata as a proof premise.
`refinement.nodes.*.status: stable` means only that proposition/decomposition refinement has converged
for the recorded semantic signature; it does not mean the proposition is true in implementation or that
its dependency composition has independent certification.

## 2. Mandatory clean-context isolation

Every runnable audit MUST be performed by a NEW isolated sub-agent with no prior
conversation, no earlier audit transcript, and no knowledge of why the node was created.
Do not reuse an agent across nodes or refinement rounds.

The clean verifier may use general distributed-systems knowledge to understand terms and
construct executions, but it must not invent guarantees absent from its generated task.

For a runnable node NODE, the clean sub-agent should run:

    python3 correctness.py audit-prompt NODE

and treat that output as its complete audit specification. The clean verifier must not read the
full correctness.yaml, README, source article, prior audits, mutation history, or sibling-agent
results at any later phase. Full-graph reuse and patch placement belong exclusively to the
long-lived orchestrator.

## 3. Orchestrator loop

Always start a round with:

    python3 correctness.py validate
    python3 correctness.py refinement-status --format compact-yaml

Interpret `state` mechanically:

- `CONTINUE`: spawn fresh sub-agents for runnable frontier nodes. Independent runnable
  nodes may be audited in parallel.
- `HUMAN_BLOCKED`: stop automation and return the open human decisions. This state means
  unresolved work remains but every currently reachable audit frontier is blocked by
  semantic decisions.
- `TOOLING_BLOCKED`: stop the entire campaign immediately. A verifier/orchestrator has
  identified a limitation or policy in `correctness.py`, its mutation schema, scheduler,
  naming rules, or generated audit contract that prevents faithful correctness work. Human
  tooling repair is required before any branch continues.
- `COMPLETE`: stop successfully; no pending or stale refinement work remains.
- `STALLED`: stop as an unexpected workflow-state inconsistency. This is distinct from an
  explicit `TOOLING_BLOCKED` report.

Scheduling is bottom-up. A parent claim does not become runnable until all claim
dependencies are `stable` or `waived`. An open human decision blocks only the affected
branch; other runnable branches must continue.

## 3.1 Orchestrator as skeptical judge/compiler

The long-lived orchestrator is intentionally NOT clean-context. It may retain the full campaign
history, read the complete DAG, source article, previous audits, mutation history, and current
workflow state. Its role is semantic judge + compiler, not passive relay.

A clean sub-agent's result is advisory evidence, never authority. Before applying any suggested
refinement, the orchestrator MUST independently re-check at least:

- whether the strongest counterexample is actually consistent with the current specification;
- whether the reported defect follows from the exact target statement rather than node-name intuition;
- whether proposed child propositions have genuinely independent failure/verifier/invalidation surfaces;
- whether an existing DAG node already captures the needed proposition;
- whether the proposal is the smallest useful refinement rather than decomposition bureaucracy;
- whether any proposed change silently changes a root guarantee, assumption, failure model, protocol
  horizon, source-of-truth boundary, or other system semantics.

The orchestrator should be adversarial toward the recommendation: reject, narrow, or rewrite it when
its reasoning is weak. It should not mutate the graph merely because a clean verifier recommended a
patch. Conversely, it should not preserve the existing graph merely because it authored or previously
accepted it.

The clean sub-agent MUST produce and fix its local verdict using only `audit-prompt NODE`. The
orchestrator MAY later debate that result with the same sub-agent by presenting targeted objections
or counterexamples, but should not dump the full DAG into that sub-agent. Once debate begins, the
sub-agent is no longer an independent clean verifier. If a new independent judgment is materially
useful, spawn another brand-new clean sub-agent and give it only the canonical `audit-prompt NODE`
task.

Only the orchestrator compiles an accepted semantic conclusion into an executable mutation plan. The
sub-agent proposes reasoning-level changes; the orchestrator transforms the accepted subset into
structured `mutate` operations, attaches current semantic-signature preconditions, and submits the plan
through the locked mutation API. A rejected or superseded recommendation produces no graph mutation.

When several clean audits complete in parallel, the orchestrator may judge them in any order, but it
must treat each result as based on the signature captured when that audit started. `STALE_MUTATION`
means the relevant branch changed before commit; do not force-apply the old recommendation. Re-audit or
re-judge it against the new graph state.

## 4. Audit result classes

A leaf-boundary audit asks whether the leaf is already local enough to be checked by a
mechanically observable verifier without re-reasoning the whole parent protocol. A leaf does NOT
need to be logically atomic. If the complete proposition can already be decided by one localized,
deterministic implementation-level verifier, that is a strong stopping signal even when the verifier
internally contains several assertions.
Typical outcomes:

- `GOOD_LEAF`: keep the node as a leaf; mark it stable. Prefer this when one localized mechanical
  checker could establish the whole proposition directly—for example a pure deterministic transition
  check, schema/constraint inspection, control-flow dominance/reachability proof, static dataflow/taint
  check, bounded state-machine/model check, symbolic check, or similarly scoped implementation proof.
- `SHOULD_DECOMPOSE`: split it into the smallest independent propositions only when decomposition
  creates materially different verification or invalidation boundaries rather than merely exposing
  assertions that one localized verifier could check together.
- `HUMAN_SEMANTIC_DECISION`: proof progress requires a new protocol/product/failure-model
  choice not already entailed by the current specification.
- `TOOLING_BLOCKED`: the agent believes a script/tooling constraint or missing capability
  makes faithful analysis/refinement impossible or would force an incorrect workaround.
  Stop the entire campaign and escalate the tooling issue for human repair.
- `INCOMPLETE_CONTEXT`: the generated slice itself lacks necessary neutral definitions;
  repair the typed `system` / `scope` context or the generated task specification rather than inventing a guarantee.

A proof audit for a root/derived claim asks whether its declared dependencies are
sufficient and appropriately narrow. Typical defects include `MISSING_EDGE`,
`MISSING_NODE`, `OVER_STRONG_DEPENDENCY`, `WRONG_DEPENDENCY`, and `CLAIM_TOO_BROAD`.

## 5. What automation may change

Automation MAY make semantics-preserving proof-structure refinements, for example:

- split a broad claim into narrower propositions;
- add a missing dependency that is already entailed by the documented design;
- reuse an existing narrower proposition;
- remove a dependency proven stronger or unrelated to the parent statement;
- add/adjust verifier descriptions for a leaf;
- update refinement metadata and stable signatures.

Automation MUST create an open human decision instead of silently choosing when a change
would alter the system specification, including:

- changing a root guarantee;
- adding a new protocol/environment/external assumption;
- changing the failure model or source-of-truth boundary;
- choosing a retry/retention/availability/consistency horizon;
- selecting an architecture/product tradeoff with more than one valid design;
- narrowing the guarantee merely to make a proof easier.

## 5.1 Typed architecture catalog and synthesis boundary

System vocabulary is centrally defined under `catalog`. Claims may reference catalog mechanisms,
semantic contracts, implementation surfaces, source sections, and verifier kinds, while underscore-style
protocol terms must resolve to a catalog term/state/mechanism/semantic-contract definition. Semantic
contracts define reusable proposition meaning (for example an observation or equivalence boundary); they
are not proof premises. Automation may reuse only mechanisms whose `automation_reusable` flag is true,
and may reference an approved reusable semantic contract when creating a new proof-structure claim.
Changing or removing the semantic-contract association of an existing claim requires human authority,
because that changes the proposition's interpretation boundary. A semantic-contract association is never
hidden metadata: every referenced contract ID must appear exactly in the claim's `statement` or `formal_intent`
and in at least one of that claim's `source_refs` article sections. `catalog.sources` stable semantic IDs resolve through exact H2 `heading` text; numeric Markdown section prefixes are presentation-only and ignored. Automation may not create catalog entries or introduce an
unapproved mechanism/contract merely to close a proof hole; report a human semantic decision instead.

Verifier `kind` values come from the small canonical `catalog.verifier_kinds` taxonomy. Scenario-specific
details belong in verifier `intent`, not in newly invented verifier kinds.

## 5.2 Closed graph schema, single-writer mutation, and node IDs

`correctness.yaml` is a closed typed document, not an extensible metadata bag. The canonical graph
JSON Schema rejects unknown fields and constrains enums/types before graph-semantic validation runs:

    python3 correctness.py graph-schema --format yaml
    python3 correctness.py graph-schema --format json

For a deduplicated human-readable inventory of every canonical YAML field path, including presence rules, structural constraints, and semantic descriptions:

    python3 correctness.py schema-fields --format text

Only system/proof semantics and mutable campaign state belong in the YAML. Tool-intrinsic rules such as bottom-up scheduling, stop-state names, node-prefix meanings, dependency direction, audit interpretation rules, and the signature algorithm are defined by this program and are intentionally not duplicated as editable YAML policy. `id_allocator.next_sequence` remains YAML state because the writer must persist allocation progress.

Agents MUST NOT edit `correctness.yaml` directly. The canonical mutation-plan contract is also
machine-readable JSON Schema and is available with:

    python3 correctness.py mutation-schema --format yaml
    python3 correctness.py mutation-schema --format json

A long-lived orchestrator should load these schemas once per tooling revision/session and cache them.
Do not re-read the full schema before every graph mutation; reload it only after correctness tooling
changes or when a schema mismatch indicates that the cached contract is stale.

Every structured graph/refinement mutation must then enter through:

    python3 correctness.py mutate --plan PLAN.yaml

or stdin with `--plan -`. `mutate` validates the plan against the same in-process schema object before
performing semantic/authority checks, so the displayed schema and the runtime structural contract
share one source of truth.

`mutate` acquires an exclusive filesystem lock, reloads the latest graph, checks semantic
signature preconditions, applies structured operations in memory, validates the full DAG and
refinement control state, and only then atomically replaces `correctness.yaml`. Invalid or
stale mutations are rejected without modifying the file.

Automation mutation plans must include at least one branch-local semantic signature copied
from `refinement-status --format compact-yaml`. This is optimistic concurrency control: unrelated
branches may commit concurrently in sequence, but a result derived from a branch that changed
while its sub-agent was running is rejected as `STALE_MUTATION`.

Node IDs are allocated only by the mutation tool under the same lock. Agents provide a
semantic lower-snake-case `slug`, never a prefix or sequence number. Canonical meanings are:

- `A<n>_...`: Assumption (terminal correctness boundary)
- `G<n>_...`: Goal / root claim
- `C<n>_...`: Composite / derived claim
- `L<n>_...`: Leaf proof obligation
- `D<n>_...`: Human semantic decision
- `T<n>_...`: Global tooling/control-plane blocker

Sequences are monotonically allocated from `id_allocator.next_sequence` and are never
reused merely because an older number is absent. Changing a claim kind must use the
`reclassify_claim` mutation op so a new correctly-prefixed ID is allocated and references are
rewritten atomically.

Automation may use these structured operations: `add_claim`, `reclassify_claim`,
`update_claim`, `add_dependency`, `remove_dependency`, `remove_claim`, `set_refinement`,
`set_specification_coverage`, `set_composition_assurance`, `set_evidence_assurance`,
`add_decision`, `resolve_decision`, `block_node`, `unblock_node`, and `add_tooling_blocker`.
`resolve_tooling_blocker`, `add_assumption`, and arbitrary `set_value` are human-authority
operations because they either resume a globally stopped campaign or may change specification
semantics.

A mutation plan has this general form:

    mutation_version: 1
    authority: automation
    preconditions:
      semantic_signatures:
        SOME_RUNNABLE_NODE: <signature from refinement-status>
    operations:
      - op: add_claim
        alias: child
        kind: leaf
        slug: durable_example_invariant
        body:
          statement: >-
            ...
          severity: critical
          assurance_required: strong
          source_refs: [accepted_change_log]
          surfaces: [acceptance_path]
          mechanisms: [acceptance_transaction]
          verification:
            verifiers:
              - kind: integration_test
                intent: "..."
      - op: add_dependency
        node: SOME_RUNNABLE_NODE
        dependency: "@child"

Aliases beginning with `@` may refer to IDs allocated earlier in the same mutation plan.
After every graph-only mutation, rerun `validate` and compact `refinement-status`. The tool
regression suite is NOT a per-DAG-mutation gate: it tests `correctness.py` and its control-plane
machinery, not the current production DAG shape. Run the full unit-test suite whenever tooling
changes (`correctness.py`, mutation schema, validator, scheduler, mutation engine, or test
infrastructure), or when a tooling repair is being validated. Ordinary automated graph mutations
should commit once through `mutate`; `--dry-run` is an optional debugging/inspection aid, not a
mandatory pre-commit pass.

## 6. Human decisions

Before creating a human semantic decision, the orchestrator must classify why proof progress stopped:

1. **Genuine architecture/product choice** — the current specification really permits multiple
   materially different semantics or mechanisms. Create a human decision.
2. **Specification wording repair** — the current architecture already determines the intended
   semantics, but a root/claim is ambiguous, over-broad, or uses an undefined observer/horizon.
   Repair the specification with human authority; do not manufacture several fake product options.
3. **Decomposition artifact** — the apparent choice exists only because a proposition was split below
   a useful proof boundary. Reject/merge the decomposition instead of escalating architecture.

A human decision is appropriate only for case (1), or when case (2) genuinely requires the human to
approve a changed root/system contract. The decision question must name the unresolved semantic choice,
not merely restate an ambiguous word from the target claim.

Open decisions live under:

    refinement:
      decisions:
        D_example:
          status: open
          question: >-
            Exact semantic choice the human must make.
          reason: >-
            Why existing specification does not determine the answer.
          options:
            - ...
            - ...

Attach the decision only to directly blocked refinement nodes:

    refinement:
      nodes:
        SOME_NODE:
          status: pending
          blocked_by: [D_example]

`task` and `priority` are derived from the claim kind by `correctness.py`; they are scheduler output,
not editable YAML fields.

Do not transitively copy the blocker to ancestors; the scheduler derives waiting impact.
`refinement-status` reports each decision's affected pending nodes and roots.

## 6.1 Global tooling blockers

A tooling blocker is NOT a system-design decision. It means the correctness tool/control
plane itself prevents faithful refinement. Examples include an invalid mutation-schema
restriction, a scheduler rule that forces the wrong proof order, an ID/naming rule that
cannot represent the required graph, or a generated audit contract that suppresses a
necessary class of result.

If a clean sub-agent identifies such a problem, it must use the `TOOLING_BLOCKED` verdict,
set `tooling_blocker.required: true` in its final verdict envelope, and describe the exact limitation. The orchestrator
must stop the ENTIRE campaign immediately; unlike a `D<n>` human semantic decision, a
`T<n>` tooling blocker is not branch-local.

When possible, record it through the mutation API:

    - op: add_tooling_blocker
      slug: mutation_schema_cannot_express_required_edge
      body:
        issue: >-
          Exact tooling limitation.
        reason: >-
          Why working around it would distort correctness reasoning.
        suggested_change: >-
          Minimal tool repair, if known.

This creates `T<n>_...` under `refinement.tooling_blockers` and forces
`refinement-status` to return `TOOLING_BLOCKED` even if other graph branches would otherwise
be runnable. While that state is active, `mutate` rejects all `authority: automation`
transactions. After a human edits/repairs the tool, resolve the blocker explicitly with a
human-authority `resolve_tooling_blocker` mutation before resuming automation.

## 7. Refinement stability and semantic signatures

`stable` is a refinement-maturity state only. It means the current proposition/decomposition no longer
has known refinement work for the exact recursive proof semantics that were audited. It is not an
implementation-verification result and not an independent proof-composition certificate. After a
successful refinement audit, record the current signature:

    python3 correctness.py refinement-signature NODE

Then set:

    refinement.nodes.NODE.status: stable
    refinement.nodes.NODE.signature: <printed sha256>

The signature tracks semantic proof content and recursive claim dependencies. Evidence execution results belong in a separate evidence layer; `verification` here describes planned verifier intent only.
If a descendant proposition or dependency changes, ancestors with old signatures become
`stale` automatically and re-enter the runnable frontier when their dependencies settle.

`waived` is exceptional: use it only for a deliberately excluded refinement obligation,
with a human-readable reason in workflow metadata. It is not evidence that the claim is
true.

## 7.1 Orthogonal assurance state

The canonical model tracks three assurance dimensions separately from refinement:

- `assurance.specification_coverage` — architecture-level completeness of the root guarantee universe.
  Declared states are `unaudited`, `gap_found`, and `closed`. A non-unaudited result is bound to a
  model signature that includes the root closures, assumptions, global scope/failure semantics, and
  canonical design-article content. If those semantics change, the effective state becomes `stale`.
- `assurance.composition.<ROOT_OR_DERIVED>` — confidence that the node's declared direct dependencies
  imply the node proposition. Declared states are `unaudited`, `single_agent_audited`,
  `multi_agent_audited`, and `machine_checked`. Audited states bind a composition-specific signature
  over the target proposition plus its direct dependency propositions; deeper child decompositions do
  not invalidate an unchanged parent implication. Signature mismatch is reported as effective `stale`.
- `assurance.implementation_evidence.<LEAF>` — executable evidence lifecycle for a leaf. Declared
  states are `planned`, `implemented`, `passing`, and `failing`. `implemented`/`passing`/`failing` bind
  the leaf semantic signature, an implementation revision, and concrete evidence artifact references.
  Semantic changes make the effective evidence state `stale`; `passing` always means passing only for
  the recorded implementation revision.

`stale` is deliberately derived rather than persisted. Old assurance provenance remains in YAML, while
the tool refuses to present it as current after the relevant semantic signature changes. Node-role
changes reset assurance because composition assurance and leaf implementation evidence are different
proof layers. Ordinary proposition changes do not erase assurance records; they make them stale.

Read the current assurance envelope with:

    python3 correctness.py assurance-status --format compact-yaml

The refinement scheduler ignores these assurance levels. A graph can correctly be `COMPLETE` while
specification coverage is `unaudited`, composition assurance is `unaudited`, and leaf evidence is only
`planned`. This separation is intentional.

## 8. Applying graph refinements

After an accepted automated graph patch:

1. update `claims` / dependencies minimally;
2. add `refinement.nodes` entries for every new claim (`pending`, empty `blocked_by` unless genuinely blocked); audit task/priority are derived from claim kind;
3. do not preserve obsolete stable signatures for semantically changed nodes;
4. run `validate`;
5. run `refinement-status --format compact-yaml` again;
6. continue all newly runnable branches.

Do not run `test_correctness.py` merely because the production DAG changed. That suite is a
tooling regression suite and must use isolated synthetic fixtures rather than mutable campaign
state. Run it when correctness tooling itself changes or after a tooling repair.

Prefer minimal semantic nodes. Do not create one leaf per test case (e.g. one for crash,
one for reconnect) when those executions are witnesses of the same invariant.

## 9. Stopping decomposition

A leaf is a boundary between architectural reasoning and localized mechanical proof. Stop splitting
as soon as the complete proposition can be established by a verifier whose inputs and observations are
local to that implementation surface, without reconstructing the parent protocol.

The strongest stopping test is **single-verifier sufficiency**:

- if one deterministic/local checker can decide the whole proposition, default to one leaf;
- the checker may contain several internal assertions—those assertions do not automatically deserve
  separate DAG nodes;
- if implementation-level analysis could directly establish the property, preserving that proposition
  as the leaf is desirable: the DAG should state *what must be true*, while the evidence layer proves
  it from code/config/runtime artifacts.

Canonical tool-level stopping signals are defined once in `LEAF_STOPPING_SIGNALS` and rendered here:

__LEAF_STOPPING_SIGNALS__

Do not recursively decompose merely because a sentence can be grammatically split or because its
verifier would evaluate multiple assertions. Independent falsifiability is necessary but not sufficient
for decomposition. Split only when doing so creates materially useful proof reuse—for example distinct
implementation ownership, verifier mechanisms, change-invalidation surfaces, or separately reusable
correctness contracts.

As a reverse check, ask: if the proposed children are normally invalidated by the same code change,
checked by the same localized verifier, and maintained by the same owner, what durable value does
separate DAG identity provide? If the answer is only "they are logically separate statements", keep
or merge them as one leaf.

## 10. Required safety checks

After an ordinary DAG/workflow-state mutation that does not change correctness tooling, run:

    python3 correctness.py validate
    python3 correctness.py refinement-status --format compact-yaml

When correctness tooling itself changes, or after a tooling repair, run the stronger gate:

    python3 correctness.py validate
    python3 -m unittest -v test_correctness.py
    python3 correctness.py refinement-status --format compact-yaml

Never mark planned verifier descriptions as passed evidence. The refinement campaign
improves the proof DAG; it does not by itself verify production code.

## 11. Useful commands

    python3 correctness.py --help
    python3 correctness.py workflow-help
    python3 correctness.py catalog mechanisms
    python3 correctness.py catalog semantic_contracts
    python3 correctness.py catalog terms client_change_id
    python3 correctness.py graph-schema --format yaml
    python3 correctness.py schema-fields --format text
    python3 correctness.py mutation-schema --format yaml
    python3 correctness.py validate
    python3 correctness.py refinement-status --format compact-yaml
    python3 correctness.py refinement-status --format yaml   # full diagnostic view
    python3 correctness.py assurance-status --format compact-yaml
    python3 correctness.py assurance-status --format yaml    # full per-node assurance view
    python3 correctness.py audit-prompt NODE
    python3 correctness.py mutate --plan PLAN.yaml
    python3 correctness.py slice NODE --format prompt
    python3 correctness.py refinement-signature NODE
    python3 correctness.py invalidate NODE
    python3 correctness.py render NODE

The scheduler, not an agent's intuition, decides whether the campaign should continue,
complete, escalate to a human, or report a workflow stall.
"""
WORKFLOW_HELP = WORKFLOW_HELP.replace("__LEAF_STOPPING_SIGNALS__", _render_leaf_stopping_signals())
REFINEMENT_STATUSES = set(SCHEMA_REFINEMENT_STATUSES)
SPECIFICATION_COVERAGE_STATUSES = set(SCHEMA_SPECIFICATION_COVERAGE_STATUSES)
COMPOSITION_ASSURANCE_STATUSES = set(SCHEMA_COMPOSITION_ASSURANCE_STATUSES)
EVIDENCE_ASSURANCE_STATUSES = set(SCHEMA_EVIDENCE_ASSURANCE_STATUSES)
DECISION_STATUSES = set(SCHEMA_DECISION_STATUSES)
TOOLING_BLOCKER_STATUSES = set(SCHEMA_TOOLING_BLOCKER_STATUSES)


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate YAML mapping keys."""


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


@dataclass(frozen=True)
class Node:
    node_id: str
    node_type: str  # claim | assumption
    data: dict[str, Any]

    @property
    def dependencies(self) -> list[str]:
        value = self.data.get("depends_on", [])
        return list(value) if isinstance(value, list) else []


class Graph:
    def __init__(self, doc: dict[str, Any], *, repository_root: Path | None = None):
        self.doc = doc
        self.repository_root = repository_root
        assumptions = doc.get("assumptions", {})
        claims = doc.get("claims", {})
        roots = doc.get("roots", [])
        self.assumptions: dict[str, dict[str, Any]] = assumptions if isinstance(assumptions, dict) else {}
        self.claims: dict[str, dict[str, Any]] = claims if isinstance(claims, dict) else {}
        self.roots: list[str] = roots if isinstance(roots, list) else []
        self.nodes: dict[str, Node] = {}
        for node_id, data in self.assumptions.items():
            self.nodes[node_id] = Node(node_id, "assumption", data)
        for node_id, data in self.claims.items():
            self.nodes[node_id] = Node(node_id, "claim", data)

    @classmethod
    def load(cls, path: Path = GRAPH_PATH) -> "Graph":
        try:
            with path.open("r", encoding="utf-8") as f:
                doc = yaml.load(f, Loader=UniqueKeyLoader)
        except FileNotFoundError as exc:
            raise SystemExit(f"Graph file not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise SystemExit(f"Invalid YAML in {path}:\n{exc}") from exc
        if not isinstance(doc, dict):
            raise SystemExit(f"Top-level YAML document must be a mapping: {path}")
        return cls(doc, repository_root=_repository_root_for_graph_path(path))

    def require_node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise SystemExit(f"Unknown node: {node_id}") from exc

    def reverse_dependencies(self) -> dict[str, list[str]]:
        rev: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for node in self.nodes.values():
            for dep in node.dependencies:
                if dep in rev:
                    rev[dep].append(node.node_id)
        for values in rev.values():
            values.sort()
        return rev

    def dependency_closure(self, target: str, stop_at: set[str] | None = None) -> set[str]:
        self.require_node(target)
        stop_at = stop_at or set()
        closure: set[str] = set()
        stack = [target]
        while stack:
            node_id = stack.pop()
            if node_id in closure:
                continue
            closure.add(node_id)
            if node_id in stop_at and node_id != target:
                continue
            node = self.require_node(node_id)
            stack.extend(reversed(node.dependencies))
        return closure

    def dependency_depths(self, target: str, stop_at: set[str] | None = None) -> dict[str, int]:
        self.require_node(target)
        stop_at = stop_at or set()
        depths = {target: 0}
        queue = collections.deque([target])
        while queue:
            node_id = queue.popleft()
            if node_id in stop_at and node_id != target:
                continue
            for dep in self.require_node(node_id).dependencies:
                if dep not in self.nodes:
                    continue
                candidate = depths[node_id] + 1
                if dep not in depths or candidate < depths[dep]:
                    depths[dep] = candidate
                    queue.append(dep)
        return depths

    def affected_by(self, changed: Iterable[str]) -> tuple[dict[str, int], dict[str, str | None]]:
        changed = list(changed)
        for node_id in changed:
            self.require_node(node_id)
        rev = self.reverse_dependencies()
        distance: dict[str, int] = {}
        via: dict[str, str | None] = {}
        queue: collections.deque[str] = collections.deque()
        for node_id in changed:
            distance[node_id] = 0
            via[node_id] = None
            queue.append(node_id)
        while queue:
            node_id = queue.popleft()
            for dependent in rev[node_id]:
                candidate = distance[node_id] + 1
                if dependent not in distance or candidate < distance[dependent]:
                    distance[dependent] = candidate
                    via[dependent] = node_id
                    queue.append(dependent)
        return distance, via


def _as_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def _as_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _catalog(graph: Graph) -> dict[str, Any]:
    value = graph.doc.get("catalog", {})
    return value if isinstance(value, dict) else {}


def _catalog_namespace(graph: Graph, namespace: str) -> dict[str, Any]:
    value = _catalog(graph).get(namespace, {})
    return value if isinstance(value, dict) else {}


def _catalog_entry_payload(entry: Any) -> Any:
    if isinstance(entry, dict):
        return _plain_data(entry)
    return entry


def _semantic_catalog_refs_for_node(graph: Graph, node: Node) -> dict[str, list[str]]:
    """Resolve semantic catalog entries actually used by one proposition.

    Explicit mechanism refs are authoritative. Term/state refs are inferred only from exact
    catalog identifiers appearing in the proposition/formal intent, which keeps underscore-style
    protocol vocabulary both defined and locally injectable without NLP guessing.
    """
    texts = [str(node.data.get("statement", "")), str(node.data.get("formal_intent", ""))]
    combined = "\n".join(texts)
    refs: dict[str, list[str]] = {ns: [] for ns in SEMANTIC_CATALOG_NAMESPACES}
    for namespace in ("terms", "state"):
        for key in _catalog_namespace(graph, namespace):
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", combined, re.IGNORECASE):
                refs[namespace].append(key)
    mechanisms = node.data.get("mechanisms", [])
    if isinstance(mechanisms, list):
        refs["mechanisms"] = [x for x in mechanisms if isinstance(x, str)]
    semantic_contracts = node.data.get("semantic_contracts", [])
    if isinstance(semantic_contracts, list):
        refs["semantic_contracts"] = [x for x in semantic_contracts if isinstance(x, str)]
    for namespace in refs:
        refs[namespace] = sorted(set(refs[namespace]))
    return refs


def _slice_catalog_context(graph: Graph, node_ids: Iterable[str]) -> dict[str, Any]:
    refs: dict[str, set[str]] = {ns: set() for ns in SEMANTIC_CATALOG_NAMESPACES}
    for node_id in node_ids:
        if node_id not in graph.nodes:
            continue
        local = _semantic_catalog_refs_for_node(graph, graph.nodes[node_id])
        for namespace, keys in local.items():
            refs[namespace].update(keys)
    result: dict[str, Any] = {}
    for namespace in SEMANTIC_CATALOG_NAMESPACES:
        entries = _catalog_namespace(graph, namespace)
        selected = {key: entries[key] for key in sorted(refs[namespace]) if key in entries}
        if selected:
            result[namespace] = selected
    # The failure model is a global proof boundary, so every local audit receives it in full.
    result["failure_events"] = _catalog_namespace(graph, "failure_events")
    return result


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
    for field in ("source_refs", "severity", "assurance_required", "surfaces"):
        semantic_data.pop(field, None)
    catalog_refs = _semantic_catalog_refs_for_node(graph, node)
    semantic_catalog: dict[str, Any] = {}
    for namespace, keys in catalog_refs.items():
        # semantic_contracts was added in schema 0.9. Keep old signatures byte-for-byte stable
        # for nodes that do not reference a contract; only referenced contracts extend semantics.
        if namespace == "semantic_contracts" and not keys:
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


def _node_proposition_signature(graph: Graph, node_id: str) -> str:
    """Hash one proposition and its interpretation context, excluding proof dependencies."""
    node = graph.require_node(node_id)
    semantic_data = dict(node.data)
    for field in ("source_refs", "severity", "assurance_required", "surfaces", "depends_on"):
        semantic_data.pop(field, None)
    catalog_refs = _semantic_catalog_refs_for_node(graph, node)
    semantic_catalog: dict[str, Any] = {}
    for namespace, keys in catalog_refs.items():
        if namespace == "semantic_contracts" and not keys:
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
                if entry.get("class") not in {"safety_contract", "equivalence_relation"}:
                    errors.append(
                        f"catalog.semantic_contracts.{key}.class must be safety_contract or equivalence_relation"
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
            if namespace == "state" and entry.get("class") not in {"authoritative", "derived"}:
                errors.append(f"catalog.state.{key}.class must be authoritative or derived")
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

        surfaces = node.data.get("surfaces", [])
        if node.node_type == "claim" and node.data.get("kind") == "leaf" and not surfaces:
            errors.append(f"{node_id}: leaf claim must declare at least one surface")
        if surfaces is not None:
            if not _as_string_list(surfaces):
                errors.append(f"{node_id}: surfaces must be a list of catalog surface IDs")
            else:
                known_surfaces = _catalog_namespace(graph, "surfaces")
                for surface in surfaces:
                    if surface not in known_surfaces:
                        errors.append(f"{node_id}: unknown surface {surface}")

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
        print("These entries define the meaning/observation boundary of referencing propositions; they are not proof premises.")
        print()
        for key, entry in semantic_contracts.items():
            if not isinstance(entry, dict):
                continue
            print(f"- **{key}** [{entry.get('class', '?')}]: {str(entry.get('definition', '')).strip()}")
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
    # proof boundary (stable/waived). Treat those direct propositions as opaque contracts instead
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
        print("Verdicts: `GOOD_LEAF`, `SHOULD_DECOMPOSE`, `HUMAN_SEMANTIC_DECISION`, `TOOLING_BLOCKED`,")
        print("or `INCOMPLETE_CONTEXT`. For decomposition, explain the smallest useful children in prose.")
    else:
        print("## Question")
        print()
        print("Try to refute the target, then decide whether its direct dependency propositions are sufficient")
        print("and no stronger than necessary. Classify each direct dependency as `necessary`,")
        print("`useful_but_stronger`, `redundant`, or `unrelated`.")
        print("Verdicts: `SUPPORTED`, `REFUTED`, `INCOMPLETE`, `HUMAN_SEMANTIC_DECISION`, or `TOOLING_BLOCKED`.")

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
    print("Use empty lists when applicable and set both `required` flags explicitly.")


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
    if args.format == "yaml":
        print(yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    if args.format == "compact-yaml":
        compact = {
            "model_signature": snapshot["model_signature"],
            "specification_coverage": snapshot["specification_coverage"],
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




def _plain_data(value: Any) -> Any:
    """Convert round-trip YAML containers into plain Python containers."""
    if isinstance(value, dict):
        return {str(k): _plain_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_data(v) for v in value]
    return value


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


def _audit_task_priority(kind: Any) -> tuple[str, int]:
    if kind == "leaf":
        return "leaf_boundary_audit", 100
    if kind == "derived":
        return "proof_audit", 200
    if kind == "root":
        return "proof_audit", 300
    raise SystemExit(f"Unsupported claim kind: {kind}")


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
            entry = refinement_nodes[node_id]
            for field in ("status", "blocked_by", "rationale"):
                if field in raw_op:
                    entry[field] = _resolve_ref(raw_op[field], aliases)
            if entry.get("status") == "stable":
                stable_later.append(node_id)
                entry.pop("signature", None)
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
            "Agents: run `python3 correctness.py workflow-help` before participating in the "
            "automated refinement campaign. Orchestrators should drive work from "
            "`refinement-status --format compact-yaml`, should load `mutation-schema` once per tooling revision before compiling "
            "mutation plans, must apply graph changes only through `mutate`, and clean sub-agents "
            "should receive only a runnable node ID and obtain their full contract via `audit-prompt NODE`."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
    sub.add_parser("validate", help="Validate schema, DAG, refinement-control, and assurance-state invariants.")

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
    if args.command == "schema-fields":
        return _cmd_schema_fields(None, args)

    graph = Graph.load()
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
