#!/usr/bin/env python3

import unittest

from harness.symbolic import Z3_AVAILABLE, check_program, parse_program
from harness.symbolic.dsl import SymbolicSchemaError


def var(name: str):
    return {"var": name}


def call(fn: str, *args):
    return {"call": {"fn": fn, "args": list(args)}}


def eq(left, right):
    return {"eq": [left, right]}


def neq(left, right):
    return {"neq": [left, right]}


def implies(left, right):
    return {"implies": [left, right]}


def and_(*values):
    return {"and": list(values)}


def forall(vars_: dict[str, str], body):
    return {"forall": {"vars": vars_, "body": body}}


def exists(vars_: dict[str, str], body):
    return {"exists": {"vars": vars_, "body": body}}


def client_overlay_spec(*, repaired: bool):
    overlay_binding = forall(
        {"s": "ClientState", "o": "Overlay"},
        implies(
            call("visible_overlay", var("s"), var("o")),
            eq(
                call("overlay_document", var("o")),
                call("visible_document", var("s")),
            ),
        ),
    )
    constraints = [overlay_binding] if repaired else []
    return {
        "sorts": ["ClientState", "Overlay", "Document"],
        "functions": {
            "visible_document": {"args": ["ClientState"], "returns": "Document"},
            "overlay_document": {"args": ["Overlay"], "returns": "Document"},
            "visible_overlay": {
                "args": ["ClientState", "Overlay"],
                "returns": "Bool",
            },
        },
        "constraints": constraints,
        "query": exists(
            {"s": "ClientState", "o": "Overlay"},
            and_(
                call("visible_overlay", var("s"), var("o")),
                neq(
                    call("overlay_document", var("o")),
                    call("visible_document", var("s")),
                ),
            ),
        ),
    }


def payment_spec(*, repaired: bool):
    same_account = forall(
        {"e": "LedgerEntry", "t": "Transfer"},
        implies(
            call("emitted_for", var("e"), var("t")),
            eq(
                call("entry_account", var("e")),
                call("transfer_account", var("t")),
            ),
        ),
    )
    same_currency = forall(
        {"e": "LedgerEntry", "t": "Transfer"},
        implies(
            call("emitted_for", var("e"), var("t")),
            eq(
                call("entry_currency", var("e")),
                call("transfer_currency", var("t")),
            ),
        ),
    )
    constraints = [same_account]
    if repaired:
        constraints.append(same_currency)
    return {
        "sorts": ["LedgerEntry", "Transfer", "Account", "Currency"],
        "functions": {
            "emitted_for": {
                "args": ["LedgerEntry", "Transfer"],
                "returns": "Bool",
            },
            "entry_account": {"args": ["LedgerEntry"], "returns": "Account"},
            "transfer_account": {"args": ["Transfer"], "returns": "Account"},
            "entry_currency": {"args": ["LedgerEntry"], "returns": "Currency"},
            "transfer_currency": {"args": ["Transfer"], "returns": "Currency"},
        },
        "constraints": constraints,
        "query": exists(
            {"e": "LedgerEntry", "t": "Transfer"},
            and_(
                call("emitted_for", var("e"), var("t")),
                neq(
                    call("entry_currency", var("e")),
                    call("transfer_currency", var("t")),
                ),
            ),
        ),
    }


class SymbolicKernelTests(unittest.TestCase):
    def test_type_checker_rejects_cross_sort_composition_before_solver(self):
        spec = client_overlay_spec(repaired=False)
        spec["query"] = exists(
            {"s": "ClientState"},
            eq(
                call("overlay_document", var("s")),
                call("visible_document", var("s")),
            ),
        )
        with self.assertRaises(SymbolicSchemaError) as ctx:
            parse_program(spec)
        self.assertIn("expects Overlay, got ClientState", str(ctx.exception))

    @unittest.skipUnless(Z3_AVAILABLE, "z3-solver is not installed")
    def test_cross_document_overlay_is_sat_before_repair_and_unsat_after(self):
        before = check_program(parse_program(client_overlay_spec(repaired=False)))
        after = check_program(parse_program(client_overlay_spec(repaired=True)))
        self.assertEqual(before.status, "sat")
        self.assertEqual(after.status, "unsat")

    @unittest.skipUnless(Z3_AVAILABLE, "z3-solver is not installed")
    def test_same_kernel_handles_payment_currency_binding(self):
        before = check_program(parse_program(payment_spec(repaired=False)))
        after = check_program(parse_program(payment_spec(repaired=True)))
        self.assertEqual(before.status, "sat")
        self.assertEqual(after.status, "unsat")


if __name__ == "__main__":
    unittest.main()
