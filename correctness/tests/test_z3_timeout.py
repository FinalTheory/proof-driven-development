#!/usr/bin/env python3
"""Boundary tests for the per-check solver timeout (PDD_Z3_TIMEOUT_MS).

These tests travel with the timeout patch in z3_backend.py: the default
is a finite 120000 ms, values must be positive integers within z3's
uint32 parameter range, and anything else fails loudly instead of
silently disabling or altering the limit.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from harness.symbolic.z3_backend import _DEFAULT_TIMEOUT_MS, _MAX_TIMEOUT_MS, _solver_timeout_ms


class SolverTimeoutTests(unittest.TestCase):
    def test_default_is_finite(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_solver_timeout_ms(), _DEFAULT_TIMEOUT_MS)
            self.assertEqual(_DEFAULT_TIMEOUT_MS, 120000)

    def test_valid_value_is_accepted(self) -> None:
        with mock.patch.dict(os.environ, {"PDD_Z3_TIMEOUT_MS": "5000"}):
            self.assertEqual(_solver_timeout_ms(), 5000)

    def test_max_uint32_is_accepted(self) -> None:
        with mock.patch.dict(os.environ, {"PDD_Z3_TIMEOUT_MS": str(_MAX_TIMEOUT_MS)}):
            self.assertEqual(_solver_timeout_ms(), _MAX_TIMEOUT_MS)
            self.assertEqual(_MAX_TIMEOUT_MS, 4294967295)

    def test_zero_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"PDD_Z3_TIMEOUT_MS": "0"}):
            with self.assertRaises(ValueError):
                _solver_timeout_ms()

    def test_negative_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"PDD_Z3_TIMEOUT_MS": "-100"}):
            with self.assertRaises(ValueError):
                _solver_timeout_ms()

    def test_non_integer_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"PDD_Z3_TIMEOUT_MS": "ten"}):
            with self.assertRaises(ValueError):
                _solver_timeout_ms()

    def test_above_uint32_max_is_rejected(self) -> None:
        # 2**32 would wrap to timeout 0 (unlimited) in z3's uint param.
        with mock.patch.dict(os.environ, {"PDD_Z3_TIMEOUT_MS": "4294967296"}):
            with self.assertRaises(ValueError):
                _solver_timeout_ms()


if __name__ == "__main__":
    unittest.main()
