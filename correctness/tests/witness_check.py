#!/usr/bin/env python3
"""Checked finite-model witnesses for solver-incomplete GAP queries.

Background: some design-obligation GAP queries conjoin a root (G2) whose
formula uses a universally quantified transitive-closure clause. z3's
transitive-closure procedure returns "unknown" for the satisfiable
variant of such queries even on tiny inputs, so solver search cannot
certify those GAPs. Instead, these tests exhibit an explicit finite
model and evaluate the whole query under it with this module, which
performs no solver search: quantifiers range over the fixture's finite
universes and transitive closure is graph reachability.

A fixture is data only (universes + function entries + else-expressions
in a small JSON language). The program under check is always rebuilt
from the live bridge, so graph drift fails closed: a stale witness
simply evaluates to False and the test fails.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from harness.symbolic.ir import (
    And,
    BoolConst,
    Call,
    Eq,
    Exists,
    ForAll,
    Implies,
    Neq,
    Not,
    Or,
    TransitiveClosure,
    Var,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def eval_else_expr(node, args):
    """Evaluate a fixture else-expression against concrete arguments."""
    if "const" in node:
        return node["const"]
    if "var" in node:
        return args[node["var"]]
    if "eq" in node:
        left, right = node["eq"]
        return eval_else_expr(left, args) == eval_else_expr(right, args)
    if "neq" in node:
        left, right = node["neq"]
        return eval_else_expr(left, args) != eval_else_expr(right, args)
    if "and" in node:
        return all(eval_else_expr(child, args) for child in node["and"])
    if "or" in node:
        return any(eval_else_expr(child, args) for child in node["or"])
    if "not" in node:
        return not eval_else_expr(node["not"], args)
    if "implies" in node:
        premise, conclusion = node["implies"]
        return (not eval_else_expr(premise, args)) or eval_else_expr(
            conclusion, args
        )
    if "if" in node:
        cond, then, els = node["if"]
        return eval_else_expr(then if eval_else_expr(cond, args) else els, args)
    raise ValueError(f"unknown else-expression node: {node!r}")


class WitnessModel:
    """A finite model loaded from a fixture file."""

    def __init__(self, data):
        self.universes = data["universes"]
        self.functions = data["functions"]
        self._entries = {
            name: {tuple(args): value for args, value in decl["entries"]}
            for name, decl in self.functions.items()
        }

    def call(self, fn_name, args):
        decl = self.functions[fn_name]
        table = self._entries[fn_name]
        key = tuple(args)
        if key in table:
            return table[key]
        return eval_else_expr(decl["else"], list(args))

    def tc_reachable(self, fn_name, a, b):
        decl = self.functions[fn_name]
        arg_sorts = decl["args"]
        assert len(arg_sorts) == 2, f"TC relation {fn_name} must be binary"
        edges = {}
        for x in self.universes[arg_sorts[0]]:
            for y in self.universes[arg_sorts[1]]:
                if self.call(fn_name, [x, y]) is True:
                    edges.setdefault(x, set()).add(y)
        seen = set()
        stack = [a]
        while stack:
            node = stack.pop()
            if node == b:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges.get(node, ()))
        return False


def evaluate(expr, model, env=None):
    """Evaluate an IR expression under a witness model to a Python value."""
    env = {} if env is None else env
    if isinstance(expr, BoolConst):
        return expr.value
    if isinstance(expr, Var):
        return env[expr]
    if isinstance(expr, Call):
        args = [evaluate(arg, model, env) for arg in expr.args]
        return model.call(expr.function.name, args)
    if isinstance(expr, Eq):
        return evaluate(expr.left, model, env) == evaluate(
            expr.right, model, env
        )
    if isinstance(expr, Neq):
        return evaluate(expr.left, model, env) != evaluate(
            expr.right, model, env
        )
    if isinstance(expr, Not):
        return not evaluate(expr.value, model, env)
    if isinstance(expr, And):
        return all(evaluate(value, model, env) for value in expr.values)
    if isinstance(expr, Or):
        return any(evaluate(value, model, env) for value in expr.values)
    if isinstance(expr, Implies):
        return (not evaluate(expr.premise, model, env)) or evaluate(
            expr.conclusion, model, env
        )
    if isinstance(expr, (ForAll, Exists)):
        universes = [model.universes[var.sort.name] for var in expr.variables]
        is_forall = isinstance(expr, ForAll)
        for combo in itertools.product(*universes):
            nested = dict(env)
            for var, value in zip(expr.variables, combo):
                nested[var] = value
            value = evaluate(expr.body, model, nested)
            if is_forall and not value:
                return False
            if not is_forall and value:
                return True
        return is_forall
    if isinstance(expr, TransitiveClosure):
        return model.tc_reachable(
            expr.relation.name,
            evaluate(expr.left, model, env),
            evaluate(expr.right, model, env),
        )
    raise TypeError(f"unsupported expression type {type(expr).__name__}")


def check_program_under_witness(program, model):
    """Return (ok, failing) with failing like ("constraint", i) or ("query", None)."""
    for index, constraint in enumerate(program.constraints):
        if not evaluate(constraint, model):
            return False, ("constraint", index)
    if not evaluate(program.query, model):
        return False, ("query", None)
    return True, None


def load_witness(name):
    """Load a witness fixture by short name (e.g. "stale_base")."""
    data = json.loads((FIXTURES / f"witness_{name}.json").read_text())
    return WitnessModel(data)


def build_obligation_program(verifier, bridge, obligation, claim_overrides=None, contract_overrides=None):
    """Rebuild the exact constrained program check() would solve.

    Mirrors DesignObligationVerifier.check/check_with_overrides selection
    logic without invoking the solver, so a witness can be evaluated
    against the live program.
    """
    claim_overrides = claim_overrides or {}
    contract_overrides = contract_overrides or {}
    claim_ids, assumption_ids, contract_ids = verifier._resolve_constraints(
        obligation
    )
    selected = (
        [
            contract_overrides.get(cid, bridge.contracts[cid].formula)
            for cid in contract_ids
        ]
        + [bridge.assumptions[aid].formula for aid in assumption_ids]
        + [
            claim_overrides.get(
                cid,
                bridge.materialize_claim_formula(
                    cid, contract_formula_overrides=contract_overrides
                ),
            )
            for cid in claim_ids
        ]
    )
    return verifier._compile({"not": obligation.formula}, constraints=selected)
