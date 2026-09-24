from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
            if set(decl) != {"args", "returns", "catalog_symbols", "meaning"}:
                raise SymbolicBridgeError(
                    f"function {name} must contain exactly args, returns, catalog_symbols, and meaning"
                )
            args = decl["args"]
            result = decl["returns"]
            symbols = decl["catalog_symbols"]
            meaning = decl["meaning"]
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
            if not isinstance(meaning, str) or not meaning.strip():
                raise SymbolicBridgeError(
                    f"function {name}.meaning must be a non-empty string"
                )
            normalized_functions[name] = {
                "args": list(args),
                "returns": result,
                "catalog_symbols": list(symbols),
                "meaning": meaning.strip(),
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
            for fn in used_functions:
                catalog_symbols.update(normalized_functions[fn]["catalog_symbols"])
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

        for fn_name, decl in self.functions.items():
            unknown = set(decl["catalog_symbols"]) - known_catalog_symbols
            if unknown:
                raise SymbolicBridgeError(
                    f"function {fn_name} maps unknown catalog symbols {sorted(unknown)}"
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
            if mapping.catalog_symbols != expected:
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
            expected_symbols: set[str] = set()
            for contract_id in mapping.contract_ids:
                expected_symbols.update(self.contracts[contract_id].catalog_symbols)
            if mapping.catalog_symbols != frozenset(expected_symbols):
                missing = sorted(expected_symbols - set(mapping.catalog_symbols))
                extra = sorted(set(mapping.catalog_symbols) - expected_symbols)
                raise SymbolicBridgeError(
                    f"{claim_id} symbolic claim catalog coverage mismatch: "
                    f"missing={missing}, extra={extra}"
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
    raise SymbolicBridgeError(
        f"{contract_id} class {contract_class!r} is not supported by symbolic bridge POC"
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
