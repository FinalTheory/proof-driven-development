from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import yaml

from ..model import Graph, UniqueKeyLoader, _catalog_namespace
from .assurance_registry import SubjectTrustStatus, TranslationAssuranceRegistry
from .bridge import SymbolicBridge, SymbolicBridgeError, _formula_function_names
from .dsl import ParsedProgram, parse_program
from .translation_assurance import (
    TranslationReviewError,
    build_obligation_translation_subject,
)
from .z3_backend import CheckResult, check_program


DESIGN_OBLIGATION_SEMANTICS_VERSION = "1"


class DesignObligationError(ValueError):
    """Raised when a design-obligation sidecar or check is malformed/unsafe."""


@dataclass(frozen=True)
class DesignObligation:
    obligation_id: str
    description: str
    statement: str
    source_refs: tuple[str, ...]
    coverage_scope: str
    claim_ids: tuple[str, ...]
    assumption_ids: tuple[str, ...]
    contract_ids: tuple[str, ...]
    formula: dict[str, Any]


@dataclass(frozen=True)
class DesignObligationSpec:
    version: int
    obligations: dict[str, DesignObligation]

    @classmethod
    def load(cls, path: Path) -> "DesignObligationSpec":
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        except OSError as exc:
            raise DesignObligationError(
                f"design-obligation model unavailable at {path}: {exc}"
            ) from exc
        except yaml.YAMLError as exc:
            raise DesignObligationError(f"invalid design-obligation YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise DesignObligationError("design-obligation model must be a mapping")
        return cls.from_data(raw)

    @classmethod
    def from_data(cls, raw: dict[str, Any]) -> "DesignObligationSpec":
        if set(raw) != {"version", "obligations"}:
            raise DesignObligationError(
                "design-obligation model must contain exactly version and obligations"
            )
        if raw.get("version") != 1:
            raise DesignObligationError("design-obligation model version must be 1")
        raw_obligations = raw.get("obligations")
        if not isinstance(raw_obligations, dict):
            raise DesignObligationError("obligations must be a mapping")

        obligations: dict[str, DesignObligation] = {}
        expected_fields = {
            "description",
            "statement",
            "source_refs",
            "coverage_scope",
            "claims",
            "assumptions",
            "contracts",
            "formula",
        }
        for obligation_id, raw_obligation in raw_obligations.items():
            if (
                not isinstance(obligation_id, str)
                or not obligation_id
                or not isinstance(raw_obligation, dict)
                or set(raw_obligation) != expected_fields
            ):
                raise DesignObligationError(
                    f"obligation {obligation_id!r} must contain exactly {sorted(expected_fields)}"
                )
            description = raw_obligation["description"]
            statement = raw_obligation["statement"]
            source_refs = raw_obligation["source_refs"]
            coverage_scope = raw_obligation["coverage_scope"]
            claims = raw_obligation["claims"]
            assumptions = raw_obligation["assumptions"]
            contracts = raw_obligation["contracts"]
            formula = raw_obligation["formula"]

            for field_name, value in (("description", description), ("statement", statement)):
                if not isinstance(value, str) or not value.strip():
                    raise DesignObligationError(
                        f"obligation {obligation_id}.{field_name} must be non-empty"
                    )
            for field_name, values in (
                ("source_refs", source_refs),
                ("claims", claims),
                ("assumptions", assumptions),
                ("contracts", contracts),
            ):
                if (
                    not isinstance(values, list)
                    or not all(isinstance(item, str) and item for item in values)
                    or len(values) != len(set(values))
                ):
                    raise DesignObligationError(
                        f"obligation {obligation_id}.{field_name} must be a unique string list"
                    )
            if not source_refs:
                raise DesignObligationError(
                    f"obligation {obligation_id}.source_refs must be non-empty"
                )
            if coverage_scope not in {"selected_constraints", "all_roots"}:
                raise DesignObligationError(
                    f"obligation {obligation_id}.coverage_scope must be selected_constraints or all_roots"
                )
            if coverage_scope == "selected_constraints" and not claims and not assumptions and not contracts:
                raise DesignObligationError(
                    f"obligation {obligation_id} selected_constraints requires claims or contracts"
                )
            if coverage_scope == "all_roots" and (claims or assumptions or contracts):
                raise DesignObligationError(
                    f"obligation {obligation_id} all_roots must not manually list claims/contracts"
                )
            if not isinstance(formula, dict):
                raise DesignObligationError(
                    f"obligation {obligation_id}.formula must be a symbolic expression mapping"
                )

            obligations[obligation_id] = DesignObligation(
                obligation_id=obligation_id,
                description=description.strip(),
                statement=statement.strip(),
                source_refs=tuple(source_refs),
                coverage_scope=coverage_scope,
                claim_ids=tuple(claims),
                assumption_ids=tuple(assumptions),
                contract_ids=tuple(contracts),
                formula=formula,
            )
        return cls(version=1, obligations=obligations)

    def require(self, obligation_id: str) -> DesignObligation:
        try:
            return self.obligations[obligation_id]
        except KeyError as exc:
            raise DesignObligationError(
                f"unknown design obligation {obligation_id!r}"
            ) from exc


@dataclass(frozen=True)
class DesignObligationResult:
    obligation_id: str
    obligation_signature: str
    coverage_signature: str
    coverage_scope: str
    claim_ids: tuple[str, ...]
    assumption_ids: tuple[str, ...]
    contract_ids: tuple[str, ...]
    baseline: CheckResult
    constrained: CheckResult
    constraint_trust: dict[str, str]
    trusted_constraints_required: bool
    oracle_trust: str

    @property
    def verdict(self) -> str:
        if self.baseline.status == "unsat":
            return "VACUOUS_OBLIGATION"
        if self.baseline.status != "sat":
            return "UNKNOWN"
        if self.constrained.status == "unsat":
            return "COVERED"
        if self.constrained.status == "sat":
            return "GAP"
        return "UNKNOWN"

    @property
    def all_constraints_trusted(self) -> bool:
        return all(status == SubjectTrustStatus.TRUSTED.value for status in self.constraint_trust.values())

    @property
    def solver_checked(self) -> bool:
        return self.verdict in {"COVERED", "GAP"}

    @property
    def canonical_certified(self) -> bool:
        return (
            self.coverage_scope == "all_roots"
            and self.solver_checked
            and self.all_constraints_trusted
            and self.oracle_trust == SubjectTrustStatus.TRUSTED.value
        )


class DesignObligationVerifier:
    """Check whether selected current-spec formulas entail an independent design property."""

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
        obligation: DesignObligation,
        *,
        require_trusted: bool = True,
    ) -> DesignObligationResult:
        claim_ids, assumption_ids, contract_ids = self._resolve_constraints(obligation)
        self._validate_obligation(obligation, claim_ids, assumption_ids, contract_ids)
        trust = self._constraint_trust(claim_ids, assumption_ids, contract_ids)
        obligation_subject = build_obligation_translation_subject(
            self.graph,
            self.bridge,
            obligation_id=obligation.obligation_id,
            statement=obligation.statement,
            description=obligation.description,
            source_refs=obligation.source_refs,
            formula=obligation.formula,
        )
        oracle_trust = (
            self.assurance_registry.evaluate_subject(
                kind="obligation",
                subject_id=obligation.obligation_id,
                subject=obligation_subject,
            ).status.value
            if self.assurance_registry is not None
            else SubjectTrustStatus.UNVERIFIED.value
        )
        if require_trusted:
            failures = [f"{key}={status}" for key, status in trust.items() if status != SubjectTrustStatus.TRUSTED.value]
            if failures:
                raise TranslationReviewError(
                    "design-obligation check requires TRUSTED selected constraints: "
                    + "; ".join(failures)
                )
            if oracle_trust != SubjectTrustStatus.TRUSTED.value:
                raise TranslationReviewError(
                    f"design obligation {obligation.obligation_id} oracle translation is not TRUSTED: {oracle_trust}"
                )

        negated_obligation = {"not": obligation.formula}
        baseline_program = self._compile(negated_obligation, constraints=[])
        selected_formulas = (
            [self.bridge.contracts[cid].formula for cid in contract_ids]
            + [self.bridge.assumptions[aid].formula for aid in assumption_ids]
            + [self.bridge.claims[cid].formula for cid in claim_ids]
        )
        constrained_program = self._compile(
            negated_obligation,
            constraints=selected_formulas,
        )
        baseline = check_program(baseline_program)
        constrained = check_program(constrained_program)
        obligation_sig = design_obligation_signature(self.graph, self.bridge, obligation)
        coverage_sig = design_obligation_coverage_signature(
            self.graph,
            self.bridge,
            obligation,
            claim_ids=claim_ids,
            assumption_ids=assumption_ids,
            contract_ids=contract_ids,
            obligation_signature=obligation_sig,
        )
        return DesignObligationResult(
            obligation_id=obligation.obligation_id,
            obligation_signature=obligation_sig,
            coverage_signature=coverage_sig,
            coverage_scope=obligation.coverage_scope,
            claim_ids=tuple(claim_ids),
            assumption_ids=tuple(assumption_ids),
            contract_ids=tuple(contract_ids),
            baseline=baseline,
            constrained=constrained,
            constraint_trust=trust,
            trusted_constraints_required=require_trusted,
            oracle_trust=oracle_trust,
        )

    def check_with_overrides(
        self,
        obligation: DesignObligation,
        *,
        claim_formula_overrides: dict[str, dict[str, Any]] | None = None,
        contract_formula_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> DesignObligationResult:
        """Adversarial regression path: replay a weakened current specification."""
        claim_ids, assumption_ids, contract_ids = self._resolve_constraints(obligation)
        self._validate_obligation(obligation, claim_ids, assumption_ids, contract_ids)
        claim_overrides = claim_formula_overrides or {}
        contract_overrides = contract_formula_overrides or {}
        unknown_claims = set(claim_overrides) - set(claim_ids)
        unknown_contracts = set(contract_overrides) - set(contract_ids)
        if unknown_claims or unknown_contracts:
            raise DesignObligationError(
                "obligation overrides must target selected constraints: "
                f"claims={sorted(unknown_claims)} contracts={sorted(unknown_contracts)}"
            )
        negated_obligation = {"not": obligation.formula}
        baseline = check_program(self._compile(negated_obligation, constraints=[]))
        selected_formulas = (
            [
                contract_overrides.get(cid, self.bridge.contracts[cid].formula)
                for cid in contract_ids
            ]
            + [self.bridge.assumptions[aid].formula for aid in assumption_ids]
            + [
                claim_overrides.get(cid, self.bridge.claims[cid].formula)
                for cid in claim_ids
            ]
        )
        constrained = check_program(
            self._compile(negated_obligation, constraints=selected_formulas)
        )
        obligation_sig = design_obligation_signature(self.graph, self.bridge, obligation)
        coverage_sig = design_obligation_coverage_signature(
            self.graph,
            self.bridge,
            obligation,
            claim_ids=claim_ids,
            assumption_ids=assumption_ids,
            contract_ids=contract_ids,
            obligation_signature=obligation_sig,
        )
        return DesignObligationResult(
            obligation_id=obligation.obligation_id,
            obligation_signature=obligation_sig,
            coverage_signature=coverage_sig,
            coverage_scope=obligation.coverage_scope,
            claim_ids=tuple(claim_ids),
            assumption_ids=tuple(assumption_ids),
            contract_ids=tuple(contract_ids),
            baseline=baseline,
            constrained=constrained,
            constraint_trust=self._constraint_trust(claim_ids, assumption_ids, contract_ids),
            trusted_constraints_required=False,
            oracle_trust=(
                self.assurance_registry.evaluate_subject(
                    kind="obligation",
                    subject_id=obligation.obligation_id,
                    subject=build_obligation_translation_subject(
                        self.graph, self.bridge, obligation_id=obligation.obligation_id,
                        statement=obligation.statement, description=obligation.description,
                        source_refs=obligation.source_refs, formula=obligation.formula
                    ),
                ).status.value
                if self.assurance_registry is not None
                else SubjectTrustStatus.UNVERIFIED.value
            ),
        )

    def _resolve_constraints(
        self, obligation: DesignObligation
    ) -> tuple[list[str], list[str], list[str]]:
        if obligation.coverage_scope == "selected_constraints":
            return (
                list(obligation.claim_ids),
                list(obligation.assumption_ids),
                list(obligation.contract_ids),
            )
        roots = sorted(
            node_id
            for node_id, claim in self.graph.claims.items()
            if claim.get("kind") == "root"
        )
        assumptions = sorted(self.graph.assumptions)
        missing_roots = [node_id for node_id in roots if node_id not in self.bridge.claims]
        missing_assumptions = [
            node_id for node_id in assumptions if node_id not in self.bridge.assumptions
        ]
        if missing_roots or missing_assumptions:
            raise DesignObligationError(
                "all_roots coverage requires every current root and assumption to have a symbolic mapping; "
                f"missing_roots={missing_roots} missing_assumptions={missing_assumptions}"
            )
        return roots, assumptions, []

    def _validate_obligation(
        self,
        obligation: DesignObligation,
        claim_ids: list[str],
        assumption_ids: list[str],
        contract_ids: list[str],
    ) -> None:
        unknown_claims = sorted(set(claim_ids) - set(self.bridge.claims))
        unknown_assumptions = sorted(set(assumption_ids) - set(self.bridge.assumptions))
        unknown_contracts = sorted(set(contract_ids) - set(self.bridge.contracts))
        if unknown_claims or unknown_assumptions or unknown_contracts:
            raise DesignObligationError(
                f"obligation {obligation.obligation_id} references unmapped constraints: "
                f"claims={unknown_claims} assumptions={unknown_assumptions} contracts={unknown_contracts}"
            )
        known_sources = _catalog_namespace(self.graph, "sources")
        unknown_sources = sorted(set(obligation.source_refs) - set(known_sources))
        if unknown_sources:
            raise DesignObligationError(
                f"obligation {obligation.obligation_id} references unknown source_refs {unknown_sources}"
            )

        used_functions = _formula_function_names(obligation.formula)
        unknown_functions = sorted(used_functions - set(self.bridge.functions))
        if unknown_functions:
            raise DesignObligationError(
                f"obligation {obligation.obligation_id} uses unknown functions {unknown_functions}"
            )
        selected_anchors = (
            {f"claim:{cid}" for cid in claim_ids}
            | {f"assumption:{aid}" for aid in assumption_ids}
            | {f"contract:{cid}" for cid in contract_ids}
        )
        for fn in sorted(used_functions):
            decl = self.bridge.functions[fn]
            if decl.get("catalog_symbols"):
                continue
            anchors = set(decl.get("semantic_anchors", []))
            source_anchors = {
                anchor.split(":", 1)[1]
                for anchor in anchors
                if anchor.startswith("source:")
            }
            if source_anchors.intersection(obligation.source_refs):
                continue
            if anchors.intersection(selected_anchors):
                continue
            raise DesignObligationError(
                f"obligation {obligation.obligation_id} uses helper function {fn} without "
                "canonical catalog symbols, a matching source:<source_ref>, or a selected "
                "claim/contract semantic anchor"
            )

        # Type-check the obligation formula independently of selected constraints.
        self._compile({"bool": True}, constraints=[obligation.formula])

    def _constraint_trust(
        self,
        claim_ids: list[str],
        assumption_ids: list[str],
        contract_ids: list[str],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.assurance_registry is None:
            for cid in contract_ids:
                result[f"contract:{cid}"] = SubjectTrustStatus.UNVERIFIED.value
            for aid in assumption_ids:
                result[f"assumption:{aid}"] = SubjectTrustStatus.UNVERIFIED.value
            for cid in claim_ids:
                result[f"claim:{cid}"] = SubjectTrustStatus.UNVERIFIED.value
            return result
        for cid in contract_ids:
            trust = self.assurance_registry.evaluate(
                self.graph, self.bridge, kind="contract", subject_id=cid
            )
            result[f"contract:{cid}"] = trust.status.value
        for aid in assumption_ids:
            trust = self.assurance_registry.evaluate(
                self.graph, self.bridge, kind="assumption", subject_id=aid
            )
            result[f"assumption:{aid}"] = trust.status.value
        for cid in claim_ids:
            trust = self.assurance_registry.evaluate(
                self.graph, self.bridge, kind="claim", subject_id=cid
            )
            result[f"claim:{cid}"] = trust.status.value
        return result

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
        except Exception as exc:
            raise DesignObligationError(
                f"failed to compile design obligation: {exc}"
            ) from exc


def design_obligation_signature(
    graph: Graph,
    bridge: SymbolicBridge,
    obligation: DesignObligation,
) -> str:
    """Hash the exact design oracle, source sections, and symbolic vocabulary it uses."""
    source_sections = _resolve_source_sections(graph, obligation.source_refs)
    used_functions = sorted(_formula_function_names(obligation.formula))
    payload = {
        "semantics_version": DESIGN_OBLIGATION_SEMANTICS_VERSION,
        "obligation_id": obligation.obligation_id,
        "description": obligation.description,
        "statement": obligation.statement,
        "source_refs": list(obligation.source_refs),
        "source_sections": source_sections,
        "formula": obligation.formula,
        "functions": {name: bridge.functions[name] for name in used_functions},
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def design_obligation_coverage_signature(
    graph: Graph,
    bridge: SymbolicBridge,
    obligation: DesignObligation,
    *,
    claim_ids: list[str],
    assumption_ids: list[str],
    contract_ids: list[str],
    obligation_signature: str,
) -> str:
    payload = {
        "semantics_version": DESIGN_OBLIGATION_SEMANTICS_VERSION,
        "obligation_signature": obligation_signature,
        "coverage_scope": obligation.coverage_scope,
        "claims": {
            cid: bridge.claims[cid].formula for cid in sorted(claim_ids)
        },
        "assumptions": {
            aid: bridge.assumptions[aid].formula for aid in sorted(assumption_ids)
        },
        "contracts": {
            cid: bridge.contracts[cid].formula for cid in sorted(contract_ids)
        },
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _resolve_source_sections(graph: Graph, source_refs: tuple[str, ...]) -> dict[str, str]:
    if graph.repository_root is None:
        return {source_ref: "<repository context unavailable>" for source_ref in source_refs}
    source = graph.doc.get("source", {})
    article_ref = source.get("article") if isinstance(source, dict) else None
    if not isinstance(article_ref, str) or not article_ref:
        raise DesignObligationError("canonical source.article is unavailable")
    article_path = graph.repository_root / article_ref
    try:
        text = article_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DesignObligationError(f"cannot read canonical design source {article_ref}: {exc}") from exc

    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    by_heading: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        raw_heading = match.group(1).strip()
        heading = re.sub(r"^[0-9]+\.\s+", "", raw_heading).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        by_heading.setdefault(heading, []).append(text[match.start():end])

    sources = _catalog_namespace(graph, "sources")
    result: dict[str, str] = {}
    for source_ref in source_refs:
        entry = sources.get(source_ref)
        if not isinstance(entry, dict) or not isinstance(entry.get("heading"), str):
            raise DesignObligationError(f"unknown/invalid canonical source_ref {source_ref}")
        section_matches = by_heading.get(entry["heading"], [])
        if len(section_matches) != 1:
            raise DesignObligationError(
                f"canonical source_ref {source_ref} resolves to {len(section_matches)} sections"
            )
        result[source_ref] = section_matches[0]
    return result
