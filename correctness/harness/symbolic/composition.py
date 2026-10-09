from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..model import Graph
from ..signatures import _composition_semantic_signature
from .assurance_registry import SubjectTrustStatus, TranslationAssuranceRegistry
from .bridge import SymbolicBridge, SymbolicBridgeError
from .dsl import ParsedProgram, parse_program
from .translation_assurance import TranslationReviewError
from .z3_backend import CheckResult, check_program


class SymbolicCompositionError(ValueError):
    """Raised when a DAG composition cannot be represented safely in the symbolic model."""


@dataclass(frozen=True)
class CompositionResult:
    node_id: str
    premise_ids: tuple[str, ...]
    composition_signature: str
    solver_result: CheckResult

    @property
    def verdict(self) -> str:
        if self.solver_result.status == "unsat":
            return "ENTAILED"
        if self.solver_result.status == "sat":
            return "COUNTEREXAMPLE"
        return "UNKNOWN"

    @property
    def machine_checked(self) -> bool:
        return self.verdict == "ENTAILED"


class SymbolicCompositionVerifier:
    """Machine-check direct-premise composition for fully translated DAG claims."""

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
        node_id: str,
        *,
        require_trusted: bool = True,
    ) -> CompositionResult:
        target, premise_ids = self._composition_subjects(node_id)
        if require_trusted:
            self._require_trusted_claims([node_id, *premise_ids])
        program = self._compile(
            target_formula=target,
            premise_ids=premise_ids,
        )
        return CompositionResult(
            node_id=node_id,
            premise_ids=tuple(premise_ids),
            composition_signature=_composition_semantic_signature(self.graph, node_id),
            solver_result=check_program(program),
        )

    def check_with_premise_overrides(
        self,
        node_id: str,
        *,
        premise_formula_overrides: dict[str, dict[str, Any]],
    ) -> CompositionResult:
        """Test-only/adversarial path.

        Deliberately bypasses translation trust for overridden premise formulas.
        Production machine-checked composition must use check(require_trusted=True).
        """

        target, premise_ids = self._composition_subjects(node_id)
        unknown = set(premise_formula_overrides) - set(premise_ids)
        if unknown:
            raise SymbolicCompositionError(
                f"premise overrides are not direct dependencies of {node_id}: {sorted(unknown)}"
            )
        program = self._compile(
            target_formula=target,
            premise_ids=premise_ids,
            premise_formula_overrides=premise_formula_overrides,
        )
        return CompositionResult(
            node_id=node_id,
            premise_ids=tuple(premise_ids),
            composition_signature=_composition_semantic_signature(self.graph, node_id),
            solver_result=check_program(program),
        )

    def _composition_subjects(
        self,
        node_id: str,
    ) -> tuple[dict[str, Any], list[str]]:
        node = self.graph.require_node(node_id)
        if node.node_type != "claim" or node.data.get("kind") not in {"root", "derived"}:
            raise SymbolicCompositionError(
                "symbolic composition applies only to root/derived claims"
            )

        mapping = self.bridge.claims.get(node_id)
        if mapping is None:
            raise SymbolicCompositionError(
                f"target claim {node_id} has no symbolic mapping"
            )

        premise_ids = list(node.dependencies)
        if not premise_ids:
            raise SymbolicCompositionError(
                f"target claim {node_id} has no direct dependencies"
            )

        unsupported: list[str] = []
        for premise_id in premise_ids:
            premise = self.graph.require_node(premise_id)
            if premise.node_type == "claim":
                mapped = premise_id in self.bridge.claims
            elif premise.node_type == "assumption":
                mapped = premise_id in self.bridge.assumptions
            else:
                mapped = False
            if not mapped:
                unsupported.append(premise_id)
        if unsupported:
            raise SymbolicCompositionError(
                "all direct premises must be symbolically mapped claims or assumptions; "
                f"unsupported={sorted(unsupported)}"
            )
        return mapping.formula, premise_ids

    def _compile(
        self,
        *,
        target_formula: dict[str, Any],
        premise_ids: list[str],
        premise_formula_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> ParsedProgram:
        overrides = premise_formula_overrides or {}
        program = {
            "sorts": list(self.bridge.sorts),
            "functions": {
                name: {"args": decl["args"], "returns": decl["returns"]}
                for name, decl in self.bridge.functions.items()
            },
            "constraints": [
                overrides.get(premise_id, self._premise_formula(premise_id))
                for premise_id in premise_ids
            ],
            "query": {"not": target_formula},
        }
        try:
            return parse_program(program)
        except Exception as exc:
            raise SymbolicCompositionError(
                f"failed to compile symbolic composition: {exc}"
            ) from exc

    def _premise_formula(self, premise_id: str) -> dict[str, Any]:
        premise = self.graph.require_node(premise_id)
        if premise.node_type == "claim":
            return self.bridge.claims[premise_id].formula
        if premise.node_type == "assumption":
            return self.bridge.assumptions[premise_id].formula
        raise SymbolicCompositionError(f"unsupported premise type for {premise_id}")

    def _require_trusted_claims(self, subject_ids: list[str]) -> None:
        if self.assurance_registry is None:
            raise TranslationReviewError(
                "trusted symbolic composition requires a translation assurance registry"
            )
        failures: list[str] = []
        for subject_id in subject_ids:
            node = self.graph.require_node(subject_id)
            if node.node_type == "claim":
                kind = "claim"
            elif node.node_type == "assumption":
                kind = "assumption"
            else:
                failures.append(f"{subject_id}=UNSUPPORTED_NODE_TYPE")
                continue
            result = self.assurance_registry.evaluate(
                self.graph,
                self.bridge,
                kind=kind,
                subject_id=subject_id,
            )
            if result.status != SubjectTrustStatus.TRUSTED:
                failures.append(
                    f"{subject_id}={result.status.value} ({result.reason})"
                )
        if failures:
            raise TranslationReviewError(
                "symbolic composition requires TRUSTED target and direct premises: "
                + "; ".join(failures)
            )
