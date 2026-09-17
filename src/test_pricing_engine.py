#!/usr/bin/env python3
"""
Regression tests for the deterministic pricing engine.

Every expected figure is computed independently from the contract text rather
than copied from the engine's output. The suite focuses especially on failure
modes that can produce plausible but incorrect reimbursement amounts:

  1. rounding half-up AFTER EACH STEP rather than once at the end
  2. threshold premiums assessed on the patient/service/DAY AGGREGATE rather
     than on a single line item's quantity
  3. cumulative volume discounts counting utilisation strictly BEFORE the line
     being priced, across all patients, in (service_date, line_id) order

Run:  python -m pytest src/test_pricing_engine.py -v
      (or: python src/test_pricing_engine.py  for a plain-stdlib run)
"""

import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pricing_engine import (  # noqa: E402
    price_hospital,
    round_half_up_cents,
    build_cumulative_index,
)

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_DIR = os.path.join(SRC_DIR, "..", "data", "contract_rules")


def load_rules(hospital: str) -> dict:
    with open(os.path.join(RULES_DIR, f"rules_{hospital}.json")) as f:
        return json.load(f)


def line(
    line_id,
    invoice_id,
    patient,
    svc_date,
    service,
    qty,
    unit_price,
    facility="F-MAIN",
    tier="STANDARD",
    unit_basis=None,
    line_total=None,
):
    return {
        "line_id": line_id,
        "invoice_id": invoice_id,
        "patient_id": patient,
        "service_date": svc_date,
        "matched_service": service,
        "quantity": qty,
        "unit_price_cents": unit_price,
        "line_total_cents": (
            line_total if line_total is not None else unit_price * qty
        ),
        "facility_code": facility,
        "plan_tier": tier,
        "unit_basis_as_billed": unit_basis,
    }


def by_id(results) -> dict:
    return {r.line_id: r for r in results}


class TestRounding(unittest.TestCase):
    """s3.1: 'rounded to the nearest whole cent, with exact halves rounded
    away from zero ("half up")'."""

    def test_half_rounds_away_from_zero(self):
        self.assertEqual(round_half_up_cents(Decimal("100.5")), 101)
        self.assertEqual(round_half_up_cents(Decimal("101.5")), 102)  # not banker's
        self.assertEqual(round_half_up_cents(Decimal("100.4")), 100)
        self.assertEqual(round_half_up_cents(Decimal("100.6")), 101)

    def test_no_float_error(self):
        # 3375 * 1.1 in binary float is 3712.5000000000005, which would round
        # to 3713 either way; the Decimal path must give an exact 3712.5 -> 3713
        self.assertEqual(round_half_up_cents(Decimal("3375") * Decimal("1.1")), 3713)


class TestBaseRate(unittest.TestCase):
    def test_unadjusted_service_prices_at_base_rate(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Cardiac Recovery Room Occupancy", 3, 20000)]
        r = price_hospital(lines, rules)[0]
        self.assertEqual(r.expected_unit_price_cents, 20000)
        self.assertEqual(r.expected_line_total_cents, 60000)
        self.assertEqual(r.violations, [])


class TestBundling(unittest.TestCase):
    """s9.1: bundled rates replace standalone rates when BOTH services in a
    pair are delivered to the same Patient on the same Service Day."""

    def test_bundle_applies_when_both_present_same_patient_same_day(self):
        rules = load_rules("h1")
        lines = [
            line("L1", "INV1", "P1", "2024-03-05",
                 "Advanced Cardiac Recovery Room Occupancy", 1, 16400),
            line("L2", "INV1", "P1", "2024-03-05",
                 "Routine Cardiac Specimen Analysis", 1, 19150),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertEqual(res["L1"].expected_unit_price_cents, 16400)  # not 20000
        self.assertEqual(res["L2"].expected_unit_price_cents, 19150)  # not 23350

    def test_bundle_does_not_apply_when_only_one_half_present(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Cardiac Recovery Room Occupancy", 1, 20000)]
        r = price_hospital(lines, rules)[0]
        self.assertEqual(r.expected_unit_price_cents, 20000)  # standalone rate

    def test_bundle_does_not_apply_across_different_days(self):
        rules = load_rules("h1")
        lines = [
            line("L1", "INV1", "P1", "2024-03-05",
                 "Advanced Cardiac Recovery Room Occupancy", 1, 20000),
            line("L2", "INV1", "P1", "2024-03-06",
                 "Routine Cardiac Specimen Analysis", 1, 23350),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertEqual(res["L1"].expected_unit_price_cents, 20000)
        self.assertEqual(res["L2"].expected_unit_price_cents, 23350)

    def test_bundle_does_not_apply_across_different_patients(self):
        rules = load_rules("h1")
        lines = [
            line("L1", "INV1", "P1", "2024-03-05",
                 "Advanced Cardiac Recovery Room Occupancy", 1, 20000),
            line("L2", "INV2", "P2", "2024-03-05",
                 "Routine Cardiac Specimen Analysis", 1, 23350),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertEqual(res["L1"].expected_unit_price_cents, 20000)
        self.assertEqual(res["L2"].expected_unit_price_cents, 23350)


class TestThresholdPremium(unittest.TestCase):
    """s5.1: assessed against the AGGREGATE quantity for the Patient on the
    Service Day, 'not against the quantity on any one line item'.

    Ambulatory Ophthalmic Case Conference: base 16925, >6 visits -> +20%.
    """

    def test_premium_does_not_fire_at_exactly_the_threshold(self):
        # 'exceeds 6' means 6 is not enough
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Ambulatory Ophthalmic Case Conference", 6, 16925)]
        r = price_hospital(lines, rules)[0]
        self.assertEqual(r.expected_unit_price_cents, 16925)

    def test_premium_fires_above_threshold(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Ambulatory Ophthalmic Case Conference", 7, 20310)]
        r = price_hospital(lines, rules)[0]
        # 16925 * 1.20 = 20310.0 -> 20310
        self.assertEqual(r.expected_unit_price_cents, 20310)
        self.assertEqual(r.expected_line_total_cents, 20310 * 7)

    def test_premium_assessed_on_daily_aggregate_not_single_line(self):
        # Two lines of 4 each = 8 for the day, which exceeds 6, so BOTH lines
        # get the premium even though neither alone would trigger it. This is
        # the case a naive per-line implementation gets wrong.
        rules = load_rules("h1")
        lines = [
            line("L1", "INV1", "P1", "2024-03-05",
                 "Ambulatory Ophthalmic Case Conference", 4, 20310),
            line("L2", "INV1", "P1", "2024-03-05",
                 "Ambulatory Ophthalmic Case Conference", 4, 20310),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertEqual(res["L1"].expected_unit_price_cents, 20310)
        self.assertEqual(res["L2"].expected_unit_price_cents, 20310)

    def test_aggregate_is_per_day_not_across_days(self):
        rules = load_rules("h1")
        lines = [
            line("L1", "INV1", "P1", "2024-03-05",
                 "Ambulatory Ophthalmic Case Conference", 4, 16925),
            line("L2", "INV1", "P1", "2024-03-06",
                 "Ambulatory Ophthalmic Case Conference", 4, 16925),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertEqual(res["L1"].expected_unit_price_cents, 16925)
        self.assertEqual(res["L2"].expected_unit_price_cents, 16925)


class TestNonBusinessDayUplift(unittest.TestCase):
    """s2.2: Business Day = any Service Day other than Saturday or Sunday.
    Advanced Neurological Consultation: base 14125, +20% on non-business days.
    """

    def test_saturday_gets_uplift(self):
        rules = load_rules("h1")
        # 2024-03-09 is a Saturday
        lines = [line("L1", "INV1", "P1", "2024-03-09",
                      "Advanced Neurological Consultation", 1, 16950)]
        r = price_hospital(lines, rules)[0]
        self.assertEqual(r.expected_unit_price_cents, 16950)  # 14125*1.2 = 16950

    def test_sunday_gets_uplift(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-10",  # Sunday
                      "Advanced Neurological Consultation", 1, 16950)]
        self.assertEqual(price_hospital(lines, rules)[0].expected_unit_price_cents, 16950)

    def test_weekday_gets_no_uplift(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-11",  # Monday
                      "Advanced Neurological Consultation", 1, 14125)]
        self.assertEqual(price_hospital(lines, rules)[0].expected_unit_price_cents, 14125)


class TestCumulativeVolumeDiscount(unittest.TestCase):
    """s2.4 / s7.1 / s7.2: counted across the whole term, aggregated across all
    Patients, in Service Date order, UP TO BUT EXCLUDING the line being priced.
    Ties on Service Date broken by ascending line identifier.

    Comprehensive Infectious Nursing Observation: base 169825,
    >80 units -> 10%, >240 units -> 25%.
    """

    def test_cumulative_excludes_the_line_being_priced(self):
        rules = load_rules("h1")
        svc = "Comprehensive Infectious Nursing Observation"
        # First line carries 100 units. Prior utilisation is 0, so NO discount
        # applies to it even though it alone exceeds the threshold.
        lines = [line("L1", "INV1", "P1", "2024-01-05", svc, 100, 169825)]
        r = price_hospital(lines, rules)[0]
        self.assertEqual(r.expected_unit_price_cents, 169825)

    def test_discount_applies_to_the_following_line(self):
        rules = load_rules("h1")
        svc = "Comprehensive Infectious Nursing Observation"
        lines = [
            line("L1", "INV1", "P1", "2024-01-05", svc, 100, 169825),
            line("L2", "INV2", "P2", "2024-02-05", svc, 1, 152843),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertEqual(res["L1"].expected_unit_price_cents, 169825)
        # prior = 100 > 80 -> 10%: 169825 * 0.90 = 152842.5 -> 152843 (half up)
        self.assertEqual(res["L2"].expected_unit_price_cents, 152843)

    def test_aggregates_across_different_patients(self):
        rules = load_rules("h1")
        svc = "Comprehensive Infectious Nursing Observation"
        lines = [
            line("L1", "INV1", "P1", "2024-01-05", svc, 50, 169825),
            line("L2", "INV2", "P2", "2024-01-06", svc, 50, 169825),
            line("L3", "INV3", "P3", "2024-01-07", svc, 1, 152843),
        ]
        res = by_id(price_hospital(lines, rules))
        # prior for L3 = 50 + 50 = 100 > 80, across three different patients
        self.assertEqual(res["L3"].expected_unit_price_cents, 152843)

    def test_deeper_of_two_thresholds_applies(self):
        rules = load_rules("h1")
        svc = "Comprehensive Infectious Nursing Observation"
        lines = [
            line("L1", "INV1", "P1", "2024-01-05", svc, 250, 169825),
            line("L2", "INV2", "P2", "2024-02-05", svc, 1, 127369),
        ]
        res = by_id(price_hospital(lines, rules))
        # prior = 250 > 240 -> 25% (not 10%): 169825 * 0.75 = 127368.75 -> 127369
        self.assertEqual(res["L2"].expected_unit_price_cents, 127369)

    def test_same_date_ordered_by_line_id(self):
        svc = "Comprehensive Infectious Nursing Observation"
        lines = [
            line("L2", "INV1", "P1", "2024-01-05", svc, 1, 0),
            line("L1", "INV1", "P1", "2024-01-05", svc, 100, 0),
        ]
        before = build_cumulative_index(lines)
        # L1 sorts first despite being listed second
        self.assertEqual(before["L1"], 0)
        self.assertEqual(before["L2"], 100)


class TestDailyCap(unittest.TestCase):
    """s8: 'Maximum billable units per Patient per Service Day'.
    Advanced Metabolic Nursing Observation: base 130125, cap 6.
    """

    def test_within_cap_is_not_flagged(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Metabolic Nursing Observation", 6, 130125)]
        r = price_hospital(lines, rules)[0]
        self.assertNotIn("daily_cap_exceeded", r.violations)
        self.assertEqual(r.expected_line_total_cents, 130125 * 6)

    def test_over_cap_is_flagged_and_excess_not_billable(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Metabolic Nursing Observation", 9, 130125)]
        r = price_hospital(lines, rules)[0]
        self.assertIn("daily_cap_exceeded", r.violations)
        self.assertEqual(r.expected_line_total_cents, 130125 * 6)  # 6, not 9

    def test_cap_assessed_on_aggregate_across_lines(self):
        rules = load_rules("h1")
        svc = "Advanced Metabolic Nursing Observation"
        lines = [
            line("L1", "INV1", "P1", "2024-03-05", svc, 4, 130125),
            line("L2", "INV1", "P1", "2024-03-05", svc, 4, 130125),
        ]
        res = by_id(price_hospital(lines, rules))
        # 8 > 6: allocation gives L1 its full 4, L2 only the remaining 2
        self.assertIn("daily_cap_exceeded", res["L1"].violations)
        self.assertEqual(res["L1"].expected_line_total_cents, 130125 * 4)
        self.assertEqual(res["L2"].expected_line_total_cents, 130125 * 2)


class TestExclusionWindow(unittest.TestCase):
    """s10.1: measured in EITHER direction from the Service Date of the
    excluded Service. Advanced Metabolic Anaesthesia Administration is not
    billable within 7 days of Standard Endocrine Endoscopic Procedure.
    """

    def test_violation_when_billed_after_the_other_service(self):
        rules = load_rules("h1")
        lines = [
            line("L1", "INV1", "P1", "2024-03-05",
                 "Standard Endocrine Endoscopic Procedure", 1, 124450),
            line("L2", "INV1", "P1", "2024-03-08",
                 "Advanced Metabolic Anaesthesia Administration", 2, 9200),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertIn("exclusion_window_violation", res["L2"].violations)
        self.assertEqual(res["L2"].expected_line_total_cents, 0)

    def test_violation_when_billed_BEFORE_the_other_service(self):
        # bidirectional - a one-directional implementation passes the test
        # above and fails this one
        rules = load_rules("h1")
        lines = [
            line("L1", "INV1", "P1", "2024-03-12",
                 "Standard Endocrine Endoscopic Procedure", 1, 124450),
            line("L2", "INV1", "P1", "2024-03-08",
                 "Advanced Metabolic Anaesthesia Administration", 2, 9200),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertIn("exclusion_window_violation", res["L2"].violations)

    def test_no_violation_outside_the_window(self):
        rules = load_rules("h1")
        lines = [
            line("L1", "INV1", "P1", "2024-03-05",
                 "Standard Endocrine Endoscopic Procedure", 1, 124450),
            line("L2", "INV1", "P1", "2024-03-20",  # 15 days later, window is 7
                 "Advanced Metabolic Anaesthesia Administration", 2, 9200),
        ]
        res = by_id(price_hospital(lines, rules))
        self.assertNotIn("exclusion_window_violation", res["L2"].violations)


class TestMultipliersH5(unittest.TestCase):
    """H5 s3.1/s3.3: facility multiplier then plan-tier multiplier, each
    rounded half-up before the next is applied. s3.4: a multiplier of 1.0
    leaves the amount unchanged 'but the rounding step is still taken'.

    Advanced Cardiac Ventilation Support: base 3375.
    facility F-NORTH 1.1, F-COAST 0.92; tier SILVER 0.98, GOLD 0.92.
    """

    def test_facility_multiplier_alone(self):
        rules = load_rules("h5")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Cardiac Ventilation Support", 1, 3713,
                      facility="F-NORTH", tier="BRONZE")]
        r = price_hospital(lines, rules)[0]
        # 3375 * 1.1 = 3712.5 -> 3713 (half up); BRONZE is 1.0
        self.assertEqual(r.expected_unit_price_cents, 3713)

    def test_facility_then_tier_rounds_after_each_step(self):
        rules = load_rules("h5")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Cardiac Ventilation Support", 1, 3639,
                      facility="F-NORTH", tier="SILVER")]
        r = price_hospital(lines, rules)[0]
        # per-step:  3375*1.1 = 3712.5 -> 3713;  3713*0.98 = 3638.74 -> 3639
        # if rounded ONCE at the end: 3375*1.1*0.98 = 3638.25 -> 3638 (wrong)
        self.assertEqual(r.expected_unit_price_cents, 3639)

    def test_coast_and_gold(self):
        rules = load_rules("h5")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Cardiac Ventilation Support", 1, 2857,
                      facility="F-COAST", tier="GOLD")]
        r = price_hospital(lines, rules)[0]
        # 3375*0.92 = 3105.0 -> 3105;  3105*0.92 = 2856.6 -> 2857
        self.assertEqual(r.expected_unit_price_cents, 2857)


class TestContractTermWindow(unittest.TestCase):
    """s10.2 / equivalent: 'Every Service Date falls within the term'.

    Regression guard: the engine reads `effective_from_iso`/`effective_to_iso`.
    The parser originally emitted only the prose form ('1 January 2024'), so
    this check silently never fired — it raised no error and produced no
    finding. These tests fail loudly if the ISO fields go missing again.
    """

    def test_rules_files_carry_iso_term_dates(self):
        for hospital in ("h1", "h4", "h5"):
            rules = load_rules(hospital)
            self.assertIsNotNone(rules.get("effective_from_iso"),
                                 f"{hospital}: effective_from_iso missing")
            self.assertIsNotNone(rules.get("effective_to_iso"),
                                 f"{hospital}: effective_to_iso missing")

    def test_service_date_after_term_end_is_flagged(self):
        rules = load_rules("h1")  # term ends 2025-12-31
        lines = [line("L1", "INV1", "P1", "2026-07-24",
                      "Advanced Cardiac Recovery Room Occupancy", 1, 20000)]
        r = price_hospital(lines, rules)[0]
        self.assertIn("service_date_out_of_window", r.violations)

    def test_service_date_before_term_start_is_flagged(self):
        rules = load_rules("h1")  # term starts 2024-01-01
        lines = [line("L1", "INV1", "P1", "2023-11-02",
                      "Advanced Cardiac Recovery Room Occupancy", 1, 20000)]
        r = price_hospital(lines, rules)[0]
        self.assertIn("service_date_out_of_window", r.violations)

    def test_service_date_inside_term_is_not_flagged(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-06-15",
                      "Advanced Cardiac Recovery Room Occupancy", 1, 20000)]
        r = price_hospital(lines, rules)[0]
        self.assertNotIn("service_date_out_of_window", r.violations)


class TestWrongBasisOnAmbiguousLine(unittest.TestCase):
    """A line we cannot identify can still yield a certain finding.

    When every remaining candidate is contracted on the same unit basis, and
    the line was billed on a different one, the basis is wrong under every
    possibility still open — so the error holds without knowing the service.

    This is the INV-H1-000657 case: 'Procedure Immun Endosc' billed per_night,
    where both candidates are contracted per procedure. The unit-basis tiebreak
    found no survivor and the line was abandoned, when the reason it found no
    survivor WAS the error.
    """

    def _ambiguous_line(self, billed_basis):
        return {
            "line_id": "L1", "invoice_id": "INV1", "patient_id": "P1",
            "service_date": "2024-03-05", "matched_service": None,
            "match_candidates": (
                "Preoperative Immunologic Endoscopic Procedure",
                "Ambulatory Immunologic Endoscopic Procedure",
            ),
            "quantity": 1, "unit_price_cents": 151975, "line_total_cents": 151975,
            "facility_code": "F-MAIN", "plan_tier": "STANDARD",
            "unit_basis_as_billed": billed_basis,
        }

    def test_wrong_basis_reported_when_all_candidates_agree(self):
        rules = load_rules("h1")
        # both candidates are contracted 'per procedure'
        r = price_hospital([self._ambiguous_line("per_night")], rules)[0]
        self.assertIn("wrong_unit_basis", r.violations)
        self.assertFalse(r.priceable)

    def test_no_false_alarm_when_basis_is_correct(self):
        rules = load_rules("h1")
        r = price_hospital([self._ambiguous_line("per_procedure")], rules)[0]
        self.assertNotIn("wrong_unit_basis", r.violations)

    def test_ambiguous_line_is_not_called_unknown_service(self):
        rules = load_rules("h1")
        r = price_hospital([self._ambiguous_line("per_procedure")], rules)[0]
        self.assertNotIn("unknown_service", r.violations)

    def test_no_candidates_is_unknown_service(self):
        rules = load_rules("h1")
        ln = self._ambiguous_line("per_procedure")
        ln["match_candidates"] = ()
        r = price_hospital([ln], rules)[0]
        self.assertIn("unknown_service", r.violations)


class TestViolationDetection(unittest.TestCase):
    def test_unit_price_mismatch_flagged(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Cardiac Recovery Room Occupancy", 1, 99999)]
        r = price_hospital(lines, rules)[0]
        self.assertIn("unit_price_mismatch", r.violations)
        self.assertEqual(r.expected_unit_price_cents, 20000)

    def test_wrong_unit_basis_flagged(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05",
                      "Advanced Cardiac Recovery Room Occupancy", 1, 20000,
                      unit_basis="per procedure")]  # contract says 'per hour'
        r = price_hospital(lines, rules)[0]
        self.assertIn("wrong_unit_basis", r.violations)

    def test_unknown_service_flagged_and_unpriceable(self):
        rules = load_rules("h1")
        lines = [line("L1", "INV1", "P1", "2024-03-05", None, 1, 20000)]
        r = price_hospital(lines, rules)[0]
        self.assertIn("unknown_service", r.violations)
        self.assertFalse(r.priceable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
