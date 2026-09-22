from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class SymbolicTypeError(ValueError):
    """Raised when a symbolic expression is well-shaped but semantically ill-typed."""


@dataclass(frozen=True)
class Sort:
    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("sort name must be non-empty")


BOOL = Sort("Bool")


class Expr:
    @property
    def sort(self) -> Sort:
        raise NotImplementedError


@dataclass(frozen=True)
class Var(Expr):
    name: str
    var_sort: Sort

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("variable name must be non-empty")

    @property
    def sort(self) -> Sort:
        return self.var_sort


@dataclass(frozen=True)
class BoolConst(Expr):
    value: bool

    @property
    def sort(self) -> Sort:
        return BOOL


@dataclass(frozen=True)
class Function:
    name: str
    args: tuple[Sort, ...]
    result: Sort

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("function name must be non-empty")

    def call(self, *args: Expr) -> "Call":
        if len(args) != len(self.args):
            raise SymbolicTypeError(
                f"{self.name} expects {len(self.args)} args, got {len(args)}"
            )
        for index, (actual, expected) in enumerate(zip(args, self.args)):
            if actual.sort != expected:
                raise SymbolicTypeError(
                    f"{self.name} arg {index} expects {expected.name}, got {actual.sort.name}"
                )
        return Call(self, tuple(args))


@dataclass(frozen=True)
class Call(Expr):
    function: Function
    args: tuple[Expr, ...]

    @property
    def sort(self) -> Sort:
        return self.function.result


def _require_bool(expr: Expr, where: str) -> None:
    if expr.sort != BOOL:
        raise SymbolicTypeError(f"{where} expects Bool, got {expr.sort.name}")


@dataclass(frozen=True)
class Eq(Expr):
    left: Expr
    right: Expr

    def __post_init__(self) -> None:
        if self.left.sort != self.right.sort:
            raise SymbolicTypeError(
                f"eq requires same sort, got {self.left.sort.name} and {self.right.sort.name}"
            )

    @property
    def sort(self) -> Sort:
        return BOOL


@dataclass(frozen=True)
class Neq(Expr):
    left: Expr
    right: Expr

    def __post_init__(self) -> None:
        if self.left.sort != self.right.sort:
            raise SymbolicTypeError(
                f"neq requires same sort, got {self.left.sort.name} and {self.right.sort.name}"
            )

    @property
    def sort(self) -> Sort:
        return BOOL


@dataclass(frozen=True)
class Not(Expr):
    value: Expr

    def __post_init__(self) -> None:
        _require_bool(self.value, "not")

    @property
    def sort(self) -> Sort:
        return BOOL


@dataclass(frozen=True)
class And(Expr):
    values: tuple[Expr, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise SymbolicTypeError("and requires at least one operand")
        for value in self.values:
            _require_bool(value, "and")

    @classmethod
    def of(cls, values: Iterable[Expr]) -> "And":
        return cls(tuple(values))

    @property
    def sort(self) -> Sort:
        return BOOL


@dataclass(frozen=True)
class Or(Expr):
    values: tuple[Expr, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise SymbolicTypeError("or requires at least one operand")
        for value in self.values:
            _require_bool(value, "or")

    @classmethod
    def of(cls, values: Iterable[Expr]) -> "Or":
        return cls(tuple(values))

    @property
    def sort(self) -> Sort:
        return BOOL


@dataclass(frozen=True)
class Implies(Expr):
    premise: Expr
    conclusion: Expr

    def __post_init__(self) -> None:
        _require_bool(self.premise, "implies premise")
        _require_bool(self.conclusion, "implies conclusion")

    @property
    def sort(self) -> Sort:
        return BOOL


@dataclass(frozen=True)
class ForAll(Expr):
    variables: tuple[Var, ...]
    body: Expr

    def __post_init__(self) -> None:
        if not self.variables:
            raise SymbolicTypeError("forall requires at least one variable")
        if len({v.name for v in self.variables}) != len(self.variables):
            raise SymbolicTypeError("forall variable names must be unique in one binder")
        _require_bool(self.body, "forall body")

    @property
    def sort(self) -> Sort:
        return BOOL


@dataclass(frozen=True)
class Exists(Expr):
    variables: tuple[Var, ...]
    body: Expr

    def __post_init__(self) -> None:
        if not self.variables:
            raise SymbolicTypeError("exists requires at least one variable")
        if len({v.name for v in self.variables}) != len(self.variables):
            raise SymbolicTypeError("exists variable names must be unique in one binder")
        _require_bool(self.body, "exists body")

    @property
    def sort(self) -> Sort:
        return BOOL
