from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from ..model import Graph, UniqueKeyLoader, _catalog_namespace
from .dsl import ParsedProgram, SymbolicSchemaError, parse_program


class SymbolicBridgeError(ValueError):
    """Raised when a symbolic mapping drifts from its canonical semantic contract."""


@dataclass(frozen=True)
class ContractMapping:
    contract_id: str
    contract_class: str
    formula: dict[str, Any]
    catalog_symbols: frozenset[str]
    observation_scope: str | None = None


@dataclass(frozen=True)
class ClaimMapping:
    claim_id: str
    contract_ids: tuple[str, ...]
    formula: dict[str, Any]
    catalog_symbols: frozenset[str]


@dataclass(frozen=True)
class SymbolicBridge:
    version: int
    sorts: tuple[str, ...]
    functions: dict[str, dict[str, Any]]
    contracts: dict[str, ContractMapping]
    claims: dict[str, ClaimMapping]

    @classmethod
    def load(cls, path: Path) -> "SymbolicBridge":
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        except OSError as exc:
            raise SymbolicBridgeError(f"symbolic bridge unavailable at {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise SymbolicBridgeError(f"invalid symbolic bridge YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise SymbolicBridgeError("symbolic bridge must be a mapping")
        return cls.from_data(raw)

    @classmethod
    def from_data(cls, raw: dict[str, Any]) -> "SymbolicBridge":
        allowed = {"version", "sorts", "functions", "contracts", "claims"}
        unknown = set(raw) - allowed
        if unknown:
            raise SymbolicBridgeError(f"unknown symbolic bridge fields: {sorted(unknown)}")
        if raw.get("version") != 1:
            raise SymbolicBridgeError("symbolic bridge version must be 1")

        sorts = raw.get("sorts")
        if not isinstance(sorts, list) or not all(isinstance(x, str) and x for x in sorts):
            raise SymbolicBridgeError("symbolic bridge sorts must be non-empty strings")
        if len(sorts) != len(set(sorts)):
            raise SymbolicBridgeError("symbolic bridge sort names must be unique")

        functions = raw.get("functions")
        if not isinstance(functions, dict):
            raise SymbolicBridgeError("symbolic bridge functions must be a mapping")
        normalized_functions: dict[str, dict[str, Any]] = {}
        for name, decl in functions.items():
            if not isinstance(name, str) or not name or not isinstance(decl, dict):
                raise SymbolicBridgeError("symbolic bridge function declarations must be mappings")
            required_function_fields = {"args", "returns", "catalog_symbols", "meaning"}
            allowed_function_fields = required_function_fields | {"dimensions", "semantic_anchors"}
            if not required_function_fields.issubset(decl) or set(decl) - allowed_function_fields:
                raise SymbolicBridgeError(
                    f"function {name} must contain args, returns, catalog_symbols, and meaning; "
                    "dimensions is the only optional field"
                )
            args = decl["args"]
            result = decl["returns"]
            symbols = decl["catalog_symbols"]
            meaning = decl["meaning"]
            dimensions = decl.get("dimensions")
            semantic_anchors = decl.get("semantic_anchors", [])
            if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
                raise SymbolicBridgeError(f"function {name}.args must be a list of sort names")
            if not isinstance(result, str):
                raise SymbolicBridgeError(f"function {name}.returns must be a sort name")
            if not isinstance(symbols, list) or not all(isinstance(x, str) for x in symbols):
                raise SymbolicBridgeError(
                    f"function {name}.catalog_symbols must be a list of strings"
                )
            if len(symbols) != len(set(symbols)):
                raise SymbolicBridgeError(
                    f"function {name}.catalog_symbols must not contain duplicates"
                )
            if (
                not isinstance(semantic_anchors, list)
                or not all(
                    isinstance(item, str)
                    and re.fullmatch(r"(?:contract|claim|source):[A-Za-z0-9_]+", item)
                    for item in semantic_anchors
                )
                or len(semantic_anchors) != len(set(semantic_anchors))
            ):
                raise SymbolicBridgeError(
                    f"function {name}.semantic_anchors must be a unique list of contract:<id>, claim:<id>, or source:<id> references"
                )
            if not symbols and not semantic_anchors:
                raise SymbolicBridgeError(
                    f"function {name} must be anchored by catalog_symbols or semantic_anchors"
                )
            if not isinstance(meaning, str) or not meaning.strip():
                raise SymbolicBridgeError(
                    f"function {name}.meaning must be a non-empty string"
                )
            if dimensions is not None:
                if (
                    not isinstance(dimensions, list)
                    or not dimensions
                    or not all(
                        isinstance(item, str)
                        and re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", item)
                        for item in dimensions
                    )
                ):
                    raise SymbolicBridgeError(
                        f"function {name}.dimensions must be a non-empty list of lower_snake semantic coordinates"
                    )
                if len(dimensions) != len(set(dimensions)):
                    raise SymbolicBridgeError(
                        f"function {name}.dimensions must not contain duplicates"
                    )
                if len(dimensions) != len(args):
                    raise SymbolicBridgeError(
                        f"function {name}.dimensions must align 1:1 with args; "
                        f"got {len(dimensions)} dimensions for {len(args)} args"
                    )
            normalized_functions[name] = {
                "args": list(args),
                "returns": result,
                "catalog_symbols": list(symbols),
                "meaning": meaning.strip(),
                **({"dimensions": list(dimensions)} if dimensions is not None else {}),
                **({"semantic_anchors": list(semantic_anchors)} if semantic_anchors else {}),
            }

        contracts = raw.get("contracts")
        if not isinstance(contracts, dict):
            raise SymbolicBridgeError("symbolic bridge contracts must be a mapping")
        normalized_contracts: dict[str, ContractMapping] = {}
        for contract_id, mapping in contracts.items():
            if not isinstance(contract_id, str) or not contract_id or not isinstance(mapping, dict):
                raise SymbolicBridgeError("symbolic contract mappings must be named mappings")
            contract_class = mapping.get("class")
            expected_fields = {"class", "formula", "observation_scope"} if contract_class == "state_invariant" else {"class", "formula"}
            if set(mapping) != expected_fields:
                raise SymbolicBridgeError(
                    f"contract mapping {contract_id} must contain exactly {sorted(expected_fields)}"
                )
            formula = mapping["formula"]
            if not isinstance(contract_class, str) or not isinstance(formula, dict):
                raise SymbolicBridgeError(
                    f"contract mapping {contract_id} has invalid class/formula"
                )
            observation_scope = mapping.get("observation_scope")
            if observation_scope is not None and not isinstance(observation_scope, str):
                raise SymbolicBridgeError(
                    f"contract mapping {contract_id}.observation_scope must be a string"
                )
            used_functions = _formula_function_names(formula)
            unknown_functions = used_functions - set(normalized_functions)
            if unknown_functions:
                raise SymbolicBridgeError(
                    f"contract mapping {contract_id} uses unknown functions {sorted(unknown_functions)}"
                )
            catalog_symbols: set[str] = set()
            for fn in used_functions:
                catalog_symbols.update(normalized_functions[fn]["catalog_symbols"])
                anchors = normalized_functions[fn].get("semantic_anchors", [])
                if anchors and f"contract:{contract_id}" not in anchors:
                    raise SymbolicBridgeError(
                        f"contract mapping {contract_id} uses function {fn} outside its semantic_anchors {anchors}"
                    )
            normalized_contracts[contract_id] = ContractMapping(
                contract_id=contract_id,
                contract_class=contract_class,
                formula=formula,
                catalog_symbols=frozenset(catalog_symbols),
                observation_scope=observation_scope,
            )

        raw_claims = raw.get("claims", {})
        if not isinstance(raw_claims, dict):
            raise SymbolicBridgeError("symbolic bridge claims must be a mapping")
        normalized_claims: dict[str, ClaimMapping] = {}
        for claim_id, mapping in raw_claims.items():
            if not isinstance(claim_id, str) or not claim_id or not isinstance(mapping, dict):
                raise SymbolicBridgeError("symbolic claim mappings must be named mappings")
            if set(mapping) != {"contracts", "formula"}:
                raise SymbolicBridgeError(
                    f"claim mapping {claim_id} must contain exactly contracts and formula"
                )
            contract_ids = mapping["contracts"]
            formula = mapping["formula"]
            if (
                not isinstance(contract_ids, list)
                or not all(isinstance(item, str) and item for item in contract_ids)
            ):
                raise SymbolicBridgeError(
                    f"claim mapping {claim_id}.contracts must be a list of non-empty strings"
                )
            if len(contract_ids) != len(set(contract_ids)):
                raise SymbolicBridgeError(
                    f"claim mapping {claim_id}.contracts must not contain duplicates"
                )
            missing_contracts = sorted(set(contract_ids) - set(normalized_contracts))
            if missing_contracts:
                raise SymbolicBridgeError(
                    f"claim mapping {claim_id} references unmapped contracts {missing_contracts}"
                )
            if not isinstance(formula, dict):
                raise SymbolicBridgeError(
                    f"claim mapping {claim_id}.formula must be a mapping"
                )
            used_functions = _formula_function_names(formula)
            unknown_functions = used_functions - set(normalized_functions)
            if unknown_functions:
                raise SymbolicBridgeError(
                    f"claim mapping {claim_id} uses unknown functions {sorted(unknown_functions)}"
                )
            catalog_symbols: set[str] = set()
            allowed_anchors = {f"claim:{claim_id}"} | {
                f"contract:{contract_id}" for contract_id in contract_ids
            }
            for fn in used_functions:
                catalog_symbols.update(normalized_functions[fn]["catalog_symbols"])
                anchors = normalized_functions[fn].get("semantic_anchors", [])
                if anchors and not (set(anchors) & allowed_anchors):
                    raise SymbolicBridgeError(
                        f"claim mapping {claim_id} uses function {fn} outside its semantic_anchors {anchors}"
                    )
            normalized_claims[claim_id] = ClaimMapping(
                claim_id=claim_id,
                contract_ids=tuple(contract_ids),
                formula=formula,
                catalog_symbols=frozenset(catalog_symbols),
            )

        return cls(
            version=1,
            sorts=tuple(sorts),
            functions=normalized_functions,
            contracts=normalized_contracts,
            claims=normalized_claims,
        )

    def validate_against_graph(self, graph: Graph) -> None:
        formula_functions = {
            name: {"args": decl["args"], "returns": decl["returns"]}
            for name, decl in self.functions.items()
        }
        for label, formula in [
            *[(f"contract {cid}", mapping.formula) for cid, mapping in self.contracts.items()],
            *[(f"claim {cid}", mapping.formula) for cid, mapping in self.claims.items()],
        ]:
            try:
                parse_program(
                    {
                        "sorts": list(self.sorts),
                        "functions": formula_functions,
                        "constraints": [formula],
                        "query": {"bool": True},
                    }
                )
            except SymbolicSchemaError as exc:
                raise SymbolicBridgeError(
                    f"{label} formula failed type/schema checking: {exc}"
                ) from exc

        semantic_contracts = _catalog_namespace(graph, "semantic_contracts")
        known_catalog_symbols = set(_catalog_namespace(graph, "terms")) | set(
            _catalog_namespace(graph, "state")
        )

        state_catalog = _catalog_namespace(graph, "state")
        for fn_name, decl in self.functions.items():
            unknown = set(decl["catalog_symbols"]) - known_catalog_symbols
            if unknown:
                raise SymbolicBridgeError(
                    f"function {fn_name} maps unknown catalog symbols {sorted(unknown)}"
                )

            for anchor in decl.get("semantic_anchors", []):
                kind, subject_id = anchor.split(":", 1)
                if kind == "contract" and subject_id not in semantic_contracts:
                    raise SymbolicBridgeError(
                        f"function {fn_name} references unknown semantic anchor {anchor}"
                    )
                if kind == "claim" and subject_id not in graph.claims:
                    raise SymbolicBridgeError(
                        f"function {fn_name} references unknown semantic anchor {anchor}"
                    )
                if kind == "source" and subject_id not in _catalog_namespace(graph, "sources"):
                    raise SymbolicBridgeError(
                        f"function {fn_name} references unknown semantic anchor {anchor}"
                    )

            required_shapes = {
                tuple(state_catalog[symbol]["symbolic_dimensions"])
                for symbol in decl["catalog_symbols"]
                if symbol in state_catalog
                and isinstance(state_catalog[symbol], dict)
                and state_catalog[symbol].get("symbolic_dimensions") is not None
            }
            declared_dimensions = decl.get("dimensions")
            if len(required_shapes) > 1:
                raise SymbolicBridgeError(
                    f"function {fn_name} maps state symbols with incompatible symbolic_dimensions: "
                    f"{sorted(required_shapes)}"
                )
            if required_shapes:
                expected_dimensions = list(next(iter(required_shapes)))
                if declared_dimensions != expected_dimensions:
                    raise SymbolicBridgeError(
                        f"function {fn_name} dimension mismatch for canonical state mapping: "
                        f"expected={expected_dimensions}, actual={declared_dimensions}"
                    )
            elif declared_dimensions is not None:
                raise SymbolicBridgeError(
                    f"function {fn_name} declares dimensions {declared_dimensions} but maps no "
                    "canonical state symbol with symbolic_dimensions; semantic dimensions must "
                    "originate in the canonical catalog"
                )

        for contract_id, mapping in self.contracts.items():
            contract = semantic_contracts.get(contract_id)
            if not isinstance(contract, dict):
                raise SymbolicBridgeError(
                    f"symbolic mapping references unknown semantic contract {contract_id}"
                )
            canonical_class = contract.get("class")
            if canonical_class != mapping.contract_class:
                raise SymbolicBridgeError(
                    f"{contract_id} class mismatch: canonical={canonical_class!r}, "
                    f"symbolic={mapping.contract_class!r}"
                )
            if canonical_class == "state_invariant":
                canonical_scope = contract.get("observation_scope")
                if mapping.observation_scope != canonical_scope:
                    raise SymbolicBridgeError(
                        f"{contract_id} observation_scope mismatch: canonical={canonical_scope!r}, "
                        f"symbolic={mapping.observation_scope!r}"
                    )
            expected = _expected_contract_symbols(contract_id, contract)
            if canonical_class != "safety_contract" and mapping.catalog_symbols != expected:
                missing = sorted(expected - mapping.catalog_symbols)
                extra = sorted(mapping.catalog_symbols - expected)
                raise SymbolicBridgeError(
                    f"{contract_id} symbolic catalog coverage mismatch: "
                    f"missing={missing}, extra={extra}"
                )

        for claim_id, mapping in self.claims.items():
            claim = graph.claims.get(claim_id)
            if not isinstance(claim, dict):
                raise SymbolicBridgeError(
                    f"symbolic claim mapping references unknown claim {claim_id}"
                )
            canonical_contracts = claim.get("semantic_contracts", [])
            if not isinstance(canonical_contracts, list) or not all(
                isinstance(item, str) for item in canonical_contracts
            ):
                raise SymbolicBridgeError(
                    f"claim {claim_id} lacks valid semantic_contracts"
                )
            if set(canonical_contracts) != set(mapping.contract_ids):
                missing = sorted(set(canonical_contracts) - set(mapping.contract_ids))
                extra = sorted(set(mapping.contract_ids) - set(canonical_contracts))
                raise SymbolicBridgeError(
                    f"{claim_id} symbolic claim-contract coverage mismatch: "
                    f"missing={missing}, extra={extra}"
                )
            expected_symbols: set[str] = set(_direct_claim_catalog_symbols(claim, known_catalog_symbols))
            for contract_id in mapping.contract_ids:
                expected_symbols.update(self.contracts[contract_id].catalog_symbols)
            if mapping.catalog_symbols != frozenset(expected_symbols):
                missing = sorted(expected_symbols - set(mapping.catalog_symbols))
                extra = sorted(set(mapping.catalog_symbols) - expected_symbols)
                raise SymbolicBridgeError(
                    f"{claim_id} symbolic claim catalog coverage mismatch: "
                    f"missing={missing}, extra={extra}"
                )

    def validate_formula_anchors(
        self,
        formula: dict[str, Any],
        *,
        claim_ids: list[str] | tuple[str, ...] = (),
        contract_ids: list[str] | tuple[str, ...] = (),
        label: str = "symbolic formula",
    ) -> None:
        """Reject opaque predicates whose canonical provenance is outside this formula's subjects."""
        expanded_contracts = set(contract_ids)
        for claim_id in claim_ids:
            mapping = self.claims.get(claim_id)
            if mapping is not None:
                expanded_contracts.update(mapping.contract_ids)
        allowed = {f"claim:{claim_id}" for claim_id in claim_ids} | {
            f"contract:{contract_id}" for contract_id in expanded_contracts
        }
        for fn in sorted(_formula_function_names(formula)):
            decl = self.functions.get(fn)
            if decl is None:
                continue
            anchors = set(decl.get("semantic_anchors", []))
            if anchors and not (anchors & allowed):
                raise SymbolicBridgeError(
                    f"{label} uses opaque function {fn} outside its semantic_anchors "
                    f"{sorted(anchors)}; allowed subjects={sorted(allowed)}"
                )

    def compile_program(
        self,
        graph: Graph,
        *,
        contract_ids: list[str],
        query: dict[str, Any],
        extra_constraints: list[dict[str, Any]] | None = None,
    ) -> ParsedProgram:
        self.validate_against_graph(graph)
        missing = [contract_id for contract_id in contract_ids if contract_id not in self.contracts]
        if missing:
            raise SymbolicBridgeError(
                f"no symbolic mapping for semantic contracts {sorted(missing)}"
            )
        program = {
            "sorts": list(self.sorts),
            "functions": {
                name: {"args": decl["args"], "returns": decl["returns"]}
                for name, decl in self.functions.items()
            },
            "constraints": [
                self.contracts[contract_id].formula for contract_id in contract_ids
            ]
            + list(extra_constraints or []),
            "query": query,
        }
        try:
            return parse_program(program)
        except SymbolicSchemaError as exc:
            raise SymbolicBridgeError(f"symbolic program failed type/schema checking: {exc}") from exc


    def compile_claim_program(
        self,
        graph: Graph,
        *,
        claim_ids: list[str],
        query: dict[str, Any],
        extra_constraints: list[dict[str, Any]] | None = None,
    ) -> ParsedProgram:
        self.validate_against_graph(graph)
        missing = [claim_id for claim_id in claim_ids if claim_id not in self.claims]
        if missing:
            raise SymbolicBridgeError(
                f"no symbolic mapping for claims {sorted(missing)}"
            )
        program = {
            "sorts": list(self.sorts),
            "functions": {
                name: {"args": decl["args"], "returns": decl["returns"]}
                for name, decl in self.functions.items()
            },
            "constraints": [
                self.claims[claim_id].formula for claim_id in claim_ids
            ]
            + list(extra_constraints or []),
            "query": query,
        }
        try:
            return parse_program(program)
        except SymbolicSchemaError as exc:
            raise SymbolicBridgeError(
                f"symbolic claim program failed type/schema checking: {exc}"
            ) from exc


def _direct_claim_catalog_symbols(
    claim: dict[str, Any], known_catalog_symbols: set[str]
) -> frozenset[str]:
    """Return canonical snake_case vocabulary explicitly named by the claim text.

    This is intentionally lexical and conservative: it does not try to understand prose.
    Its purpose is to prevent an explicitly named canonical symbol such as
    document_current_epoch from being anonymized into an unanchored helper predicate during
    symbolic lowering.
    """

    text_parts = [claim.get("statement"), claim.get("formal_intent")]
    tokens: set[str] = set()
    for value in text_parts:
        if isinstance(value, str):
            tokens.update(re.findall(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", value))
    return frozenset(tokens & known_catalog_symbols)



def _expected_contract_symbols(contract_id: str, contract: dict[str, Any]) -> frozenset[str]:
    contract_class = contract.get("class")
    if contract_class == "state_invariant":
        symbols = contract.get("state_symbols")
        if not isinstance(symbols, list) or not all(isinstance(x, str) for x in symbols):
            raise SymbolicBridgeError(
                f"{contract_id} state_invariant lacks valid state_symbols"
            )
        return frozenset(symbols)
    if contract_class == "provenance_binding":
        source = contract.get("source_symbols")
        bound = contract.get("bound_symbols")
        if (
            not isinstance(source, list)
            or not all(isinstance(x, str) for x in source)
            or not isinstance(bound, list)
            or not all(isinstance(x, str) for x in bound)
        ):
            raise SymbolicBridgeError(
                f"{contract_id} provenance_binding lacks valid source/bound symbols"
            )
        return frozenset([*source, *bound])
    if contract_class == "safety_contract":
        # safety_contract is intentionally open over typed predicates rather than a
        # canonical state/source symbol list. Its symbolic helper functions must be
        # semantically anchored to this contract (or canonical vocabulary), which is
        # validated independently above.
        return frozenset()
    raise SymbolicBridgeError(
        f"{contract_id} class {contract_class!r} is not supported by the symbolic bridge"
    )


def _formula_function_names(node: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(node, dict):
        if set(node) == {"call"} and isinstance(node["call"], dict):
            fn = node["call"].get("fn")
            if isinstance(fn, str):
                result.add(fn)
        for value in node.values():
            result.update(_formula_function_names(value))
    elif isinstance(node, list):
        for value in node:
            result.update(_formula_function_names(value))
    return result
