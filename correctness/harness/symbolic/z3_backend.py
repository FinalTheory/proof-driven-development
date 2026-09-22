from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dsl import ParsedProgram
from .ir import And, BoolConst, Call, Eq, Exists, Expr, ForAll, Implies, Neq, Not, Or, Var

try:
    import z3  # type: ignore
except ImportError:  # pragma: no cover - environment-dependent
    z3 = None

Z3_AVAILABLE = z3 is not None


@dataclass(frozen=True)
class CheckResult:
    status: str
    model: str | None = None


class _Compiler:
    def __init__(self, program: ParsedProgram):
        if z3 is None:
            raise RuntimeError("z3-solver is not installed")
        self.sorts: dict[str, Any] = {"Bool": z3.BoolSort()}
        for name in program.sorts:
            if name != "Bool":
                self.sorts[name] = z3.DeclareSort(name)
        self.functions = {
            name: z3.Function(
                name,
                *(self.sorts[s.name] for s in fn.args),
                self.sorts[fn.result.name],
            )
            for name, fn in program.functions.items()
        }

    def expr(self, expr: Expr, env: dict[Var, Any] | None = None) -> Any:
        env = {} if env is None else env
        if isinstance(expr, Var):
            if expr not in env:
                raise ValueError(f"unbound symbolic variable {expr.name!r}")
            return env[expr]
        if isinstance(expr, BoolConst):
            return z3.BoolVal(expr.value)
        if isinstance(expr, Call):
            fn = self.functions[expr.function.name]
            return fn(*(self.expr(arg, env) for arg in expr.args))
        if isinstance(expr, Eq):
            return self.expr(expr.left, env) == self.expr(expr.right, env)
        if isinstance(expr, Neq):
            return self.expr(expr.left, env) != self.expr(expr.right, env)
        if isinstance(expr, Not):
            return z3.Not(self.expr(expr.value, env))
        if isinstance(expr, And):
            return z3.And(*(self.expr(v, env) for v in expr.values))
        if isinstance(expr, Or):
            return z3.Or(*(self.expr(v, env) for v in expr.values))
        if isinstance(expr, Implies):
            return z3.Implies(self.expr(expr.premise, env), self.expr(expr.conclusion, env))
        if isinstance(expr, (ForAll, Exists)):
            nested = dict(env)
            bound = []
            for index, var in enumerate(expr.variables):
                zvar = z3.Const(f"{var.name}!{index}", self.sorts[var.sort.name])
                nested[var] = zvar
                bound.append(zvar)
            body = self.expr(expr.body, nested)
            return z3.ForAll(bound, body) if isinstance(expr, ForAll) else z3.Exists(bound, body)
        raise TypeError(f"unsupported expression type {type(expr).__name__}")


def check_program(program: ParsedProgram) -> CheckResult:
    """Check whether constraints AND query are satisfiable.

    Intended PDD use is a counterexample query:
      SAT   => current symbolic constraints admit the proposed bad state.
      UNSAT => current symbolic constraints exclude that bad state.
    """

    if z3 is None:
        raise RuntimeError("z3-solver is not installed")
    compiler = _Compiler(program)
    solver = z3.Solver()
    for constraint in program.constraints:
        solver.add(compiler.expr(constraint))
    solver.add(compiler.expr(program.query))
    result = solver.check()
    if result == z3.sat:
        return CheckResult("sat", str(solver.model()))
    if result == z3.unsat:
        return CheckResult("unsat", None)
    return CheckResult("unknown", None)
