from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..model import Graph
from .assurance_registry import TranslationAssuranceRegistry
from .bridge import SymbolicBridge
from .translation_assurance import TranslationReviewError
from .z3_backend import CheckResult, check_program


@dataclass(frozen=True)
class ExclusionCheck:
    """Compare whether a candidate bad state is admitted before and after selected contracts."""

    baseline: CheckResult
    constrained: CheckResult

    @property
    def closes_counterexample(self) -> bool:
        return self.baseline.status == "sat" and self.constrained.status == "unsat"


class SymbolicVerifier:
    """Stable high-level verification facade over bridge compilation and the SMT backend."""

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

    def _require_trusted(self, *, kind: str, subject_ids: Iterable[str]) -> None:
        if self.assurance_registry is None:
            raise TranslationReviewError(
                "trusted symbolic verification requires a translation-assurance registry"
            )
        for subject_id in subject_ids:
            self.assurance_registry.require_trusted(
                self.graph,
                self.bridge,
                kind=kind,
                subject_id=subject_id,
            )

    def check(
        self,
        query: dict[str, Any],
        *,
        contracts: Iterable[str] = (),
        extra_constraints: Iterable[dict[str, Any]] = (),
    ) -> CheckResult:
        program = self.bridge.compile_program(
            self.graph,
            contract_ids=list(contracts),
            query=query,
            extra_constraints=list(extra_constraints),
        )
        return check_program(program)

    def trusted_check(
        self,
        query: dict[str, Any],
        *,
        contracts: Iterable[str],
        extra_constraints: Iterable[dict[str, Any]] = (),
    ) -> CheckResult:
        selected = list(contracts)
        self._require_trusted(kind="contract", subject_ids=selected)
        return self.check(
            query,
            contracts=selected,
            extra_constraints=extra_constraints,
        )

    def check_claims(
        self,
        query: dict[str, Any],
        *,
        claims: Iterable[str] = (),
        extra_constraints: Iterable[dict[str, Any]] = (),
    ) -> CheckResult:
        program = self.bridge.compile_claim_program(
            self.graph,
            claim_ids=list(claims),
            query=query,
            extra_constraints=list(extra_constraints),
        )
        return check_program(program)

    def trusted_check_claims(
        self,
        query: dict[str, Any],
        *,
        claims: Iterable[str],
        extra_constraints: Iterable[dict[str, Any]] = (),
    ) -> CheckResult:
        selected = list(claims)
        self._require_trusted(kind="claim", subject_ids=selected)
        return self.check_claims(
            query,
            claims=selected,
            extra_constraints=extra_constraints,
        )

    def claim_exclusion_check(
        self,
        query: dict[str, Any],
        *,
        claims: Iterable[str],
    ) -> ExclusionCheck:
        selected = list(claims)
        return ExclusionCheck(
            baseline=self.check_claims(query),
            constrained=self.check_claims(query, claims=selected),
        )

    def trusted_claim_exclusion_check(
        self,
        query: dict[str, Any],
        *,
        claims: Iterable[str],
    ) -> ExclusionCheck:
        selected = list(claims)
        self._require_trusted(kind="claim", subject_ids=selected)
        return ExclusionCheck(
            baseline=self.check_claims(query),
            constrained=self.check_claims(query, claims=selected),
        )

    def trusted_exclusion_check(
        self,
        query: dict[str, Any],
        *,
        contracts: Iterable[str],
    ) -> ExclusionCheck:
        selected = list(contracts)
        self._require_trusted(kind="contract", subject_ids=selected)
        return ExclusionCheck(
            baseline=self.check(query),
            constrained=self.check(query, contracts=selected),
        )

    def exclusion_check(
        self,
        query: dict[str, Any],
        *,
        contracts: Iterable[str],
    ) -> ExclusionCheck:
        selected = list(contracts)
        return ExclusionCheck(
            baseline=self.check(query),
            constrained=self.check(query, contracts=selected),
        )
