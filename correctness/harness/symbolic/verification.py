from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..model import Graph
from .bridge import SymbolicBridge
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

    def __init__(self, graph: Graph, bridge: SymbolicBridge):
        bridge.validate_against_graph(graph)
        self.graph = graph
        self.bridge = bridge

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
