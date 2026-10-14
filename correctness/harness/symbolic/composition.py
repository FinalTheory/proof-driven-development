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


def _contains_transitive_closure(node: Any) -> bool:
    if isinstance(node, dict):
        if set(node) == {"transitive_closure"}:
            return True
        return any(_contains_transitive_closure(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_transitive_closure(value) for value in node)
    return False


def _transitive_closure_relations(node: Any) -> set[str]:
    """Return every relation named by a transitive_closure expression."""
    result: set[str] = set()
    if isinstance(node, dict):
        if set(node) == {"transitive_closure"} and isinstance(
            node["transitive_closure"], dict
        ):
            relation = node["transitive_closure"].get("fn")
            if isinstance(relation, str):
                result.add(relation)
        for value in node.values():
            result.update(_transitive_closure_relations(value))
    elif isinstance(node, list):
        for value in node:
            result.update(_transitive_closure_relations(value))
    return result


def _abstract_transitive_closure(
    node: Any,
    relation_aliases: dict[str, str],
) -> Any:
    """Replace each TC(R,x,y) by one shared ordinary relation R_tc(x,y).

    This rewrite is used only in composition phase two. The fresh relations have no
    closure axioms: phase two therefore has to hold for every interpretation of each
    R_tc. If it holds for arbitrary relations, it also holds for the concrete finite
    transitive-closure relation whose invariant lemma was established in phase one.
    """
    if isinstance(node, dict):
        if set(node) == {"transitive_closure"}:
            closure = node["transitive_closure"]
            if not isinstance(closure, dict) or set(closure) != {"fn", "from", "to"}:
                raise SymbolicCompositionError(
                    "phase-two transitive_closure must contain exactly fn, from, and to"
                )
            relation = closure.get("fn")
            if not isinstance(relation, str) or relation not in relation_aliases:
                raise SymbolicCompositionError(
                    "phase-two transitive_closure relation was not prepared for abstraction"
                )
            return {
                "call": {
                    "fn": relation_aliases[relation],
                    "args": [
                        _abstract_transitive_closure(closure["from"], relation_aliases),
                        _abstract_transitive_closure(closure["to"], relation_aliases),
                    ],
                }
            }
        return {
            key: _abstract_transitive_closure(value, relation_aliases)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_abstract_transitive_closure(value, relation_aliases) for value in node]
    return node


def _top_level_conjuncts(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten only explicit top-level conjunctions for structural proof lemmas."""
    if set(node) == {"and"} and isinstance(node["and"], list):
        result: list[dict[str, Any]] = []
        for value in node["and"]:
            if isinstance(value, dict):
                result.extend(_top_level_conjuncts(value))
        return result
    return [node]


def _direct_relation_inclusions(
    constraints: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    """Find explicit `forall x,y: R1(x,y) -> R2(x,y)` premise lemmas.

    Composition may soundly lift such an established edge inclusion to finite-path
    reachability inclusion, TC(R1) subset TC(R2). The matcher is intentionally exact:
    it does not infer inclusion from names, catalog metadata, or richer formulas.
    """
    result: set[tuple[str, str]] = set()
    for constraint in constraints:
        for clause in _top_level_conjuncts(constraint):
            if set(clause) != {"forall"} or not isinstance(clause["forall"], dict):
                continue
            quant = clause["forall"]
            variables = quant.get("vars")
            body = quant.get("body")
            if (
                not isinstance(variables, dict)
                or len(variables) != 2
                or not isinstance(body, dict)
                or set(body) != {"implies"}
            ):
                continue
            implication = body["implies"]
            if not isinstance(implication, list) or len(implication) != 2:
                continue
            left, right = implication
            if (
                not isinstance(left, dict)
                or set(left) != {"call"}
                or not isinstance(right, dict)
                or set(right) != {"call"}
            ):
                continue
            left_call = left["call"]
            right_call = right["call"]
            if not isinstance(left_call, dict) or not isinstance(right_call, dict):
                continue
            left_fn = left_call.get("fn")
            right_fn = right_call.get("fn")
            left_args = left_call.get("args")
            right_args = right_call.get("args")
            if (
                not isinstance(left_fn, str)
                or not isinstance(right_fn, str)
                or not isinstance(left_args, list)
                or len(left_args) != 2
                or left_args != right_args
            ):
                continue
            expected_args = [{"var": name} for name in variables]
            if left_args != expected_args:
                continue
            result.add((left_fn, right_fn))
    return result


def _abstract_closure_inclusion_lemma(
    source_alias: str,
    target_alias: str,
    *,
    endpoint_sort: str,
) -> dict[str, Any]:
    """Encode TC(R1) subset TC(R2) over phase-two abstract closure relations."""
    return {
        "forall": {
            "vars": {"tc_from": endpoint_sort, "tc_to": endpoint_sort},
            "body": {
                "implies": [
                    {
                        "call": {
                            "fn": source_alias,
                            "args": [{"var": "tc_from"}, {"var": "tc_to"}],
                        }
                    },
                    {
                        "call": {
                            "fn": target_alias,
                            "args": [{"var": "tc_from"}, {"var": "tc_to"}],
                        }
                    },
                ]
            },
        }
    }


def _substitute_var(node: Any, source: str, destination: str) -> Any:
    """Capture-safe substitution for a top-level quantified variable in formula data.

    The symbolic parser rejects shadowing of an outer variable, so replacing a top-level
    bound variable name cannot be captured by a nested quantifier in a well-formed formula.
    """
    if isinstance(node, dict):
        if set(node) == {"var"} and node["var"] == source:
            return {"var": destination}
        return {
            key: _substitute_var(value, source, destination)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_substitute_var(value, source, destination) for value in node]
    return node


def _rewrite_transitive_invariant_clause(clause: dict[str, Any]) -> dict[str, Any]:
    """Reduce one finite-path invariant clause to its one-step induction obligation.

    Accepted shape, deliberately narrow:

        forall x,y,p...:
          TC(R,x,y) AND I(x,p...) -> I(y,p...)

    where the conclusion is alpha-equivalent to the source invariant after replacing
    x with y.  The theorem used by the composition verifier is ordinary induction on
    the length of a finite non-empty R path.  Any other transitive-closure use fails
    closed rather than being delegated to the SMT solver.
    """
    if set(clause) != {"forall"} or not isinstance(clause["forall"], dict):
        raise SymbolicCompositionError(
            "transitive invariant lifting requires a top-level forall clause"
        )
    quant = clause["forall"]
    if set(quant) != {"vars", "body"} or not isinstance(quant["vars"], dict):
        raise SymbolicCompositionError(
            "transitive invariant forall must contain exactly vars and body"
        )
    body = quant["body"]
    if not isinstance(body, dict) or set(body) != {"implies"}:
        raise SymbolicCompositionError(
            "transitive invariant forall body must be one implication"
        )
    implication = body["implies"]
    if not isinstance(implication, list) or len(implication) != 2:
        raise SymbolicCompositionError(
            "transitive invariant implication must have exactly two operands"
        )
    premise, conclusion = implication
    if not isinstance(premise, dict) or set(premise) != {"and"}:
        raise SymbolicCompositionError(
            "transitive invariant implication premise must be an and"
        )
    conjuncts = premise["and"]
    if not isinstance(conjuncts, list) or len(conjuncts) != 2:
        raise SymbolicCompositionError(
            "transitive invariant premise must contain exactly closure and source invariant"
        )

    closure_indexes = [
        index
        for index, value in enumerate(conjuncts)
        if isinstance(value, dict) and set(value) == {"transitive_closure"}
    ]
    if len(closure_indexes) != 1:
        raise SymbolicCompositionError(
            "transitive invariant premise must contain exactly one transitive_closure"
        )
    closure_index = closure_indexes[0]
    invariant_index = 1 - closure_index
    closure = conjuncts[closure_index]["transitive_closure"]
    if not isinstance(closure, dict) or set(closure) != {"fn", "from", "to"}:
        raise SymbolicCompositionError(
            "transitive_closure must contain exactly fn, from, and to"
        )
    relation = closure["fn"]
    left = closure["from"]
    right = closure["to"]
    if (
        not isinstance(relation, str)
        or not isinstance(left, dict)
        or set(left) != {"var"}
        or not isinstance(right, dict)
        or set(right) != {"var"}
        or not isinstance(left["var"], str)
        or not isinstance(right["var"], str)
    ):
        raise SymbolicCompositionError(
            "transitive invariant closure endpoints must be bound variables"
        )
    source_var = left["var"]
    destination_var = right["var"]
    if source_var == destination_var:
        raise SymbolicCompositionError(
            "transitive invariant source and destination variables must be distinct"
        )
    variables = quant["vars"]
    if source_var not in variables or destination_var not in variables:
        raise SymbolicCompositionError(
            "transitive invariant closure endpoints must be quantified by the clause"
        )
    if variables[source_var] != variables[destination_var]:
        raise SymbolicCompositionError(
            "transitive invariant closure endpoints must have the same sort"
        )

    source_invariant = conjuncts[invariant_index]
    if _contains_transitive_closure(source_invariant) or _contains_transitive_closure(conclusion):
        raise SymbolicCompositionError(
            "nested transitive closure is not supported by invariant lifting"
        )
    expected_conclusion = _substitute_var(
        source_invariant, source_var, destination_var
    )
    if expected_conclusion != conclusion:
        raise SymbolicCompositionError(
            "transitive invariant conclusion must be the source invariant with only "
            "the path-state variable advanced to the destination"
        )

    direct_edge = {
        "call": {
            "fn": relation,
            "args": [{"var": source_var}, {"var": destination_var}],
        }
    }
    rewritten_conjuncts = list(conjuncts)
    rewritten_conjuncts[closure_index] = direct_edge
    return {
        "forall": {
            "vars": dict(variables),
            "body": {
                "implies": [
                    {"and": rewritten_conjuncts},
                    conclusion,
                ]
            },
        }
    }


@dataclass(frozen=True)
class CompositionResult:
    node_id: str
    premise_ids: tuple[str, ...]
    composition_signature: str
    solver_result: CheckResult
    proof_rule: str

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
        established_formulas = [self._premise_formula(premise_id) for premise_id in premise_ids]
        phase_one_target, derived_lemmas, phase_two_target, proof_rule = self._proof_plan(
            target, established_formulas=established_formulas
        )
        phase_one = check_program(
            self._compile(
                target_formula=phase_one_target,
                premise_ids=premise_ids,
                abstract_transitive_closure=(proof_rule != "direct_smt"),
            )
        )
        solver_result = phase_one
        if phase_one.status == "unsat" and phase_two_target is not None:
            solver_result = check_program(
                self._compile(
                    target_formula=phase_two_target,
                    premise_ids=premise_ids,
                    extra_constraints=list(derived_lemmas),
                    abstract_transitive_closure=True,
                )
            )
        return CompositionResult(
            node_id=node_id,
            premise_ids=tuple(premise_ids),
            composition_signature=_composition_semantic_signature(self.graph, node_id),
            solver_result=solver_result,
            proof_rule=proof_rule,
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
        established_formulas = [
            premise_formula_overrides.get(premise_id, self._premise_formula(premise_id))
            for premise_id in premise_ids
        ]
        phase_one_target, derived_lemmas, phase_two_target, proof_rule = self._proof_plan(
            target, established_formulas=established_formulas
        )
        phase_one = check_program(
            self._compile(
                target_formula=phase_one_target,
                premise_ids=premise_ids,
                premise_formula_overrides=premise_formula_overrides,
                abstract_transitive_closure=(proof_rule != "direct_smt"),
            )
        )
        solver_result = phase_one
        if phase_one.status == "unsat" and phase_two_target is not None:
            solver_result = check_program(
                self._compile(
                    target_formula=phase_two_target,
                    premise_ids=premise_ids,
                    premise_formula_overrides=premise_formula_overrides,
                    extra_constraints=list(derived_lemmas),
                    abstract_transitive_closure=True,
                )
            )
        return CompositionResult(
            node_id=node_id,
            premise_ids=tuple(premise_ids),
            composition_signature=_composition_semantic_signature(self.graph, node_id),
            solver_result=solver_result,
            proof_rule=proof_rule,
        )

    def _proof_plan(
        self,
        target_formula: dict[str, Any],
        *,
        established_formulas: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], dict[str, Any] | None, str]:
        """Build a sound one- or two-phase proof plan for one composition target.

        Direct first-order targets are checked unchanged. Supported finite transitive
        invariant clauses are proved first by reducing each to its one-step induction
        obligation. Only after that phase is UNSAT may the original finite-path clauses
        be used as derived lemmas while proving any remaining target clauses. This avoids
        both circularly assuming unproved target clauses and leaking transitive-closure
        induction back into the SMT solver.
        """
        if not _contains_transitive_closure(target_formula):
            return target_formula, (), None, "direct_smt"

        if set(target_formula) == {"and"} and isinstance(target_formula["and"], list):
            clauses = target_formula["and"]
        else:
            clauses = [target_formula]

        lifted_original: list[dict[str, Any]] = []
        lifted_one_step: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for clause in clauses:
            if not isinstance(clause, dict):
                raise SymbolicCompositionError(
                    "composition target clauses must be mappings"
                )
            if _contains_transitive_closure(clause):
                lifted_original.append(clause)
                lifted_one_step.append(_rewrite_transitive_invariant_clause(clause))
            else:
                # A target clause that is literally one of the active direct-premise
                # formulas is already established. Compare against the effective
                # premise formulas (including test-only overrides), so mutation
                # sensitivity cannot be bypassed by this shortcut.
                if clause not in established_formulas:
                    remaining.append(clause)

        if not lifted_original:
            raise SymbolicCompositionError(
                "transitive closure was detected but no supported invariant clause was found"
            )

        phase_one_target = (
            lifted_one_step[0]
            if len(lifted_one_step) == 1
            else {"and": lifted_one_step}
        )
        if not remaining:
            return (
                phase_one_target,
                tuple(lifted_original),
                None,
                "finite_transitive_invariant_lift",
            )
        phase_two_target = remaining[0] if len(remaining) == 1 else {"and": remaining}
        return (
            phase_one_target,
            tuple(lifted_original),
            phase_two_target,
            "finite_transitive_invariant_lift_then_smt",
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
        extra_constraints: list[dict[str, Any]] | None = None,
        abstract_transitive_closure: bool = False,
    ) -> ParsedProgram:
        overrides = premise_formula_overrides or {}
        constraints = [
            overrides.get(premise_id, self._premise_formula(premise_id))
            for premise_id in premise_ids
        ] + list(extra_constraints or [])
        function_declarations = {
            name: {"args": decl["args"], "returns": decl["returns"]}
            for name, decl in self.bridge.functions.items()
        }

        if abstract_transitive_closure:
            if _contains_transitive_closure(target_formula):
                raise SymbolicCompositionError(
                    "phase-two target must not contain transitive_closure"
                )
            relation_names: set[str] = set()
            for constraint in constraints:
                relation_names.update(_transitive_closure_relations(constraint))
            direct_inclusions = _direct_relation_inclusions(constraints)
            relation_aliases: dict[str, str] = {}
            for relation in sorted(relation_names):
                declaration = self.bridge.functions.get(relation)
                if declaration is None:
                    raise SymbolicCompositionError(
                        f"phase-two transitive relation {relation!r} is not declared"
                    )
                args = declaration.get("args")
                returns = declaration.get("returns")
                if (
                    not isinstance(args, list)
                    or len(args) != 2
                    or args[0] != args[1]
                    or returns != "Bool"
                ):
                    raise SymbolicCompositionError(
                        "phase-two transitive abstraction requires a binary Bool relation "
                        "whose arguments have the same sort"
                    )
                alias = f"__composition_tc__{relation}"
                if alias in function_declarations:
                    raise SymbolicCompositionError(
                        f"phase-two transitive abstraction function collision: {alias}"
                    )
                relation_aliases[relation] = alias
                function_declarations[alias] = {
                    "args": list(args),
                    "returns": "Bool",
                }
            constraints = [
                _abstract_transitive_closure(constraint, relation_aliases)
                for constraint in constraints
            ]
            for source_relation, target_relation in sorted(direct_inclusions):
                if (
                    source_relation not in relation_aliases
                    or target_relation not in relation_aliases
                ):
                    continue
                source_decl = self.bridge.functions[source_relation]
                target_decl = self.bridge.functions[target_relation]
                if source_decl["args"] != target_decl["args"]:
                    raise SymbolicCompositionError(
                        "transitive closure inclusion requires relations with identical "
                        f"argument sorts: {source_relation} -> {target_relation}"
                    )
                constraints.append(
                    _abstract_closure_inclusion_lemma(
                        relation_aliases[source_relation],
                        relation_aliases[target_relation],
                        endpoint_sort=source_decl["args"][0],
                    )
                )

        program = {
            "sorts": list(self.bridge.sorts),
            "functions": function_declarations,
            "constraints": constraints,
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
