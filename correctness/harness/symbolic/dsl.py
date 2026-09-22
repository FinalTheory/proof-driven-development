from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ir import (
    BOOL,
    And,
    BoolConst,
    Eq,
    Exists,
    Expr,
    ForAll,
    Function,
    Implies,
    Neq,
    Not,
    Or,
    Sort,
    SymbolicTypeError,
    Var,
)


class SymbolicSchemaError(ValueError):
    """Raised when structured symbolic input is malformed or references unknown symbols."""


@dataclass(frozen=True)
class ParsedProgram:
    sorts: dict[str, Sort]
    functions: dict[str, Function]
    constraints: tuple[Expr, ...]
    query: Expr


def _expect_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SymbolicSchemaError(f"{where} must be a mapping")
    return value


def _parse_expr(
    node: Any,
    *,
    sorts: dict[str, Sort],
    functions: dict[str, Function],
    env: dict[str, Var],
) -> Expr:
    data = _expect_mapping(node, "expression")
    if len(data) != 1:
        raise SymbolicSchemaError(
            f"expression must contain exactly one operator, got {sorted(data)}"
        )
    op, payload = next(iter(data.items()))

    if op == "var":
        if not isinstance(payload, str) or payload not in env:
            raise SymbolicSchemaError(f"unknown variable {payload!r}")
        return env[payload]

    if op == "bool":
        if not isinstance(payload, bool):
            raise SymbolicSchemaError("bool literal must be true/false")
        return BoolConst(payload)

    if op == "call":
        call = _expect_mapping(payload, "call")
        fn_name = call.get("fn")
        args = call.get("args", [])
        if not isinstance(fn_name, str) or fn_name not in functions:
            raise SymbolicSchemaError(f"unknown function {fn_name!r}")
        if not isinstance(args, list):
            raise SymbolicSchemaError("call.args must be a list")
        parsed = [
            _parse_expr(arg, sorts=sorts, functions=functions, env=env)
            for arg in args
        ]
        return functions[fn_name].call(*parsed)

    if op in {"eq", "neq", "implies"}:
        if not isinstance(payload, list) or len(payload) != 2:
            raise SymbolicSchemaError(f"{op} requires exactly two operands")
        left = _parse_expr(payload[0], sorts=sorts, functions=functions, env=env)
        right = _parse_expr(payload[1], sorts=sorts, functions=functions, env=env)
        if op == "eq":
            return Eq(left, right)
        if op == "neq":
            return Neq(left, right)
        return Implies(left, right)

    if op in {"and", "or"}:
        if not isinstance(payload, list):
            raise SymbolicSchemaError(f"{op} requires a list")
        values = [
            _parse_expr(value, sorts=sorts, functions=functions, env=env)
            for value in payload
        ]
        return And.of(values) if op == "and" else Or.of(values)

    if op == "not":
        return Not(_parse_expr(payload, sorts=sorts, functions=functions, env=env))

    if op in {"forall", "exists"}:
        quant = _expect_mapping(payload, op)
        raw_vars = _expect_mapping(quant.get("vars"), f"{op}.vars")
        if not raw_vars:
            raise SymbolicSchemaError(f"{op}.vars must not be empty")
        nested = dict(env)
        variables: list[Var] = []
        for name, sort_name in raw_vars.items():
            if not isinstance(name, str) or not isinstance(sort_name, str):
                raise SymbolicSchemaError(f"{op}.vars entries must be string:string")
            if name in nested:
                raise SymbolicSchemaError(f"variable {name!r} shadows an outer variable")
            if sort_name not in sorts:
                raise SymbolicSchemaError(f"unknown sort {sort_name!r}")
            var = Var(name, sorts[sort_name])
            nested[name] = var
            variables.append(var)
        body = _parse_expr(
            quant.get("body"), sorts=sorts, functions=functions, env=nested
        )
        return ForAll(tuple(variables), body) if op == "forall" else Exists(tuple(variables), body)

    raise SymbolicSchemaError(f"unsupported symbolic operator {op!r}")


def parse_program(spec: dict[str, Any]) -> ParsedProgram:
    data = _expect_mapping(spec, "program")
    allowed = {"sorts", "functions", "constraints", "query"}
    unknown = set(data) - allowed
    if unknown:
        raise SymbolicSchemaError(f"unknown program fields: {sorted(unknown)}")

    raw_sorts = data.get("sorts", [])
    if not isinstance(raw_sorts, list) or not all(isinstance(x, str) and x for x in raw_sorts):
        raise SymbolicSchemaError("sorts must be a list of non-empty strings")
    if len(raw_sorts) != len(set(raw_sorts)):
        raise SymbolicSchemaError("sort names must be unique")
    if "Bool" in raw_sorts:
        raise SymbolicSchemaError("Bool is built in and must not be redeclared")
    sorts = {"Bool": BOOL, **{name: Sort(name) for name in raw_sorts}}

    raw_functions = _expect_mapping(data.get("functions", {}), "functions")
    functions: dict[str, Function] = {}
    for name, raw in raw_functions.items():
        if not isinstance(name, str) or not name:
            raise SymbolicSchemaError("function names must be non-empty strings")
        decl = _expect_mapping(raw, f"function {name}")
        if set(decl) != {"args", "returns"}:
            raise SymbolicSchemaError(
                f"function {name} must contain exactly args and returns"
            )
        arg_names = decl["args"]
        result_name = decl["returns"]
        if not isinstance(arg_names, list) or not all(isinstance(x, str) for x in arg_names):
            raise SymbolicSchemaError(f"function {name}.args must be a list of sort names")
        if not isinstance(result_name, str):
            raise SymbolicSchemaError(f"function {name}.returns must be a sort name")
        missing = [x for x in [*arg_names, result_name] if x not in sorts]
        if missing:
            raise SymbolicSchemaError(f"function {name} references unknown sorts {missing}")
        functions[name] = Function(
            name,
            tuple(sorts[arg] for arg in arg_names),
            sorts[result_name],
        )

    raw_constraints = data.get("constraints", [])
    if not isinstance(raw_constraints, list):
        raise SymbolicSchemaError("constraints must be a list")
    try:
        constraints = tuple(
            _parse_expr(item, sorts=sorts, functions=functions, env={})
            for item in raw_constraints
        )
        query = _parse_expr(data.get("query"), sorts=sorts, functions=functions, env={})
    except SymbolicTypeError as exc:
        raise SymbolicSchemaError(str(exc)) from exc

    for expr in (*constraints, query):
        if expr.sort != BOOL:
            raise SymbolicSchemaError("top-level constraints and query must be Bool")

    return ParsedProgram(sorts, functions, constraints, query)
