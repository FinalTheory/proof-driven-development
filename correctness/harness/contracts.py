from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

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
    REFINEMENT_STATUSES as SCHEMA_REFINEMENT_STATUSES,
    SPECIFICATION_COVERAGE_STATUSES as SCHEMA_SPECIFICATION_COVERAGE_STATUSES,
    TOOLING_BLOCKER_STATUSES as SCHEMA_TOOLING_BLOCKER_STATUSES,
)

SCHEMA_REFERENCE_PATH = Path(__file__).resolve().parent.parent / "SCHEMA.md"
SCHEMA_INDEX_START = "<!-- canonical-schema-index:start -->"
SCHEMA_INDEX_END = "<!-- canonical-schema-index:end -->"
SCHEMA_INDEX_LINE_RE = re.compile(r"^- \`([^\`]+)\` => (.+)$")

NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
CLAIM_KINDS = {"root", "derived", "leaf"}
CATALOG_NAMESPACES = (
    "terms",
    "state",
    "symbolic_dimensions",
    "mechanisms",
    "semantic_contracts",
    "failure_events",
    "verifier_kinds",
    "sources",
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
    "transaction_atomicity": "One localized database transaction boundary can directly establish all-or-nothing coupling of the authoritative writes or success evidence named by the proposition; this signal does not by itself establish uniqueness, predicate correctness, or concurrency serialization.",
    "control_flow": "A localized control-flow dominance, reachability, or ordering analysis can decide the complete proposition.",
    "static_dataflow": "A localized static dataflow, provenance, or taint analysis can decide the complete proposition.",
    "bounded_state_machine": "A bounded state-machine/model checker can decide the complete proposition without reconstructing the parent protocol.",
    "symbolic_execution": "A localized symbolic execution or equivalent path proof can decide the complete proposition.",
    "differential_property": "A localized differential/property checker can decide the complete proposition over its implementation surface.",
}


def _render_leaf_stopping_signals(*, indent: str = "") -> str:
    return "\n".join(f"{indent}- `{key}`: {description}" for key, description in LEAF_STOPPING_SIGNALS.items())


HARNESS_FEEDBACK_CONTRACT = r"""## NON-NORMATIVE HARNESS FEEDBACK

Only after fixing the canonical audit verdict/result, report any meaningful issues encountered while
executing the Harness contract. This feedback is diagnostic only: it must not alter, reinterpret,
repair, or serve as a premise for the canonical verdict.

Useful feedback includes semantic/terminology ambiguity, missing context that forced inference, redundant
context, unclear auditor/orchestrator responsibility, conflicting or hard-to-operationalize instructions,
insufficient output fields, or opportunities to make the Harness smaller and more deterministic.

Classify each issue as exactly one of:
- `correctness_model_defect` — the canonical model/context itself is ambiguous or underspecified;
- `harness_prompt_defect` — generated audit instructions or isolation boundaries are unclear/inadequate;
- `documentation_ergonomics` — workflow is semantically clear but unnecessarily awkward or noisy.

Do not invent feedback to appear helpful. If there are no meaningful issues, output exactly:

`HARNESS FEEDBACK: none`

Otherwise use:
```yaml
HARNESS_FEEDBACK:
  - category: <correctness_model_defect|harness_prompt_defect|documentation_ergonomics>
    issue: <specific problem encountered>
    impact: <how it affected reasoning or execution>
    suggested_change: <minimal improvement>
```
"""


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
                "contract_realizations": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/ref"},
                    "uniqueItems": True,
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "stable"}}, "required": ["status"]},
                    "then": {"required": ["contract_realizations"]},
                },
                {
                    "if": {
                        "properties": {"status": {"enum": ["pending", "waived"]}},
                        "required": ["status"],
                    },
                    "then": {"not": {"required": ["contract_realizations"]}},
                },
                {
                    "if": {"required": ["contract_realizations"]},
                    "then": {
                        "required": ["status"],
                        "properties": {"status": {"const": "stable"}},
                    },
                },
            ],
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

When the Writer MCP exposes `spawn_chatgpt_subagents`, that is the canonical clean-context
execution primitive and the orchestrator MUST use it rather than simulating multiple roles
inside its own context. If that function is not present in the initially loaded tool subset,
perform Writer-tool discovery before concluding that clean-context execution is unavailable.
Browser-slot exhaustion or a child-task failure is an execution/capacity
condition, not evidence that clean-context tooling does not exist: reap/finish existing tasks and
retry when capacity is available. If a clean sub-agent cannot actually be obtained, do not silently
downgrade the audit to same-context reasoning and do not claim a clean audit result.

The clean verifier may use general distributed-systems knowledge to understand terms and
construct executions, but it must not invent guarantees absent from its generated task.

For a runnable node NODE, the clean sub-agent should run:

    .venv/bin/python3 correctness.py audit-prompt NODE

and treat that output as its complete audit specification. The clean verifier must not read the
full correctness.yaml, README, source article, prior audits, mutation history, or sibling-agent
results at any later phase. Full-graph reuse and patch placement belong exclusively to the
long-lived orchestrator.

## 3. Orchestrator loop

Always start a round with:

    .venv/bin/python3 correctness.py validate
    .venv/bin/python3 correctness.py refinement-status --format compact-yaml

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

Canonical model text is current-state specification, not a changelog. When compiling claims, contracts,
verification intents, or temporary pending/waiver rationale, state what is true now. Do not encode edit
history such as "previously X, now Y", "after the repair", "reclassified from", or why an older wording was
wrong unless that history is itself part of the current system semantics. Stable refinement nodes MUST NOT
retain rationale at all; their current proposition plus semantic signature is the canonical state. Waived
nodes still require rationale because the exception itself is current workflow state. Git/history and
external audit artifacts carry change narrative; `correctness.yaml` should remain a clean statement of the
present model.

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
- `CONTRACT_MISMATCH`: the target references a semantic contract but does not fully realize that
  contract's guarantee across its declared class/scope/binding semantics. The claim may be stronger
  than the reusable contract, but it may not be weaker. Repair the proposition boundary before stable.
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
semantic contracts, source sections, and verifier kinds, while underscore-style
protocol terms must resolve to a catalog term/state/mechanism/semantic-contract definition. A semantic
contract is a reusable property specification / refinement type: it defines the property's truth condition,
typed relation, exclusions, and where applicable its minimum semantic scope. It is not a theorem, does not
assert that the system satisfies the property, and is never a proof premise by itself. In addition to generic
`safety_contract` and `equivalence_relation` boundaries, the type system has two relation-specific forms:

- `state_invariant`: an inductive predicate over typed `state_symbols` and an explicit `observation_scope`.
  A referencing proof must establish the predicate at the relevant entry/publication boundary and preserve
  it across every in-scope transition capable of mutating any participating symbol. Listing several named
  transitions is not enough unless all other mutators are explicitly outside the contract.
`catalog.state` distinguishes `authoritative`, `derived`, `speculative`, and `control` state. In particular,
optimistic/uncommitted user intent must not be mislabeled as derived canonical state, and protocol modes or
pending/reconciliation bookkeeping may be represented as control state when they participate in correctness
relations.

- `provenance_binding`: a typed origin/binding relation from `source_symbols` to `bound_symbols`. A
  referencing proof must establish producer/source/capture identity and preserve that binding through
  downstream use. Correct processing of a value does not establish that the value was sourced from or
  bound to the correct execution/state.

Automation may reuse only mechanisms whose `automation_reusable` flag is true, and may reference an
approved reusable semantic contract when creating a new proof-structure claim. Changing or removing the
semantic-contract association of an existing claim requires human authority, because that changes the
proposition's interpretation boundary. A semantic-contract association is never hidden metadata: every
referenced contract ID must appear exactly in the claim's `statement` or `formal_intent` and in at least one
of that claim's `source_refs` article sections.

Every referenced semantic contract also creates a mandatory **contract-realization obligation** during
refinement. Association means that this claim itself asserts a full realization of that property specification;
a claim must not inherit or copy a semantic contract merely because one of its proof dependencies already
realizes it. The clean auditor must explicitly judge whether the target proposition entails the contract's
complete truth condition across its full class, minimum observation scope, provenance/identity coordinates,
and exclusions. A target may be stronger than the reusable contract, but any `WEAKER`, `MISMATCH`, or `INCOMPLETE` result
prevents stable certification. When compiling `set_refinement(status=stable)`, the orchestrator MUST pass
`contract_realizations` containing exactly the currently referenced contract IDs that the audit judged
`REALIZES`; use an explicit empty list for a claim with no semantic contracts. The mutation tool rejects a
stable transition if this declaration is missing or does not exactly match the claim's current contract set.
Contract-bearing claims participate in a dedicated realization-semantics signature version, so changing this
obligation reopens only affected proof branches rather than unrelated claims.

`catalog.sources` stable semantic IDs resolve through exact H2 `heading` text; numeric Markdown section prefixes are presentation-only and ignored. Automation may not
create catalog entries or introduce an unapproved mechanism/contract merely to close a proof hole; report a
human semantic decision instead.

Verifier `kind` values come from the small canonical `catalog.verifier_kinds` taxonomy. Scenario-specific
details belong in verifier `intent`, not in newly invented verifier kinds.

## 5.2 Closed graph schema, single-writer mutation, and node IDs

`correctness.yaml` is a closed typed document, not an extensible metadata bag. The canonical graph
JSON Schema rejects unknown fields and constrains enums/types before graph-semantic validation runs:

    .venv/bin/python3 correctness.py graph-schema --format yaml
    .venv/bin/python3 correctness.py graph-schema --format json

Persisted symbolic sidecars/artifacts have a separate closed schema surface:

    .venv/bin/python3 correctness.py formal-schema bridge --format yaml
    .venv/bin/python3 correctness.py formal-schema assurance --format yaml
    .venv/bin/python3 correctness.py formal-schema coverage --format yaml
    .venv/bin/python3 correctness.py formal-schema composition-artifact --format yaml
    .venv/bin/python3 correctness.py formal-schema coverage-artifact --format yaml

For a deduplicated human-readable inventory of every canonical YAML field path, including presence rules, structural constraints, and semantic descriptions:

    .venv/bin/python3 correctness.py schema-fields --format text

Only system/proof semantics and mutable campaign state belong in the YAML. Tool-intrinsic rules such as bottom-up scheduling, stop-state names, node-prefix meanings, dependency direction, audit interpretation rules, and the signature algorithm are defined by this program and are intentionally not duplicated as editable YAML policy. `id_allocator.next_sequence` remains YAML state because the writer must persist allocation progress.

Agents MUST NOT edit `correctness.yaml` directly. The canonical mutation-plan contract is also
machine-readable JSON Schema and is available with:

    .venv/bin/python3 correctness.py mutation-schema --format yaml
    .venv/bin/python3 correctness.py mutation-schema --format json

A long-lived orchestrator should load these schemas once per tooling revision/session and cache them.
Do not re-read the full schema before every graph mutation; reload it only after correctness tooling
changes or when a schema mismatch indicates that the cached contract is stale.

Every structured graph/refinement mutation must then enter through:

    .venv/bin/python3 correctness.py mutate --plan PLAN.yaml

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
          source_refs: [accepted_change_log]
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

    .venv/bin/python3 correctness.py refinement-signature NODE

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

    .venv/bin/python3 correctness.py assurance-status --format compact-yaml

The refinement scheduler ignores these assurance levels. A graph can correctly be `COMPLETE` while
specification coverage is `unaudited`, composition assurance is `unaudited`, and leaf evidence is only
`planned`. This separation is intentional.

## 7.2 Assurance certification campaigns

Once refinement is `COMPLETE`, drive architecture-level specification coverage and node-level proof
composition as separate frozen-DAG campaigns:

    .venv/bin/python3 correctness.py coverage-audit-prompt
    .venv/bin/python3 correctness.py composition-status --format compact-yaml
    .venv/bin/python3 correctness.py composition-audit-prompt NODE --focus execution
    .venv/bin/python3 correctness.py composition-audit-prompt NODE --focus quantifier
    .venv/bin/python3 correctness.py composition-audit-prompt NODE --focus premise

`coverage-audit-prompt` generates the canonical global root-coverage campaign and binds it to the current
model signature. It asks whether an in-scope material failure can still occur assuming all claims,
assumptions, and dependency implications are true, and it includes a semantic-alignment check against the
canonical design. Record an accepted orchestrator result through `set_specification_coverage`; do not edit
assurance YAML directly.

`composition-status` is a certification scheduler, not the refinement scheduler. It runs only over a
frozen (`COMPLETE`) refinement graph and targets at least `multi_agent_audited` (or `machine_checked`) for
each root/derived node. `single_agent_audited` remains eligible for further certification. The status output
recommends independent auditor counts and adversarial focus roles from the node role: roots receive
three independent auditors and derived claims receive two.

`composition-audit-prompt NODE` exposes exactly the target proposition and its direct dependency
propositions as opaque premises. It never recursively opens grandchildren and never asks whether those
premises are themselves true. Independent auditors should use different `--focus` roles; the orchestrator
accepts `multi_agent_audited` only after reconciling their results and challenging any surviving
`deps=true, target=false` counterexample. Record the result through `set_composition_assurance`.

These certification prompts are analysis-only. They do not mutate the graph, and assurance metadata must
never be used as a proof premise.

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
implementation ownership, verifier mechanisms, or separately reusable correctness contracts.

As a reverse check, ask: if the proposed children are normally invalidated by the same code change,
checked by the same localized verifier, and maintained by the same owner, what durable value does
separate DAG identity provide? If the answer is only "they are logically separate statements", keep
or merge them as one leaf.

## 10. Required safety checks

After an ordinary DAG/workflow-state mutation that does not change correctness tooling, run:

    .venv/bin/python3 correctness.py validate
    .venv/bin/python3 correctness.py refinement-status --format compact-yaml

When correctness tooling itself changes, or after a tooling repair, run the stronger gate:

    .venv/bin/python3 correctness.py validate
    python3 -m unittest -v test_correctness.py
    .venv/bin/python3 correctness.py refinement-status --format compact-yaml

Never mark planned verifier descriptions as passed evidence. The refinement campaign
improves the proof DAG; it does not by itself verify production code.

## 10.1 Non-normative Harness dogfood feedback

Every human-invoked audit/certification campaign should keep its canonical result separate from
Harness feedback. Use the same contract that generated audit prompts append:

__HARNESS_FEEDBACK_CONTRACT__

## 11. Useful commands

    .venv/bin/python3 correctness.py --help
    .venv/bin/python3 correctness.py workflow-help
    .venv/bin/python3 correctness.py catalog mechanisms
    .venv/bin/python3 correctness.py catalog semantic_contracts
    .venv/bin/python3 correctness.py catalog terms client_change_id
    .venv/bin/python3 correctness.py graph-schema --format yaml
    .venv/bin/python3 correctness.py schema-fields --format text
    .venv/bin/python3 correctness.py mutation-schema --format yaml
    .venv/bin/python3 correctness.py validate
    .venv/bin/python3 correctness.py refinement-status --format compact-yaml
    .venv/bin/python3 correctness.py refinement-status --format yaml   # full diagnostic view
    .venv/bin/python3 correctness.py assurance-status --format compact-yaml
    .venv/bin/python3 correctness.py assurance-status --format yaml    # full per-node assurance view
    .venv/bin/python3 correctness.py audit-prompt NODE
    .venv/bin/python3 correctness.py coverage-audit-prompt
    .venv/bin/python3 correctness.py composition-status --format compact-yaml
    .venv/bin/python3 correctness.py composition-audit-prompt NODE --focus execution
    .venv/bin/python3 correctness.py mutate --plan PLAN.yaml
    .venv/bin/python3 correctness.py slice NODE --format prompt
    .venv/bin/python3 correctness.py refinement-signature NODE
    .venv/bin/python3 correctness.py invalidate NODE
    .venv/bin/python3 correctness.py render NODE

The scheduler, not an agent's intuition, decides whether the campaign should continue,
complete, escalate to a human, or report a workflow stall.
"""
WORKFLOW_HELP = WORKFLOW_HELP.replace("__LEAF_STOPPING_SIGNALS__", _render_leaf_stopping_signals())
WORKFLOW_HELP = WORKFLOW_HELP.replace("__HARNESS_FEEDBACK_CONTRACT__", HARNESS_FEEDBACK_CONTRACT.rstrip())
REFINEMENT_STATUSES = set(SCHEMA_REFINEMENT_STATUSES)
SPECIFICATION_COVERAGE_STATUSES = set(SCHEMA_SPECIFICATION_COVERAGE_STATUSES)
COMPOSITION_ASSURANCE_STATUSES = set(SCHEMA_COMPOSITION_ASSURANCE_STATUSES)
EVIDENCE_ASSURANCE_STATUSES = set(SCHEMA_EVIDENCE_ASSURANCE_STATUSES)
DECISION_STATUSES = set(SCHEMA_DECISION_STATUSES)
TOOLING_BLOCKER_STATUSES = set(SCHEMA_TOOLING_BLOCKER_STATUSES)


