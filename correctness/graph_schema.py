from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "0.15"
PROOF_SEMANTICS_VERSION = "1"
ASSUMPTION_STATUSES = (
    "external_assumption",
    "protocol_assumption",
    "environment_assumption",
)
REFINEMENT_STATUSES = ("pending", "stable", "waived")
SPECIFICATION_COVERAGE_STATUSES = ("unaudited", "gap_found", "closed")
COMPOSITION_ASSURANCE_STATUSES = (
    "unaudited",
    "single_agent_audited",
    "multi_agent_audited",
    "machine_checked",
)
EVIDENCE_ASSURANCE_STATUSES = ("planned", "implemented", "passing", "failing")
DECISION_STATUSES = ("open", "resolved")
TOOLING_BLOCKER_STATUSES = ("open", "resolved")

LOWER_SNAKE = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
SOURCE_ID = r"^(?!section_[0-9]+$)[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
ASSUMPTION_ID = r"^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
ROOT_ID = r"^G[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
CLAIM_ID = r"^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
DECISION_ID = r"^D[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
TOOLING_BLOCKER_ID = r"^T[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
HEX64 = r"^[0-9a-f]{64}$"


def _string_list(*, min_items: int = 0, pattern: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "string", "minLength": 1}
    if pattern:
        item["pattern"] = pattern
    result: dict[str, Any] = {
        "type": "array",
        "items": item,
        "uniqueItems": True,
    }
    if min_items:
        result["minItems"] = min_items
    return result


def _closed_object(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] | list[str] = (),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        result["required"] = list(required)
    if extra:
        result.update(extra)
    return result


NONEMPTY_STRING = {"type": "string", "minLength": 1}
SOURCE_REFS = _string_list(min_items=1, pattern=SOURCE_ID)
MECHANISM_REFS = _string_list(min_items=1, pattern=LOWER_SNAKE)
SEMANTIC_CONTRACT_REFS = _string_list(min_items=1, pattern=LOWER_SNAKE)
SEMANTIC_SYMBOL_REFS = _string_list(min_items=1, pattern=LOWER_SNAKE)
DEPENDENCY_REFS = _string_list(min_items=1, pattern=r"^[AGCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

VERIFIER_SCHEMA = _closed_object(
    {
        "kind": {
            "type": "string",
            "pattern": LOWER_SNAKE,
            "description": "Verifier strategy category. Must resolve to catalog.verifier_kinds.",
        },
        "intent": {
            **NONEMPTY_STRING,
            "description": "Scenario-specific observation/check this verifier is expected to perform.",
        },
    },
    required=("kind", "intent"),
)

VERIFICATION_SCHEMA = _closed_object(
    {
        "verifiers": {
            "type": "array",
            "minItems": 1,
            "items": VERIFIER_SCHEMA,
            "description": "One or more planned verifier strategies whose concrete scenario is described by each intent.",
        },
    },
    required=("verifiers",),
)

CLAIM_BODY_PROPERTIES: dict[str, Any] = {
    "statement": {
        **NONEMPTY_STRING,
        "description": "Authoritative natural-language proposition represented by this node.",
    },
    "source_refs": {
        **SOURCE_REFS,
        "description": "Traceability references into catalog.sources; excluded from proposition semantics.",
    },
    "mechanisms": {
        **MECHANISM_REFS,
        "description": "Approved architecture mechanisms referenced by the proposition and used for synthesis-boundary checks.",
    },
    "semantic_contracts": {
        **SEMANTIC_CONTRACT_REFS,
        "description": "Reusable typed property specifications / refinement types fully realized by this claim. A contract defines truth conditions, interpretation boundaries, exclusions, and any minimum semantic scope; it is not itself a theorem or proof premise.",
    },
    "formal_intent": {
        **NONEMPTY_STRING,
        "description": "Optional compact/formal restatement of the proposition; included in semantic signatures.",
    },
    "depends_on": {
        **DEPENDENCY_REFS,
        "description": "Logical proof dependencies. A depends_on B means A is not established unless B holds; this is not runtime order.",
    },
    "verification": {
        **VERIFICATION_SCHEMA,
        "description": "Planned executable/mechanical evidence attached to the claim. Presence means evidence is planned; implementation/execution state is tracked separately under assurance.implementation_evidence.",
    },
}

CLAIM_MUTABLE_FIELDS = tuple(CLAIM_BODY_PROPERTIES)


CLAIM_SCHEMA = _closed_object(
    {"kind": {"enum": ["root", "derived", "leaf"], "description": "Proof-graph role of the claim."}, **CLAIM_BODY_PROPERTIES},
    required=("kind", "statement", "source_refs"),
    extra={
        "allOf": [
            {
                "if": {"properties": {"kind": {"enum": ["root", "derived"]}}, "required": ["kind"]},
                "then": {"required": ["depends_on"]},
            },
            {
                "if": {"properties": {"kind": {"const": "leaf"}}, "required": ["kind"]},
                "then": {
                    "required": ["mechanisms", "verification"],
                    "not": {"required": ["depends_on"]},
                },
            },
        ]
    },
)


ASSUMPTION_BODY_SCHEMA = _closed_object(
    {
        "statement": {
            **NONEMPTY_STRING,
            "description": "Authoritative proposition accepted as a terminal proof boundary rather than proved inside this DAG.",
        },
        "status": {
            "enum": list(ASSUMPTION_STATUSES),
            "description": "Classifies whether the assumption is delegated to an external component, protocol participant, or environment/liveness condition.",
        },
        "note": {
            **NONEMPTY_STRING,
            "description": "Optional clarification of the assumption boundary; included in assumption semantics when present.",
        },
        "source_refs": {
            **SOURCE_REFS,
            "description": "Traceability references into catalog.sources for this assumption.",
        },
    },
    required=("statement", "status", "source_refs"),
)

DECISION_BODY_SCHEMA = _closed_object(
    {
        "question": {
            **NONEMPTY_STRING,
            "description": "Exact system-semantic choice that existing specification cannot determine automatically.",
        },
        "reason": {
            **NONEMPTY_STRING,
            "description": "Why proof progress requires human architecture/product judgment rather than proof-structure refinement.",
        },
        "options": {
            **_string_list(min_items=1),
            "description": "Concrete candidate semantic choices presented for human judgment.",
        },
        "status": {
            "enum": list(DECISION_STATUSES),
            "description": "Whether the semantic decision is still open or has been resolved.",
        },
        "resolution": {
            **NONEMPTY_STRING,
            "description": "Chosen semantic decision and its operative meaning; required when status is resolved.",
        },
    },
    required=("question", "reason", "options", "status"),
    extra={
        "allOf": [
            {
                "if": {"properties": {"status": {"const": "resolved"}}, "required": ["status"]},
                "then": {"required": ["resolution"]},
            },
            {
                "if": {"properties": {"status": {"const": "open"}}, "required": ["status"]},
                "then": {"not": {"required": ["resolution"]}},
            },
        ]
    },
)

TOOLING_BLOCKER_BODY_SCHEMA = _closed_object(
    {
        "status": {
            "enum": list(TOOLING_BLOCKER_STATUSES),
            "description": "Whether this control-plane defect still globally blocks automated refinement.",
        },
        "issue": {
            **NONEMPTY_STRING,
            "description": "Concrete defect or limitation in the correctness tooling/control plane.",
        },
        "reason": {
            **NONEMPTY_STRING,
            "description": "Why continuing despite the tooling defect would make refinement untrustworthy or unrepresentable.",
        },
        "suggested_change": {
            **NONEMPTY_STRING,
            "description": "Optional proposed tooling repair; advisory rather than a system-semantic decision.",
        },
        "resolution": {
            **NONEMPTY_STRING,
            "description": "Description of the completed tooling repair; required when status is resolved.",
        },
    },
    required=("status", "issue", "reason"),
    extra={
        "allOf": [
            {
                "if": {"properties": {"status": {"const": "resolved"}}, "required": ["status"]},
                "then": {"required": ["resolution"]},
            },
            {
                "if": {"properties": {"status": {"const": "open"}}, "required": ["status"]},
                "then": {"not": {"required": ["resolution"]}},
            },
        ]
    },
)

REFINEMENT_ENTRY_SCHEMA = _closed_object(
    {
        "status": {
            "enum": list(REFINEMENT_STATUSES),
            "description": "Current audit state for this claim: pending work, stable for its current semantics, or explicitly waived.",
        },
        "blocked_by": {
            **_string_list(pattern=DECISION_ID),
            "description": "Open human semantic decisions that directly block this pending claim.",
        },
        "rationale": {
            **NONEMPTY_STRING,
            "description": "Human-readable reason for the current stable/waived judgment; audit history metadata.",
        },
        "signature": {
            "type": "string",
            "pattern": HEX64,
            "description": "SHA-256 of this node's current recursive proof semantics and relevant global/catalog semantics.",
        },
    },
    required=("status", "blocked_by"),
    extra={
        "allOf": [
            {
                "if": {"properties": {"status": {"const": "stable"}}, "required": ["status"]},
                "then": {
                    "required": ["signature"],
                    "properties": {"blocked_by": {"maxItems": 0}},
                    "not": {"required": ["rationale"]},
                },
            },
            {
                "if": {"properties": {"status": {"const": "waived"}}, "required": ["status"]},
                "then": {
                    "required": ["rationale"],
                    "properties": {"blocked_by": {"maxItems": 0}},
                    "not": {"required": ["signature"]},
                },
            },
            {
                "if": {"properties": {"status": {"const": "pending"}}, "required": ["status"]},
                "then": {"not": {"required": ["signature"]}},
            },
        ]
    },
)


SPECIFICATION_COVERAGE_SCHEMA = _closed_object(
    {
        "status": {
            "enum": list(SPECIFICATION_COVERAGE_STATUSES),
            "description": "Architecture-level root-coverage audit result for the current model semantics. `stale` is derived by tooling from signature mismatch and is never written directly.",
        },
        "signature": {
            "type": "string",
            "pattern": HEX64,
            "description": "SHA-256 of the audited root universe, global semantics, assumptions, and canonical design article content.",
        },
        "auditor_count": {
            "type": "integer",
            "minimum": 1,
            "description": "Number of independent adversarial auditors whose results were incorporated into this coverage judgment.",
        },
        "rationale": {
            **NONEMPTY_STRING,
            "description": "Concise explanation of the latest root-coverage judgment.",
        },
    },
    required=("status",),
    extra={
        "allOf": [
            {
                "if": {
                    "properties": {"status": {"enum": ["gap_found", "closed"]}},
                    "required": ["status"],
                },
                "then": {"required": ["signature", "auditor_count", "rationale"]},
            },
            {
                "if": {"properties": {"status": {"const": "unaudited"}}, "required": ["status"]},
                "then": {
                    "not": {
                        "anyOf": [
                            {"required": ["signature"]},
                            {"required": ["auditor_count"]},
                            {"required": ["rationale"]},
                        ]
                    }
                },
            },
        ]
    },
)

COMPOSITION_ASSURANCE_ENTRY_SCHEMA = _closed_object(
    {
        "status": {
            "enum": list(COMPOSITION_ASSURANCE_STATUSES),
            "description": "Assurance level for the logical implication from this non-leaf claim's direct dependencies to the claim itself. `stale` is derived from signature mismatch.",
        },
        "signature": {
            "type": "string",
            "pattern": HEX64,
            "description": "Signature of the composition contract: this target proposition plus its direct dependency propositions and interpretation context, excluding deeper dependency decompositions.",
        },
        "auditor_count": {
            "type": "integer",
            "minimum": 1,
            "description": "Number of independent semantic auditors used for agent-audited composition assurance.",
        },
        "artifact_refs": {
            **_string_list(min_items=1),
            "description": "Machine-checkable proof/model artifacts supporting machine_checked composition assurance.",
        },
        "rationale": {
            **NONEMPTY_STRING,
            "description": "Concise explanation of why this dependency composition is believed sound at the recorded level.",
        },
    },
    required=("status",),
    extra={
        "allOf": [
            {
                "if": {"properties": {"status": {"const": "single_agent_audited"}}, "required": ["status"]},
                "then": {
                    "required": ["signature", "auditor_count", "rationale"],
                    "properties": {"auditor_count": {"const": 1}},
                    "not": {"required": ["artifact_refs"]},
                },
            },
            {
                "if": {"properties": {"status": {"const": "multi_agent_audited"}}, "required": ["status"]},
                "then": {
                    "required": ["signature", "auditor_count", "rationale"],
                    "properties": {"auditor_count": {"type": "integer", "minimum": 2}},
                    "not": {"required": ["artifact_refs"]},
                },
            },
            {
                "if": {"properties": {"status": {"const": "machine_checked"}}, "required": ["status"]},
                "then": {
                    "required": ["signature", "artifact_refs", "rationale"],
                    "not": {"required": ["auditor_count"]},
                },
            },
            {
                "if": {"properties": {"status": {"const": "unaudited"}}, "required": ["status"]},
                "then": {
                    "not": {
                        "anyOf": [
                            {"required": ["signature"]},
                            {"required": ["auditor_count"]},
                            {"required": ["artifact_refs"]},
                            {"required": ["rationale"]},
                        ]
                    }
                },
            },
        ]
    },
)

IMPLEMENTATION_EVIDENCE_ENTRY_SCHEMA = _closed_object(
    {
        "status": {
            "enum": list(EVIDENCE_ASSURANCE_STATUSES),
            "description": "Lifecycle state of executable implementation evidence for this leaf. `stale` is derived from semantic-signature mismatch rather than written directly.",
        },
        "signature": {
            "type": "string",
            "pattern": HEX64,
            "description": "Leaf semantic signature against which the executable evidence was implemented or executed.",
        },
        "implementation_revision": {
            **NONEMPTY_STRING,
            "description": "Implementation revision/build identifier to which the recorded executable evidence result applies.",
        },
        "artifact_refs": {
            **_string_list(min_items=1),
            "description": "Paths, CI artifacts, test/model-checker IDs, or equivalent references for the executable evidence.",
        },
        "rationale": {
            **NONEMPTY_STRING,
            "description": "Optional explanation of the evidence state; required for failing evidence.",
        },
    },
    required=("status",),
    extra={
        "allOf": [
            {
                "if": {
                    "properties": {"status": {"enum": ["implemented", "passing", "failing"]}},
                    "required": ["status"],
                },
                "then": {"required": ["signature", "implementation_revision", "artifact_refs"]},
            },
            {
                "if": {"properties": {"status": {"const": "failing"}}, "required": ["status"]},
                "then": {"required": ["rationale"]},
            },
            {
                "if": {"properties": {"status": {"const": "planned"}}, "required": ["status"]},
                "then": {
                    "not": {
                        "anyOf": [
                            {"required": ["signature"]},
                            {"required": ["implementation_revision"]},
                            {"required": ["artifact_refs"]},
                            {"required": ["rationale"]},
                        ]
                    }
                },
            },
        ]
    },
)

ASSURANCE_SCHEMA = _closed_object(
    {
        "specification_coverage": {
            **SPECIFICATION_COVERAGE_SCHEMA,
            "description": "Global specification-completeness assurance for the current root universe and canonical design.",
        },
        "composition": {
            "type": "object",
            "patternProperties": {CLAIM_ID: COMPOSITION_ASSURANCE_ENTRY_SCHEMA},
            "additionalProperties": False,
            "description": "Per-root/per-derived assurance that direct proof dependencies logically imply the claim.",
        },
        "implementation_evidence": {
            "type": "object",
            "patternProperties": {CLAIM_ID: IMPLEMENTATION_EVIDENCE_ENTRY_SCHEMA},
            "additionalProperties": False,
            "description": "Per-leaf executable evidence state. Planned verifier descriptions remain on claims; execution state lives here.",
        },
    },
    required=("specification_coverage", "composition", "implementation_evidence"),
)


def _catalog_map(entry_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "minProperties": 1,
        "patternProperties": {LOWER_SNAKE: entry_schema},
        "additionalProperties": False,
    }


CATALOG_SCHEMA = _closed_object(
    {
        "terms": {
            **_catalog_map(_closed_object(
                {
                    "description": {**NONEMPTY_STRING, "description": "Definition of this protocol/domain term; vocabulary only, not a correctness premise."},
                    "scope_dimensions": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string", "pattern": LOWER_SNAKE},
                        "description": "Semantic namespace/resource dimensions in which this term is meaningful; provenance bindings that preserve the term across a matching scoped boundary must preserve these coordinates.",
                    },
                },
                required=("description",),
            )),
            "description": "Protocol/domain vocabulary registry. Dynamic keys must be lower_snake_case term IDs.",
        },
        "state": {
            **_catalog_map(
                _closed_object(
                    {
                        "class": {
                            "enum": ["authoritative", "derived", "speculative", "control"],
                            "description": "Semantic state class: source-of-truth authoritative state, reconstructable/materialized derived state, uncommitted speculative intent, or protocol/control state that coordinates execution without itself being canonical truth.",
                        },
                        "description": {
                            **NONEMPTY_STRING,
                            "description": "Definition and correctness role of this state item.",
                        },
                        "symbolic_dimensions": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "pattern": LOWER_SNAKE,
                            },
                            "description": (
                                "Ordered semantic coordinates that a symbolic function directly "
                                "reading this state item must expose as distinct arguments. "
                                "For example [document, observation] prevents a time-varying "
                                "per-document state value from being collapsed into Document -> Value."
                            ),
                        },
                        "scope_dimensions": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string", "pattern": LOWER_SNAKE},
                            "description": "Semantic namespace/resource dimensions in which this state item is meaningful; matching scoped provenance relations must preserve these coordinates.",
                        },
                    },
                    required=("class", "description"),
                )
            ),
            "description": "Architecture state registry. Dynamic keys identify authoritative, derived/materialized, speculative, or protocol/control state items.",
        },
        "symbolic_dimensions": {
            **_catalog_map(_closed_object(
                {
                    "description": {
                        **NONEMPTY_STRING,
                        "description": "Definition of one semantic coordinate used to preserve arity/dimensionality in symbolic lowering.",
                    }
                },
                required=("description",),
            )),
            "description": "Stable registry of semantic coordinates that may be referenced by catalog.state.*.symbolic_dimensions.",
        },
        "scope_dimensions": {
            **_catalog_map(_closed_object(
                {
                    "description": {
                        **NONEMPTY_STRING,
                        "description": "Definition of one semantic namespace/resource coordinate whose identity must be preserved across scoped provenance relations.",
                    }
                },
                required=("description",),
            )),
            "description": "Stable registry of semantic namespace/resource coordinates referenced by term/state scope_dimensions and provenance scope_bindings.",
        },
        "mechanisms": {
            **_catalog_map(
                _closed_object(
                    {
                        "description": {
                            **NONEMPTY_STRING,
                            "description": "Definition of an architecture mechanism that claims may reference.",
                        },
                        "automation_reusable": {
                            "type": "boolean",
                            "description": "Whether automated proof refinement may reuse this already-approved architecture mechanism in new/changed claims.",
                        },
                    },
                    required=("description", "automation_reusable"),
                )
            ),
            "description": "Approved architecture-mechanism registry and automation synthesis boundary.",
        },
        "semantic_contracts": {
            **_catalog_map(
                _closed_object(
                    {
                        "class": {
                            "enum": [
                                "safety_contract",
                                "equivalence_relation",
                                "state_invariant",
                                "provenance_binding",
                            ],
                            "description": "Semantic role of this reusable contract definition.",
                        },
                        "definition": {
                            **NONEMPTY_STRING,
                            "description": "Normative meaning of the contract. Referencing claims inherit this interpretation but do not gain any proof premise from it.",
                        },
                        "excludes": {
                            **_string_list(),
                            "description": "Explicitly excluded observations, horizons, or stronger guarantees that must not be inferred from this contract.",
                        },
                        "automation_reusable": {
                            "type": "boolean",
                            "description": "Whether automated refinement may reference this already human-approved semantic contract when creating a new proof-structure claim. Changing the semantic-contract association of an existing claim requires human authority.",
                        },
                        "state_symbols": {
                            **SEMANTIC_SYMBOL_REFS,
                            "description": "Typed catalog term/state identifiers whose relation forms a state_invariant predicate.",
                        },
                        "observation_scope": {
                            "enum": [
                                "all_reachable_states",
                                "authoritative_states",
                                "user_visible_states",
                                "protocol_internal_states",
                            ],
                            "description": "State horizon over which a state_invariant is interpreted inductively.",
                        },
                        "source_symbols": {
                            **SEMANTIC_SYMBOL_REFS,
                            "description": "Typed catalog term/state identifiers that are the semantic source of a provenance_binding.",
                        },
                        "bound_symbols": {
                            **SEMANTIC_SYMBOL_REFS,
                            "description": "Typed catalog term/state identifiers whose meaning is bound to source_symbols by a provenance_binding.",
                        },
                        "scope_bindings": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": _closed_object(
                                {
                                    "dimension": {"type": "string", "pattern": LOWER_SNAKE, "description": "Semantic scope dimension preserved by this provenance relation."},
                                    "source_symbols": {**SEMANTIC_SYMBOL_REFS, "description": "All provenance source symbols carrying this scope dimension."},
                                    "bound_symbols": {**SEMANTIC_SYMBOL_REFS, "description": "All provenance bound symbols carrying this scope dimension."},
                                },
                                required=("dimension", "source_symbols", "bound_symbols"),
                            ),
                            "description": "Complete preservation map for each semantic scope dimension shared by source and bound sides of a provenance_binding. Every scoped source/bound symbol for that dimension must be enumerated here.",
                        },
                    },
                    required=("class", "definition", "excludes", "automation_reusable"),
                    extra={
                        "allOf": [
                            {
                                "if": {
                                    "properties": {"class": {"const": "state_invariant"}},
                                    "required": ["class"],
                                },
                                "then": {
                                    "required": ["state_symbols", "observation_scope"],
                                    "not": {"anyOf": [{"required": ["source_symbols"]}, {"required": ["bound_symbols"]}]},
                                },
                            },
                            {
                                "if": {
                                    "properties": {"class": {"const": "provenance_binding"}},
                                    "required": ["class"],
                                },
                                "then": {
                                    "required": ["source_symbols", "bound_symbols"],
                                    "not": {"anyOf": [{"required": ["state_symbols"]}, {"required": ["observation_scope"]}]},
                                },
                            },
                            {
                                "if": {
                                    "properties": {"class": {"enum": ["safety_contract", "equivalence_relation"]}},
                                    "required": ["class"],
                                },
                                "then": {
                                    "not": {
                                        "anyOf": [
                                            {"required": ["state_symbols"]},
                                            {"required": ["observation_scope"]},
                                            {"required": ["source_symbols"]},
                                            {"required": ["bound_symbols"]},
                                            {"required": ["scope_bindings"]},
                                        ]
                                    }
                                },
                            },
                        ]
                    },
                )
            ),
            "description": "Reusable semantic relations/boundaries shared by claims. state_invariant contracts define inductive state predicates; provenance_binding contracts define typed origin/binding relations. These entries define claim meaning rather than asserting that the claim is true.",
        },
        "failure_events": {
            **_catalog_map(
                _closed_object(
                    {
                        "class": {
                            "enum": ["allowed", "excluded"],
                            "description": "Whether the event must be tolerated by proofs or is outside the modeled failure envelope.",
                        },
                        "description": {
                            **NONEMPTY_STRING,
                            "description": "Definition of the failure/concurrency event and its modeled meaning.",
                        },
                    },
                    required=("class", "description"),
                )
            ),
            "description": "Global failure model. Entries apply to every proof audit and participate in semantic signatures.",
        },
        "verifier_kinds": {
            **_catalog_map(_closed_object(
                {"description": {**NONEMPTY_STRING, "description": "Definition of a reusable verifier strategy category."}},
                required=("description",),
            )),
            "description": "Small stable taxonomy of verifier strategies; scenario detail belongs in verifier.intent.",
        },
        "sources": {
            "type": "object",
            "minProperties": 1,
            "patternProperties": {
                SOURCE_ID: _closed_object(
                    {
                        "heading": {
                            **NONEMPTY_STRING,
                            "description": "Exact semantic H2 heading text in source.article, excluding any leading numeric display prefix such as `6.`.",
                        },
                    },
                    required=("heading",),
                )
            },
            "additionalProperties": False,
            "description": "Traceability registry keyed by stable semantic source IDs. Positional IDs such as section_6 are forbidden; article numbering is presentation only.",
        },
    },
    required=("terms", "state", "symbolic_dimensions", "mechanisms", "semantic_contracts", "failure_events", "verifier_kinds", "sources"),
)

REFINEMENT_SCHEMA = _closed_object(
    {
        "decisions": {
            "type": "object",
            "patternProperties": {DECISION_ID: DECISION_BODY_SCHEMA},
            "additionalProperties": False,
            "description": "Human semantic decisions required when the current system specification does not determine a proof obligation.",
        },
        "tooling_blockers": {
            "type": "object",
            "patternProperties": {TOOLING_BLOCKER_ID: TOOLING_BLOCKER_BODY_SCHEMA},
            "additionalProperties": False,
            "description": "Resolved or open control-plane defects that prevent trustworthy automated refinement.",
        },
        "nodes": {
            "type": "object",
            "patternProperties": {CLAIM_ID: REFINEMENT_ENTRY_SCHEMA},
            "additionalProperties": False,
            "description": "Per-claim refinement execution state. Keys must be existing claim IDs.",
        },
    },
    required=("decisions", "tooling_blockers", "nodes"),
)

ID_ALLOCATOR_SCHEMA = _closed_object(
    {
        "next_sequence": {
            **_closed_object(
                {prefix: {"type": "integer", "minimum": 1, "description": f"Next unused sequence for {prefix}-prefixed IDs."} for prefix in ("A", "G", "C", "L", "D", "T")},
                required=("A", "G", "C", "L", "D", "T"),
            ),
            "description": "Next unused sequence number for each node-role prefix. This is mutable allocator state, not proof semantics.",
        },
    },
    required=("next_sequence",),
)

SYSTEM_SCHEMA = _closed_object(
    {
        "id": {
            "type": "string",
            "pattern": LOWER_SNAKE,
            "description": "Stable identifier for the system model.",
        },
        "summary": {
            **NONEMPTY_STRING,
            "description": "Neutral system summary injected into every local proof audit and included in semantic signatures.",
        },
        "liveness_boundary": {
            **NONEMPTY_STRING,
            "description": "Global liveness interpretation boundary. Liveness not stated here or in explicit claims/assumptions must not be inferred.",
        },
    },
    required=("id", "summary", "liveness_boundary"),
)

SCOPE_SCHEMA = _closed_object(
    {
        "includes": {
            **_string_list(min_items=1),
            "description": "System/protocol concerns whose correctness is modeled by this DAG.",
        },
        "excludes": {
            **_string_list(min_items=1),
            "description": "Product/protocol concerns intentionally outside this DAG. Failure assumptions belong in catalog.failure_events instead.",
        },
    },
    required=("includes", "excludes"),
)

CANONICAL_GRAPH_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:proof-driven-development:correctness-graph:v0.14",
    "title": "Proof-Driven Development correctness graph",
    "description": (
        "Closed typed representation of one system correctness model. Fixed object fields are schema-defined; "
        "only semantically dynamic maps such as claim IDs, assumption IDs, decisions, refinement/assurance-node IDs, and "
        "catalog entry IDs admit dynamic keys, and those keys are pattern-constrained."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "system",
        "source",
        "scope",
        "catalog",
        "id_allocator",
        "assumptions",
        "roots",
        "claims",
        "refinement",
        "assurance",
    ],
    "properties": {
        "schema_version": {
            "const": SCHEMA_VERSION,
            "description": "Version of the canonical correctness-document schema.",
        },
        "system": {
            **SYSTEM_SCHEMA,
            "description": "System-specific global semantic context consumed by every local proof audit.",
        },
        "source": {
            **_closed_object(
                {
                    "article": {
                        **NONEMPTY_STRING,
                        "description": "Repository-relative architecture document from which the correctness model is derived.",
                    }
                },
                required=("article",),
            ),
            "description": "Traceability pointer to the architecture narrative; this pointer is not itself a correctness premise.",
        },
        "scope": {
            **SCOPE_SCHEMA,
            "description": "Explicit product/protocol boundary of the correctness model. Failure assumptions belong in catalog.failure_events, not here.",
        },
        "catalog": {
            **CATALOG_SCHEMA,
            "description": "Typed vocabulary, state, mechanisms, reusable semantic contracts, failure events, verifier taxonomy, and source-section registry.",
        },
        "id_allocator": {
            **ID_ALLOCATOR_SCHEMA,
            "description": "Mutable allocator state used only by the single-writer mutation engine. ID format and prefix meanings are tool semantics, not YAML configuration.",
        },
        "assumptions": {
            "type": "object",
            "minProperties": 1,
            "patternProperties": {ASSUMPTION_ID: ASSUMPTION_BODY_SCHEMA},
            "additionalProperties": False,
            "description": "Explicit terminal proof boundaries supplied by protocol, environment, or external components.",
        },
        "roots": {
            **_string_list(min_items=1, pattern=ROOT_ID),
            "description": "Claim IDs that define the top-level guarantees of the model.",
        },
        "claims": {
            "type": "object",
            "minProperties": 1,
            "patternProperties": {CLAIM_ID: CLAIM_SCHEMA},
            "additionalProperties": False,
            "description": "Dynamically keyed proof propositions. Keys encode node role and sequence; each value has a closed claim schema.",
        },
        "refinement": {
            **REFINEMENT_SCHEMA,
            "description": "Execution state of the adversarial refinement campaign. Scheduler policy itself is defined by correctness.py, not configurable here.",
        },
        "assurance": {
            **ASSURANCE_SCHEMA,
            "description": "Orthogonal assurance state: specification completeness, dependency-composition review, and executable leaf evidence. These states never act as proof premises or refinement scheduler gates.",
        },
    },
}

# Mutation payloads deliberately omit `kind` because it is carried by add/reclassify operations.
MUTATION_CLAIM_BODY_SCHEMA = _closed_object(CLAIM_BODY_PROPERTIES, required=("statement",))
MUTATION_CLAIM_UPDATE_SCHEMA = _closed_object(CLAIM_BODY_PROPERTIES)
MUTATION_ASSUMPTION_BODY_SCHEMA = ASSUMPTION_BODY_SCHEMA
MUTATION_DECISION_BODY_SCHEMA = _closed_object(
    {
        "question": {
            **NONEMPTY_STRING,
            "description": "Exact system-semantic choice that existing specification cannot determine automatically.",
        },
        "reason": {
            **NONEMPTY_STRING,
            "description": "Why proof progress requires human architecture/product judgment rather than proof-structure refinement.",
        },
        "options": {
            **_string_list(min_items=1),
            "description": "Concrete candidate semantic choices presented for human judgment.",
        },
        "status": {
            "enum": list(DECISION_STATUSES),
            "description": "Whether the semantic decision is still open or has been resolved.",
        },
        "resolution": {
            **NONEMPTY_STRING,
            "description": "Chosen semantic decision and its operative meaning; required when status is resolved.",
        },
    },
    required=("question", "reason", "options"),
)
MUTATION_TOOLING_BLOCKER_BODY_SCHEMA = _closed_object(
    {
        "status": {
            "enum": list(TOOLING_BLOCKER_STATUSES),
            "description": "Whether this control-plane defect still globally blocks automated refinement.",
        },
        "issue": {
            **NONEMPTY_STRING,
            "description": "Concrete defect or limitation in the correctness tooling/control plane.",
        },
        "reason": {
            **NONEMPTY_STRING,
            "description": "Why continuing despite the tooling defect would make refinement untrustworthy or unrepresentable.",
        },
        "suggested_change": {
            **NONEMPTY_STRING,
            "description": "Optional proposed tooling repair; advisory rather than a system-semantic decision.",
        },
        "resolution": {
            **NONEMPTY_STRING,
            "description": "Description of the completed tooling repair; required when status is resolved.",
        },
    },
    required=("issue", "reason"),
)
