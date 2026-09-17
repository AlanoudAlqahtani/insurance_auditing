#!/usr/bin/env python3
"""Regression tests for submission-level reconciliation behavior."""

import os
import sys
import unittest
from types import SimpleNamespace

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC_DIR)

from run_pipeline import build_submission_rows  # noqa: E402


def priced_line(
    line_id,
    record_key,
    expected,
    billed,
    violations=(),
    *,
    priceable=True,
    cumulative_incomplete=False,
    used_unit_basis=False,
):
    """Create the minimal priced-line shape used by build_submission_rows."""
    return SimpleNamespace(
        line_id=line_id,
        record_key=record_key,
        expected_line_total_cents=expected,
        billed_line_total_cents=billed,
        violations=list(violations),
        priceable=priceable,
        cumulative_incomplete=cumulative_incomplete,
        used_unit_basis=used_unit_basis,
    )


def build_one(priced, findings, billed_total, canonical_key=1):
    """Build and return one submission row for a synthetic invoice."""
    invoice_id = "INV-TEST-001"
    rows = build_submission_rows(
        {invoice_id: priced},
        {invoice_id: findings},
        {invoice_id: billed_total},
        {invoice_id: canonical_key},
    )
    return rows[0]


class TestDuplicateInvoiceReconciliation(unittest.TestCase):
    def test_duplicate_invoice_id_uses_canonical_record_only(self):
        priced = [
            priced_line("OLD-L1", 0, 9_000, 10_000, ["unit_price_mismatch"]),
            priced_line("NEW-L1", 1, 20_000, 20_000),
        ]
        findings = [
            {"category": "duplicate_invoice_id", "record_key": None},
        ]

        row = build_one(priced, findings, billed_total=20_000)

        self.assertEqual(row["expected_total_cents"], 20_000)
        self.assertEqual(row["billed_total_cents"], 20_000)
        self.assertEqual(row["error_category"], "duplicate_invoice_id")

    def test_noncanonical_record_does_not_leak_record_specific_findings(self):
        priced = [
            priced_line("OLD-L1", 0, 9_000, 10_000),
            priced_line("NEW-L1", 1, 20_000, 20_000),
        ]
        findings = [
            {"category": "line_total_arithmetic", "record_key": 0},
            {"category": "duplicate_invoice_id", "record_key": None},
        ]

        row = build_one(priced, findings, billed_total=20_000)

        self.assertEqual(row["error_category"], "duplicate_invoice_id")
        self.assertNotIn("line_total_arithmetic", row["error_category"])

    def test_cross_invoice_duplicate_removes_identified_offending_line(self):
        priced = [
            priced_line("KEEP", 1, 10_000, 10_000),
            priced_line("DUP", 1, 5_000, 5_000),
        ]
        findings = [{
            "category": "cross_invoice_duplicate",
            "record_key": 1,
            "remediation_line_ids": ["DUP"],
        }]

        row = build_one(priced, findings, billed_total=15_000)

        self.assertEqual(row["expected_total_cents"], 10_000)
        self.assertIn("cross_invoice_duplicate", row["error_category"])

    def test_ambiguous_cross_invoice_duplicate_does_not_change_expected_total(self):
        priced = [
            priced_line("L1", 1, 10_000, 10_000),
            priced_line("L2", 1, 5_000, 5_000),
        ]
        findings = [{
            "category": "cross_invoice_duplicate",
            "record_key": 1,
            "remediation_line_ids": None,
        }]

        row = build_one(priced, findings, billed_total=15_000)

        self.assertEqual(row["expected_total_cents"], 15_000)
        self.assertIn("cross_invoice_duplicate", row["error_category"])


if __name__ == "__main__":
    unittest.main()
