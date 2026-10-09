from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1

NONEMPTY_STRING: dict[str, Any] = {"type": "string", "minLength": 1}
SHA256: dict[str, Any] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
SYMBOLIC_NAME: dict[str, Any] = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]*$"}
ANCHOR: dict[str, Any] = {
    "type": "string",
    "pattern": "^(?:contract|claim|source):[A-Za-z0-9_]+$",
}


def _closed(properties: dict[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


FORMULA_SCHEMA: dict[str, Any] = {
    "oneOf": [
        _closed({"var": NONEMPTY_STRING}, ("var",)),
        _closed({"bool": {"type": "boolean"}}, ("bool",)),
        _closed(
            {
                "call": _closed(
                    {
                        "fn": SYMBOLIC_NAME,
                        "args": {"type": "array", "items": {"$ref": "#/$defs/formula"}},
                    },
                    ("fn", "args"),
                )
            },
            ("call",),
        ),
        *[
            _closed(
                {
                    op: {
                        "type": "array",
                        "prefixItems": [
                            {"$ref": "#/$defs/formula"},
                            {"$ref": "#/$defs/formula"},
                        ],
                        "minItems": 2,
                        "maxItems": 2,
                    }
                },
                (op,),
            )
            for op in ("eq", "neq", "implies")
        ],
        *[
            _closed(
                {op: {"type": "array", "items": {"$ref": "#/$defs/formula"}}},
                (op,),
            )
            for op in ("and", "or")
        ],
        _closed({"not": {"$ref": "#/$defs/formula"}}, ("not",)),
        *[
            _closed(
                {
                    op: _closed(
                        {
                            "vars": {
                                "type": "object",
                                "minProperties": 1,
                                "propertyNames": SYMBOLIC_NAME,
                                "additionalProperties": SYMBOLIC_NAME,
                            },
                            "body": {"$ref": "#/$defs/formula"},
                        },
                        ("vars", "body"),
                    )
                },
                (op,),
            )
            for op in ("forall", "exists")
        ],
    ]
}

FUNCTION_SCHEMA = _closed(
    {
        "args": {
            "type": "array",
            "items": SYMBOLIC_NAME,
        },
        "returns": SYMBOLIC_NAME,
        "catalog_symbols": {
            "type": "array",
            "items": SYMBOLIC_NAME,
            "uniqueItems": True,
        },
        "meaning": NONEMPTY_STRING,
        "dimensions": {
            "type": "array",
            "items": SYMBOLIC_NAME,
            "uniqueItems": True,
        },
        "semantic_anchors": {
            "type": "array",
            "items": ANCHOR,
            "minItems": 1,
            "uniqueItems": True,
        },
    },
    ("args", "returns", "catalog_symbols", "meaning"),
)
FUNCTION_SCHEMA["anyOf"] = [
    {"properties": {"catalog_symbols": {"minItems": 1}}},
    {"required": ["semantic_anchors"]},
]

CLAIM_MAPPING_SCHEMA = _closed(
    {
        "contracts": {
            "type": "array",
            "items": SYMBOLIC_NAME,
            "uniqueItems": True,
        },
        "formula": {"$ref": "#/$defs/formula"},
    },
    ("contracts", "formula"),
)

STATE_INVARIANT_MAPPING_SCHEMA = _closed(
    {
        "class": {"const": "state_invariant"},
        "observation_scope": NONEMPTY_STRING,
        "formula": {"$ref": "#/$defs/formula"},
    },
    ("class", "observation_scope", "formula"),
)

PROVENANCE_MAPPING_SCHEMA = _closed(
    {
        "class": {"const": "provenance_binding"},
        "formula": {"$ref": "#/$defs/formula"},
    },
    ("class", "formula"),
)

SAFETY_CONTRACT_MAPPING_SCHEMA = _closed(
    {
        "class": {"const": "safety_contract"},
        "formula": {"$ref": "#/$defs/formula"},
    },
    ("class", "formula"),
)

BRIDGE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:proof-driven-development:symbolic-bridge:v1",
    "type": "object",
    "properties": {
        "version": {"const": 1},
        "sorts": {
            "type": "array",
            "items": SYMBOLIC_NAME,
            "uniqueItems": True,
        },
        "functions": {
            "type": "object",
            "propertyNames": SYMBOLIC_NAME,
            "additionalProperties": {"$ref": "#/$defs/function"},
        },
        "claims": {
            "type": "object",
            "propertyNames": NONEMPTY_STRING,
            "additionalProperties": {"$ref": "#/$defs/claim_mapping"},
        },
        "contracts": {
            "type": "object",
            "propertyNames": SYMBOLIC_NAME,
            "additionalProperties": {
                "oneOf": [
                    {"$ref": "#/$defs/state_invariant_mapping"},
                    {"$ref": "#/$defs/provenance_mapping"},
                    {"$ref": "#/$defs/safety_contract_mapping"},
                ]
            },
        },
    },
    "required": ["version", "sorts", "functions", "claims", "contracts"],
    "additionalProperties": False,
    "$defs": {
        "formula": FORMULA_SCHEMA,
        "function": FUNCTION_SCHEMA,
        "claim_mapping": CLAIM_MAPPING_SCHEMA,
        "state_invariant_mapping": STATE_INVARIANT_MAPPING_SCHEMA,
        "provenance_mapping": PROVENANCE_MAPPING_SCHEMA,
        "safety_contract_mapping": SAFETY_CONTRACT_MAPPING_SCHEMA,
    },
}

ROUNDTRIP_SCHEMA = _closed(
    {
        "subject_signature": SHA256,
        "status": {"enum": ["TRANSLATED", "INCOMPLETE"]},
        "natural_language": {"type": "string"},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
    },
    ("subject_signature", "status", "natural_language", "ambiguities"),
)
ROUNDTRIP_SCHEMA["allOf"] = [
    {
        "if": {"properties": {"status": {"const": "TRANSLATED"}}, "required": ["status"]},
        "then": {"properties": {"natural_language": {"minLength": 1}}},
    }
]

REVIEW_SCHEMA = _closed(
    {
        "subject_signature": SHA256,
        "verdict": {
            "enum": ["EQUIVALENT", "WEAKER", "STRONGER", "MISMATCH", "INCOMPLETE"]
        },
        "reason": NONEMPTY_STRING,
        "differences": {"type": "array", "items": {"type": "string"}},
    },
    ("subject_signature", "verdict", "reason", "differences"),
)
REVIEW_SCHEMA["allOf"] = [
    {
        "if": {"properties": {"verdict": {"const": "EQUIVALENT"}}, "required": ["verdict"]},
        "then": {"properties": {"differences": {"maxItems": 0}}},
    }
]

REVIEW_SET_SCHEMA = _closed(
    {
        "weakening": {"$ref": "#/$defs/review"},
        "strengthening": {"$ref": "#/$defs/review"},
    },
    ("weakening", "strengthening"),
)

ASSURANCE_RECORD_SCHEMA = _closed(
    {
        "kind": {"enum": ["contract", "claim"]},
        "subject_id": NONEMPTY_STRING,
        "subject_signature": SHA256,
        "roundtrip": {"$ref": "#/$defs/roundtrip"},
        "direct_reviews": {"$ref": "#/$defs/review_set"},
        "roundtrip_reviews": {"$ref": "#/$defs/review_set"},
    },
    (
        "kind",
        "subject_id",
        "subject_signature",
        "roundtrip",
        "direct_reviews",
        "roundtrip_reviews",
    ),
)

ASSURANCE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:proof-driven-development:translation-assurance:v1",
    **_closed(
        {
            "version": {"const": 1},
            "records": {
                "type": "object",
                "propertyNames": {
                    "type": "string",
                    "pattern": "^(?:contract|claim):[A-Za-z0-9_]+$",
                },
                "additionalProperties": {"$ref": "#/$defs/assurance_record"},
            },
        },
        ("version", "records"),
    ),
    "$defs": {
        "roundtrip": ROUNDTRIP_SCHEMA,
        "review": REVIEW_SCHEMA,
        "review_set": REVIEW_SET_SCHEMA,
        "assurance_record": ASSURANCE_RECORD_SCHEMA,
    },
}

COVERAGE_CASE_SCHEMA = _closed(
    {
        "description": NONEMPTY_STRING,
        "candidate_statement": NONEMPTY_STRING,
        "trusted_claims": {
            "type": "array",
            "items": NONEMPTY_STRING,
            "uniqueItems": True,
        },
        "trusted_contracts": {
            "type": "array",
            "items": SYMBOLIC_NAME,
            "uniqueItems": True,
        },
        "artifact": {
            "type": "string",
            "pattern": "^symbolic_artifacts/[A-Za-z0-9_.-]+\\.coverage\\.yaml$",
        },
        "formula": {"$ref": "#/$defs/formula"},
    },
    (
        "description",
        "candidate_statement",
        "trusted_claims",
        "trusted_contracts",
        "artifact",
        "formula",
    ),
)
COVERAGE_CASE_SCHEMA["anyOf"] = [
    {"properties": {"trusted_claims": {"minItems": 1}}},
    {"properties": {"trusted_contracts": {"minItems": 1}}},
]

COVERAGE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:proof-driven-development:symbolic-coverage:v1",
    **_closed(
        {
            "version": {"const": 1},
            "cases": {
                "type": "object",
                "propertyNames": NONEMPTY_STRING,
                "additionalProperties": {"$ref": "#/$defs/coverage_case"},
            },
        },
        ("version", "cases"),
    ),
    "$defs": {
        "formula": FORMULA_SCHEMA,
        "coverage_case": COVERAGE_CASE_SCHEMA,
    },
}

DESIGN_OBLIGATION_CASE_SCHEMA = _closed(
    {
        "description": NONEMPTY_STRING,
        "statement": NONEMPTY_STRING,
        "source_refs": {
            "type": "array",
            "items": SYMBOLIC_NAME,
            "minItems": 1,
            "uniqueItems": True,
        },
        "coverage_scope": {"enum": ["selected_constraints", "all_roots"]},
        "claims": {
            "type": "array",
            "items": NONEMPTY_STRING,
            "uniqueItems": True,
        },
        "contracts": {
            "type": "array",
            "items": SYMBOLIC_NAME,
            "uniqueItems": True,
        },
        "formula": {"$ref": "#/$defs/formula"},
    },
    (
        "description",
        "statement",
        "source_refs",
        "coverage_scope",
        "claims",
        "contracts",
        "formula",
    ),
)
DESIGN_OBLIGATION_CASE_SCHEMA["allOf"] = [
    {
        "if": {
            "properties": {"coverage_scope": {"const": "selected_constraints"}},
            "required": ["coverage_scope"],
        },
        "then": {
            "anyOf": [
                {"properties": {"claims": {"minItems": 1}}},
                {"properties": {"contracts": {"minItems": 1}}},
            ]
        },
    },
    {
        "if": {
            "properties": {"coverage_scope": {"const": "all_roots"}},
            "required": ["coverage_scope"],
        },
        "then": {
            "properties": {
                "claims": {"maxItems": 0},
                "contracts": {"maxItems": 0},
            }
        },
    },
]

DESIGN_OBLIGATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:proof-driven-development:design-obligations:v1",
    **_closed(
        {
            "version": {"const": 1},
            "obligations": {
                "type": "object",
                "propertyNames": NONEMPTY_STRING,
                "additionalProperties": {"$ref": "#/$defs/design_obligation"},
            },
        },
        ("version", "obligations"),
    ),
    "$defs": {
        "formula": FORMULA_SCHEMA,
        "design_obligation": DESIGN_OBLIGATION_CASE_SCHEMA,
    },
}

TRUST_SCHEMA = _closed(
    {
        "status": {"const": "TRUSTED"},
        "subject_signature": SHA256,
    },
    ("status", "subject_signature"),
)

SOLVER_SCHEMA = _closed(
    {
        "name": {"const": "z3"},
        "version": NONEMPTY_STRING,
    },
    ("name", "version"),
)

MUTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "weakened_premise": NONEMPTY_STRING,
        "operation": {"const": "replace_formula"},
        "premise_formula_override": {"$ref": "#/$defs/formula"},
        "description": NONEMPTY_STRING,
        "expected": NONEMPTY_STRING,
        "solver_status": {"const": "sat"},
        "verdict": {"const": "COUNTEREXAMPLE"},
        "counterexample_model": NONEMPTY_STRING,
    },
    "required": [
        "weakened_premise",
        "operation",
        "premise_formula_override",
        "description",
        "expected",
        "solver_status",
        "verdict",
        "counterexample_model",
    ],
    "additionalProperties": False,
}

COMPOSITION_ARTIFACT_BASE = {
    "version": {"const": 1},
    "kind": {"const": "symbolic_composition_machine_check"},
    "node": NONEMPTY_STRING,
    "direct_premises": {
        "type": "array",
        "items": NONEMPTY_STRING,
        "minItems": 1,
        "uniqueItems": True,
    },
    "composition_signature": SHA256,
    "translation_assurance": _closed(
        {
            "target": {"$ref": "#/$defs/trust"},
            "premises": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {"$ref": "#/$defs/trust"},
            },
        },
        ("target", "premises"),
    ),
    "query_semantics": {"const": "AND(all direct premise formulas) AND NOT(target formula)"},
    "solver": {"$ref": "#/$defs/solver"},
    "result": _closed(
        {
            "solver_status": {"const": "unsat"},
            "verdict": {"const": "ENTAILED"},
            "counterexample_model": {"type": "null"},
        },
        ("solver_status", "verdict", "counterexample_model"),
    ),
}

COMPOSITION_ARTIFACT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:proof-driven-development:symbolic-composition-artifact:v1",
    "oneOf": [
        _closed(
            {
                **COMPOSITION_ARTIFACT_BASE,
                "mutation_test": {"$ref": "#/$defs/mutation"},
            },
            tuple(COMPOSITION_ARTIFACT_BASE) + ("mutation_test",),
        ),
        _closed(
            {
                **COMPOSITION_ARTIFACT_BASE,
                "mutation_tests": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/mutation"},
                    "minItems": 1,
                },
            },
            tuple(COMPOSITION_ARTIFACT_BASE) + ("mutation_tests",),
        ),
    ],
    "$defs": {
        "trust": TRUST_SCHEMA,
        "solver": SOLVER_SCHEMA,
        "formula": FORMULA_SCHEMA,
        "mutation": MUTATION_SCHEMA,
    },
}

VOCABULARY_ENTRY_SCHEMA = _closed(
    {
        "type": NONEMPTY_STRING,
        "meaning": NONEMPTY_STRING,
    },
    ("type", "meaning"),
)

TRUSTED_CONSTRAINT_SCHEMA = _closed(
    {
        "id": NONEMPTY_STRING,
        "statement": NONEMPTY_STRING,
        "formula": {"$ref": "#/$defs/formula"},
        "translation_assurance": {"$ref": "#/$defs/trust"},
    },
    ("id", "statement", "formula", "translation_assurance"),
)

COVERAGE_ARTIFACT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:proof-driven-development:symbolic-coverage-artifact:v1",
    **_closed(
        {
            "version": {"const": 1},
            "kind": {"const": "symbolic_coverage_machine_check"},
            "case": NONEMPTY_STRING,
            "artifact_ref": {
                "type": "string",
                "pattern": "^symbolic_artifacts/[A-Za-z0-9_.-]+\\.coverage\\.yaml$",
            },
            "coverage_signature": SHA256,
            "candidate": _closed(
                {
                    "statement": NONEMPTY_STRING,
                    "description": NONEMPTY_STRING,
                    "signature": SHA256,
                    "formula": {"$ref": "#/$defs/formula"},
                    "vocabulary": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": {"$ref": "#/$defs/vocabulary_entry"},
                    },
                },
                ("statement", "description", "signature", "formula", "vocabulary"),
            ),
            "trusted_constraints": _closed(
                {
                    "claims": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/trusted_constraint"},
                    },
                    "contracts": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/trusted_constraint"},
                    },
                },
                ("claims", "contracts"),
            ),
            "proof": _closed(
                {
                    "baseline": _closed(
                        {
                            "query_semantics": {"const": "candidate_bad_state"},
                            "solver_status": {"const": "sat"},
                            "witness_model": NONEMPTY_STRING,
                            "meaning": NONEMPTY_STRING,
                        },
                        ("query_semantics", "solver_status", "witness_model", "meaning"),
                    ),
                    "constrained": _closed(
                        {
                            "query_semantics": {
                                "const": "AND(all trusted constraint formulas) AND candidate_bad_state"
                            },
                            "solver_status": {"const": "unsat"},
                            "counterexample_model": {"type": "null"},
                            "meaning": NONEMPTY_STRING,
                        },
                        (
                            "query_semantics",
                            "solver_status",
                            "counterexample_model",
                            "meaning",
                        ),
                    ),
                },
                ("baseline", "constrained"),
            ),
            "solver": {"$ref": "#/$defs/solver"},
            "result": _closed(
                {
                    "verdict": {"const": "EXCLUDED"},
                    "machine_checked": {"const": True},
                    "closes_counterexample": {"const": True},
                    "meaning": NONEMPTY_STRING,
                },
                ("verdict", "machine_checked", "closes_counterexample", "meaning"),
            ),
        },
        (
            "version",
            "kind",
            "case",
            "artifact_ref",
            "coverage_signature",
            "candidate",
            "trusted_constraints",
            "proof",
            "solver",
            "result",
        ),
    ),
    "$defs": {
        "formula": FORMULA_SCHEMA,
        "trust": TRUST_SCHEMA,
        "solver": SOLVER_SCHEMA,
        "vocabulary_entry": VOCABULARY_ENTRY_SCHEMA,
        "trusted_constraint": TRUSTED_CONSTRAINT_SCHEMA,
    },
}

FORMAL_SCHEMAS: dict[str, dict[str, Any]] = {
    "bridge": BRIDGE_SCHEMA,
    "assurance": ASSURANCE_SCHEMA,
    "coverage": COVERAGE_SCHEMA,
    "design-obligations": DESIGN_OBLIGATION_SCHEMA,
    "composition-artifact": COMPOSITION_ARTIFACT_SCHEMA,
    "coverage-artifact": COVERAGE_ARTIFACT_SCHEMA,
}
