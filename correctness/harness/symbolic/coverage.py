from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from ..model import Graph, UniqueKeyLoader
from .assurance_registry import SubjectTrustStatus, TranslationAssuranceRegistry
from .bridge import SymbolicBridge, SymbolicBridgeError, _formula_function_names
from .dsl import ParsedProgram, SymbolicSchemaError, parse_program
from .translation_assurance import (
    TranslationReviewError,
    build_claim_translation_subject,
    build_translation_subject,
    translation_subject_signature,
)
from .z3_backend import CheckResult, check_program

try:
    import z3  # type: ignore
except ImportError:  # pragma: no cover - environment-dependent
    z3 = None


COVERAGE_SEMANTICS_VERSION = "1"


class SymbolicCoverageError(ValueError):
    """Raised when a declarative symbolic coverage case is malformed or unsafe."""


@dataclass(frozen=True)
class CoverageCase:
    case_id: str
    description: str
    candidate_statement: str
    trusted_claim_ids: tuple[str, ...]
    trusted_contract_ids: tuple[str, ...]
    formula: dict[str, Any]


@dataclass(frozen=True)
class CoverageSpec:
    version: int
    cases: dict[str, CoverageCase]

    @classmethod
    def load(cls, path: Path) -> "CoverageSpec":
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        except OSError as exc:
            raise SymbolicCoverageError(
                f"symbolic coverage model unavailable at {path}: {exc}"
            ) from exc
        except yaml.YAMLError as exc:
            raise SymbolicCoverageError(f"invalid symbolic coverage YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise SymbolicCoverageError("symbolic coverage model must be a mapping")
        return cls.from_data(raw)

    @classmethod
    def from_data(cls, raw: dict[str, Any]) -> "CoverageSpec":
        if set(raw) != {"version", "cases"}:
            raise SymbolicCoverageError(
                "symbolic coverage model must contain exactly version and cases"
            )
        if raw.get("version") != 1:
            raise SymbolicCoverageError("symbolic coverage model version must be 1")
        raw_cases = raw.get("cases")
        if not isinstance(raw_cases, dict):
            raise SymbolicCoverageError("symbolic coverage cases must be a mapping")

        cases: dict[str, CoverageCase] = {}
        expected_fields = {
            "description",
            "candidate_statement",
            "trusted_claims",
            "trusted_contracts",
            "formula",
        }
        for case_id, raw_case in raw_cases.items():
            if not isinstance(case_id, str) or not case_id:
                raise SymbolicCoverageError("coverage case IDs must be non-empty strings")
            if not isinstance(raw_case, dict) or set(raw_case) != expected_fields:
                raise SymbolicCoverageError(
                    f"coverage case {case_id} must contain exactly "
                    f"{sorted(expected_fields)}"
                )

            description = raw_case["description"]
            candidate_statement = raw_case["candidate_statement"]
            trusted_claims = raw_case["trusted_claims"]
            trusted_contracts = raw_case["trusted_contracts"]
            formula = raw_case["formula"]
            if not isinstance(description, str) or not description.strip():
                raise SymbolicCoverageError(
                    f"coverage case {case_id}.description must be non-empty"
                )
            if not isinstance(candidate_statement, str) or not candidate_statement.strip():
                raise SymbolicCoverageError(
                    f"coverage case {case_id}.candidate_statement must be non-empty"
                )
            for field_name, values in (
                ("trusted_claims", trusted_claims),
                ("trusted_contracts", trusted_contracts),
            ):
                if (
                    not isinstance(values, list)
                    or not all(isinstance(item, str) and item for item in values)
                    or len(values) != len(set(values))
                ):
                    raise SymbolicCoverageError(
                        f"coverage case {case_id}.{field_name} must be a unique string list"
                    )
            if not trusted_claims and not trusted_contracts:
                raise SymbolicCoverageError(
                    f"coverage case {case_id} must select at least one trusted constraint"
                )
            if not isinstance(formula, dict):
                raise SymbolicCoverageError(
                    f"coverage case {case_id}.formula must be an expression mapping"
                )
            cases[case_id] = CoverageCase(
                case_id=case_id,
                description=description.strip(),
                candidate_statement=candidate_statement.strip(),
                trusted_claim_ids=tuple(trusted_claims),
                trusted_contract_ids=tuple(trusted_contracts),
                formula=formula,
            )

        return cls(version=1, cases=cases)

    def require_case(self, case_id: str) -> CoverageCase:
        try:
            return self.cases[case_id]
        except KeyError as exc:
            raise SymbolicCoverageError(
                f"unknown symbolic coverage case {case_id!r}"
            ) from exc


@dataclass(frozen=True)
class CoverageResult:
    case_id: str
    candidate_signature: str
    coverage_signature: str
    trusted_claim_ids: tuple[str, ...]
    trusted_contract_ids: tuple[str, ...]
    baseline: CheckResult
    constrained: CheckResult

    @property
    def closes_counterexample(self) -> bool:
        return self.baseline.status == "sat" and self.constrained.status == "unsat"

    @property
    def verdict(self) -> str:
        if self.baseline.status == "unsat":
            return "VACUOUS_CANDIDATE"
        if self.baseline.status != "sat":
            return "UNKNOWN"
        if self.constrained.status == "unsat":
            return "EXCLUDED"
        if self.constrained.status == "sat":
            return "ADMITTED"
        return "UNKNOWN"

    @property
    def machine_checked(self) -> bool:
        return self.verdict == "EXCLUDED"


class SymbolicCoverageVerifier:
    """Run declarative bad-state exclusion checks against trusted symbolic mappings."""

    def __init__(
        self,
        graph: Graph,
        bridge: SymbolicBridge,
        assurance_registry: TranslationAssuranceRegistry | None = None,
    ):
        bridge.validate_against_graph(graph)
        self.graph = graph
        self.bridge = bridge
        self.assurance_registry = assurance_registry

    def check(
        self,
        case: CoverageCase,
        *,
        require_trusted: bool = True,
    ) -> CoverageResult:
        return self._check(
            case,
            require_trusted=require_trusted,
            claim_formula_overrides={},
            contract_formula_overrides={},
        )

    def check_with_overrides(
        self,
        case: CoverageCase,
        *,
        claim_formula_overrides: dict[str, dict[str, Any]] | None = None,
        contract_formula_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> CoverageResult:
        """Adversarial/test-only path that bypasses trust for overridden formulas."""

        return self._check(
            case,
            require_trusted=False,
            claim_formula_overrides=claim_formula_overrides or {},
            contract_formula_overrides=contract_formula_overrides or {},
        )

    def _check(
        self,
        case: CoverageCase,
        *,
        require_trusted: bool,
        claim_formula_overrides: dict[str, dict[str, Any]],
        contract_formula_overrides: dict[str, dict[str, Any]],
    ) -> CoverageResult:
        self._validate_case_subjects(case)

        unknown_claim_overrides = set(claim_formula_overrides) - set(case.trusted_claim_ids)
        unknown_contract_overrides = set(contract_formula_overrides) - set(
            case.trusted_contract_ids
        )
        if unknown_claim_overrides or unknown_contract_overrides:
            raise SymbolicCoverageError(
                "coverage overrides must target selected trusted constraints: "
                f"claims={sorted(unknown_claim_overrides)} "
                f"contracts={sorted(unknown_contract_overrides)}"
            )

        if require_trusted:
            self._require_trusted(case)

        baseline_program = self._compile(case.formula, constraints=[])
        constrained_formulas = [
            contract_formula_overrides.get(
                contract_id, self.bridge.contracts[contract_id].formula
            )
            for contract_id in case.trusted_contract_ids
        ] + [
            claim_formula_overrides.get(claim_id, self.bridge.claims[claim_id].formula)
            for claim_id in case.trusted_claim_ids
        ]
        constrained_program = self._compile(
            case.formula,
            constraints=constrained_formulas,
        )

        candidate_sig = coverage_candidate_signature(case, self.bridge)
        coverage_sig = coverage_semantic_signature(
            self.graph,
            self.bridge,
            case,
            candidate_signature=candidate_sig,
        )
        if claim_formula_overrides or contract_formula_overrides:
            encoded = json.dumps(
                {
                    "base_coverage_signature": coverage_sig,
                    "claim_formula_overrides": claim_formula_overrides,
                    "contract_formula_overrides": contract_formula_overrides,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            coverage_sig = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return CoverageResult(
            case_id=case.case_id,
            candidate_signature=candidate_sig,
            coverage_signature=coverage_sig,
            trusted_claim_ids=case.trusted_claim_ids,
            trusted_contract_ids=case.trusted_contract_ids,
            baseline=check_program(baseline_program),
            constrained=check_program(constrained_program),
        )

    def _compile(
        self,
        query: dict[str, Any],
        *,
        constraints: list[dict[str, Any]],
    ) -> ParsedProgram:
        program = {
            "sorts": list(self.bridge.sorts),
            "functions": {
                name: {"args": decl["args"], "returns": decl["returns"]}
                for name, decl in self.bridge.functions.items()
            },
            "constraints": constraints,
            "query": query,
        }
        try:
            return parse_program(program)
        except SymbolicSchemaError as exc:
            raise SymbolicCoverageError(
                f"failed to compile symbolic coverage query: {exc}"
            ) from exc

    def _validate_case_subjects(self, case: CoverageCase) -> None:
        missing_claims = sorted(set(case.trusted_claim_ids) - set(self.bridge.claims))
        missing_contracts = sorted(
            set(case.trusted_contract_ids) - set(self.bridge.contracts)
        )
        if missing_claims or missing_contracts:
            raise SymbolicCoverageError(
                "coverage case references unmapped trusted constraints: "
                f"claims={missing_claims} contracts={missing_contracts}"
            )
        try:
            self.bridge.validate_formula_anchors(
                case.formula,
                claim_ids=case.trusted_claim_ids,
                contract_ids=case.trusted_contract_ids,
                label=f"coverage case {case.case_id}",
            )
        except SymbolicBridgeError as exc:
            raise SymbolicCoverageError(str(exc)) from exc
        try:
            self._compile(case.formula, constraints=[])
        except SymbolicCoverageError:
            raise

    def _require_trusted(self, case: CoverageCase) -> None:
        if self.assurance_registry is None:
            raise TranslationReviewError(
                "trusted symbolic coverage requires a translation-assurance registry"
            )
        failures: list[str] = []
        for kind, subject_ids in (
            ("contract", case.trusted_contract_ids),
            ("claim", case.trusted_claim_ids),
        ):
            for subject_id in subject_ids:
                result = self.assurance_registry.evaluate(
                    self.graph,
                    self.bridge,
                    kind=kind,
                    subject_id=subject_id,
                )
                if result.status != SubjectTrustStatus.TRUSTED:
                    failures.append(
                        f"{kind}:{subject_id}={result.status.value} ({result.reason})"
                    )
        if failures:
            raise TranslationReviewError(
                "symbolic coverage requires TRUSTED constraints: " + "; ".join(failures)
            )


def coverage_candidate_signature(case: CoverageCase, bridge: SymbolicBridge) -> str:
    function_names = sorted(_formula_function_names(case.formula))
    missing = sorted(set(function_names) - set(bridge.functions))
    if missing:
        raise SymbolicCoverageError(
            f"coverage case {case.case_id} uses unknown functions {missing}"
        )
    payload = {
        "coverage_semantics_version": COVERAGE_SEMANTICS_VERSION,
        "case_id": case.case_id,
        "description": case.description,
        "candidate_statement": case.candidate_statement,
        "formula": case.formula,
        "vocabulary": {
            name: {
                key: value
                for key, value in bridge.functions[name].items()
                if key != "semantic_anchors"
            }
            for name in function_names
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def coverage_semantic_signature(
    graph: Graph,
    bridge: SymbolicBridge,
    case: CoverageCase,
    *,
    candidate_signature: str | None = None,
) -> str:
    candidate_sig = candidate_signature or coverage_candidate_signature(case, bridge)
    trusted_claims = {
        claim_id: translation_subject_signature(
            build_claim_translation_subject(graph, bridge, claim_id)
        )
        for claim_id in case.trusted_claim_ids
    }
    trusted_contracts = {
        contract_id: translation_subject_signature(
            build_translation_subject(graph, bridge, contract_id)
        )
        for contract_id in case.trusted_contract_ids
    }
    payload = {
        "coverage_semantics_version": COVERAGE_SEMANTICS_VERSION,
        "candidate_signature": candidate_sig,
        "trusted_claims": trusted_claims,
        "trusted_contracts": trusted_contracts,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_coverage_artifact(
    graph: Graph,
    bridge: SymbolicBridge,
    registry: TranslationAssuranceRegistry,
    case: CoverageCase,
    result: CoverageResult,
) -> dict[str, Any]:
    if result.case_id != case.case_id:
        raise SymbolicCoverageError("coverage result does not match requested case")
    if result.trusted_claim_ids != case.trusted_claim_ids:
        raise SymbolicCoverageError("coverage result trusted claims do not match requested case")
    if result.trusted_contract_ids != case.trusted_contract_ids:
        raise SymbolicCoverageError(
            "coverage result trusted contracts do not match requested case"
        )
    expected_signature = coverage_semantic_signature(
        graph,
        bridge,
        case,
        candidate_signature=result.candidate_signature,
    )
    if result.coverage_signature != expected_signature:
        raise SymbolicCoverageError(
            "coverage result was produced from stale or overridden constraints"
        )

    claim_entries: list[dict[str, Any]] = []
    for claim_id in case.trusted_claim_ids:
        node = graph.require_node(claim_id)
        trust = registry.evaluate(
            graph,
            bridge,
            kind="claim",
            subject_id=claim_id,
        )
        subject = build_claim_translation_subject(graph, bridge, claim_id)
        claim_entries.append(
            {
                "id": claim_id,
                "statement": node.data.get("statement", ""),
                "formula": bridge.claims[claim_id].formula,
                "translation_assurance": {
                    "status": trust.status.value,
                    "subject_signature": translation_subject_signature(subject),
                },
            }
        )

    contract_entries: list[dict[str, Any]] = []
    for contract_id in case.trusted_contract_ids:
        trust = registry.evaluate(
            graph,
            bridge,
            kind="contract",
            subject_id=contract_id,
        )
        subject = build_translation_subject(graph, bridge, contract_id)
        contract_entries.append(
            {
                "id": contract_id,
                "formula": bridge.contracts[contract_id].formula,
                "translation_assurance": {
                    "status": trust.status.value,
                    "subject_signature": translation_subject_signature(subject),
                },
            }
        )

    candidate_functions = sorted(_formula_function_names(case.formula))
    vocabulary = {
        name: {
            "type": (
                "(" + ", ".join(bridge.functions[name]["args"]) + ") -> "
                + bridge.functions[name]["returns"]
            ),
            "meaning": bridge.functions[name]["meaning"],
        }
        for name in candidate_functions
    }

    solver_version = z3.get_version_string() if z3 is not None else None
    return {
        "version": 1,
        "kind": "symbolic_coverage_machine_check",
        "case": case.case_id,
        "coverage_signature": result.coverage_signature,
        "candidate": {
            "statement": case.candidate_statement,
            "description": case.description,
            "signature": result.candidate_signature,
            "formula": case.formula,
            "vocabulary": vocabulary,
        },
        "trusted_constraints": {
            "claims": claim_entries,
            "contracts": contract_entries,
        },
        "proof": {
            "baseline": {
                "query_semantics": "candidate_bad_state",
                "solver_status": result.baseline.status,
                "witness_model": result.baseline.model,
                "meaning": (
                    "The candidate bad state is logically realizable before the trusted "
                    "constraints are asserted."
                    if result.baseline.status == "sat"
                    else "The candidate bad state is not independently realizable, so an "
                    "UNSAT constrained check would be vacuous."
                ),
            },
            "constrained": {
                "query_semantics": (
                    "AND(all trusted constraint formulas) AND candidate_bad_state"
                ),
                "solver_status": result.constrained.status,
                "counterexample_model": result.constrained.model,
                "meaning": (
                    "No model satisfies both the trusted constraints and the candidate "
                    "bad state."
                    if result.constrained.status == "unsat"
                    else "The trusted constraints still admit the candidate bad state."
                ),
            },
        },
        "solver": {
            "name": "z3",
            "version": solver_version,
        },
        "result": {
            "verdict": result.verdict,
            "machine_checked": result.machine_checked,
            "closes_counterexample": result.closes_counterexample,
            "meaning": _result_meaning(result),
        },
    }


def _result_meaning(result: CoverageResult) -> str:
    if result.verdict == "EXCLUDED":
        return (
            "Baseline SAT establishes that the bad state is non-vacuous; constrained "
            "UNSAT establishes that the selected trusted symbolic constraints exclude it."
        )
    if result.verdict == "ADMITTED":
        return (
            "The candidate is non-vacuous and remains satisfiable under the selected "
            "trusted symbolic constraints."
        )
    if result.verdict == "VACUOUS_CANDIDATE":
        return (
            "The candidate is already UNSAT without trusted constraints and therefore "
            "cannot certify coverage."
        )
    return "The solver could not establish a decisive symbolic coverage result."
