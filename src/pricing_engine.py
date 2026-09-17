#!/usr/bin/env python3
"""Pricing engine.

Computes what a line item should have cost under the contract, by applying
the five-step adjustment chain that the H1, H4 and H5 contracts specify in
identical wording:

    (a) substitution of a bundled rate
    (b) the facility multiplier
    (c) the plan-tier multiplier
    (d) any premium or uplift
    (e) any cumulative volume discount

    line total = effective unit rate x billed quantity

Rounding is half-up to the nearest whole cent after each step, not once at
the end. All monetary arithmetic uses Decimal; there is no float arithmetic
on money anywhere in this module.

This module does no matching and calls no model: it takes `matched_service`
as an input, produced by the service matcher, and applies the rules produced
by the contract parser.

Three adjustments need state beyond the single line being priced:
  - threshold premiums and daily caps: aggregate quantity per
    (patient, service, day), not the quantity on any one line
  - volume discounts: running total per service across the WHOLE term,
    aggregated across all patients, in service-date order, EXCLUDING the
    line being priced
so lines are priced for a whole hospital in one pass rather than invoice by
invoice.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from service_matcher import unit_basis_compatible


# --- money -----------------------------------------------------------------

def round_half_up_cents(value: Decimal) -> int:
    """Round to a whole cent, exact halves away from zero. Contract wording:
    'rounded to the nearest whole cent, with exact halves rounded away from
    zero ("half up")'."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_decimal(x) -> Decimal:
    """Convert a JSON-sourced float (e.g. 1.1, 0.92) to Decimal without
    inheriting binary-float error: Decimal(str(1.1)) == Decimal('1.1')."""
    return Decimal(str(x))


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# --- inputs / outputs ------------------------------------------------------

@dataclass
class PricedLine:
    line_id: str
    invoice_id: str
    matched_service: str | None

    billed_unit_price_cents: int
    billed_quantity: int
    billed_line_total_cents: int

    expected_unit_price_cents: int | None = None
    expected_line_total_cents: int | None = None

    # Which contract rules fired, in order, with the running rate after each.
    # This is the audit trail: any expected figure can be walked back to the
    # clause that produced it.
    trace: list = field(default_factory=list)

    # Rule violations found while pricing (category names match the label
    # vocabulary in hospital_1_labels.csv).
    violations: list = field(default_factory=list)

    # Set when the line could not be priced at all.
    priceable: bool = True
    reason_unpriceable: str | None = None

    # True when this line's price depends on a cumulative total we know is
    # incomplete, because other lines that may belong to this service could
    # not be identified. The figure is our best effort, not a confident one.
    cumulative_incomplete: bool = False

    # Pipeline metadata used when reconciling physical duplicate records and
    # assigning confidence. These belong on the result object rather than being
    # attached dynamically by the orchestration layer.
    record_key: int | None = None
    used_unit_basis: bool = False


# --- pre-pass aggregates ---------------------------------------------------

def build_daily_aggregates(lines: list[dict]) -> dict:
    """(patient_id, service, service_date) -> total billed quantity.

    Threshold premiums and daily caps are both assessed against the aggregate
    for the patient/service/day, explicitly 'not against the quantity on any
    one line item' (H1 s5.1)."""
    aggregates = defaultdict(int)
    for line in lines:
        if line.get("matched_service"):
            key = (line["patient_id"], line["matched_service"], line["service_date"])
            aggregates[key] += line["quantity"]
    return dict(aggregates)


def build_bundle_index(lines: list[dict], rules: dict) -> dict:
    """(patient_id, service_date) -> {service: bundled_rate_cents}.

    A bundled rate replaces the standalone rate 'whenever both Services in a
    pair are delivered to the same Patient on the same Service Day' (H1 s9.1).
    Both halves of the pair must be present for either to be substituted."""
    services_present = defaultdict(set)
    for line in lines:
        if line.get("matched_service"):
            key = (line["patient_id"], line["service_date"])
            services_present[key].add(line["matched_service"])

    index = defaultdict(dict)
    for (patient, day), services in services_present.items():
        for bundle in rules.get("bundles", []):
            if bundle["service_a"] in services and bundle["service_b"] in services:
                index[(patient, day)][bundle["service_a"]] = bundle["rate_a_cents"]
                index[(patient, day)][bundle["service_b"]] = bundle["rate_b_cents"]
    return dict(index)


def build_cumulative_index(lines: list[dict]) -> dict:
    """line_id -> cumulative units of that service billed BEFORE this line.

    'counted cumulatively across the whole term and aggregated across all
    Patients ... up to but excluding the line item being priced. Where two
    line items share a Service Date they are counted in ascending order of
    line identifier.' (H1 s2.4 / s7.2)"""
    ordered_lines = sorted(
        (line for line in lines if line.get("matched_service")),
        key=lambda line: (line["service_date"], line["line_id"]),
    )
    running_total = defaultdict(int)
    units_before = {}
    for line in ordered_lines:
        service = line["matched_service"]
        units_before[line["line_id"]] = running_total[service]  # excludes this line
        running_total[service] += line["quantity"]
    return units_before


def build_patient_service_dates(lines: list[dict]) -> dict:
    """(patient_id, service) -> sorted list of dates that service was billed.
    Used for exclusion-window checks."""
    dates_by_key = defaultdict(list)
    for line in lines:
        if line.get("matched_service"):
            day = parse_date(line["service_date"])
            if day:
                dates_by_key[(line["patient_id"], line["matched_service"])].append(day)
    return {key: sorted(days) for key, days in dates_by_key.items()}


def build_cap_allocation(lines: list[dict], rules: dict, aggregates: dict) -> dict:
    """line_id -> billable quantity after applying the daily cap.

    The cap is 'maximum billable units per Patient per Service Day' — an
    aggregate limit, so when it is exceeded the excess has to be attributed
    to specific lines. Units are allocated to lines in ascending line_id
    order until the cap is exhausted; later lines are reduced. This
    allocation rule is an assumption, not stated in the contract — see
    decision log."""
    caps = rules.get("daily_caps", {})
    if not caps:
        return {}

    lines_by_group = defaultdict(list)
    for line in lines:
        service = line.get("matched_service")
        if service and service in caps:
            key = (line["patient_id"], service, line["service_date"])
            lines_by_group[key].append(line)

    allocation = {}
    for (patient, service, day), group in lines_by_group.items():
        cap = caps[service]
        if aggregates.get((patient, service, day), 0) <= cap:
            continue  # within cap, nothing to allocate
        remaining = cap
        for line in sorted(group, key=lambda line: line["line_id"]):
            billable = min(line["quantity"], remaining)
            allocation[line["line_id"]] = billable
            remaining -= billable
    return allocation


def find_incomplete_cumulative_services(lines: list[dict], rules: dict) -> set:
    """Services whose running total we know is short.

    A line we could not identify still represents units that were delivered.
    If that line might have been service X, then X's cumulative total is
    missing those units, and any volume-discount tier for X may be resolved
    wrongly.

    Each unresolved line carries the candidate services it could have been
    (set by the matcher). Any candidate with a volume discount is marked, so
    lines priced under it can be reported as uncertain instead of confident.
    """
    discounted_services = set(rules.get("volume_discounts", {}))
    incomplete = set()
    for line in lines:
        if line.get("matched_service"):
            continue
        for candidate in line.get("match_candidates", ()):
            if candidate in discounted_services:
                incomplete.add(candidate)
    return incomplete


# --- line-level helpers ------------------------------------------------------

def classify_unmatched_line(line: dict, rules: dict) -> tuple[str, list[str]]:
    """Returns (reason_unpriceable, violations) for a line with no matched
    service.

    No candidates at all means the billed service genuinely isn't in the
    contract — that is `unknown_service`. Several tied candidates means the
    service IS contracted; the description just omitted the word that would
    tell them apart, so this is NOT `unknown_service`.

    Even without knowing which candidate is correct, a violation can still
    be certain: if every remaining candidate shares one contracted unit
    basis and the line was billed on a different basis, the basis is wrong
    regardless of which candidate it turns out to be.
    """
    candidates = line.get("match_candidates") or ()
    if not candidates:
        return "no matching service in contract", ["unknown_service"]

    violations = []
    billed_basis = line.get("unit_basis_as_billed")
    candidate_bases = {
        rules["services"][candidate].get("unit_basis")
        for candidate in candidates if candidate in rules["services"]
    }
    if billed_basis and len(candidate_bases) == 1:
        only_basis = next(iter(candidate_bases))
        if only_basis and not unit_basis_compatible(billed_basis, only_basis):
            violations.append("wrong_unit_basis")

    reason = f"ambiguous between {len(candidates)} contracted services"
    return reason, violations


def find_date_violations(
    line: dict, rules: dict, service: str, patient: str,
    day_date: date | None, service_dates: dict,
) -> list[str]:
    """Contract-term and exclusion-window checks.

    An exclusion window is measured in either direction from the other
    service's date, not just after it (H1 s10.1)."""
    violations = []

    term_start = parse_date(rules.get("effective_from_iso") or "")
    term_end = parse_date(rules.get("effective_to_iso") or "")
    if day_date and term_start and term_end and not (term_start <= day_date <= term_end):
        violations.append("service_date_out_of_window")

    for window in rules.get("exclusion_windows", []):
        if window["service"] != service or not day_date or not window["window_days"]:
            continue
        other_dates = service_dates.get((patient, window["relative_to_service"]), [])
        if any(abs((day_date - other).days) <= window["window_days"] for other in other_dates):
            violations.append("exclusion_window_violation")
            break

    return violations


def apply_rate_adjustments(
    line: dict, rules: dict, service: str, patient: str, day: str,
    day_date: date | None, aggregates: dict, bundle_index: dict,
    cumulative_before: dict, incomplete_cumulative_services: set,
) -> tuple[Decimal, list[tuple], bool]:
    """Runs the five-step adjustment chain and returns
    (effective unit rate, trace entries, cumulative_incomplete).
    """
    trace = []
    rate = Decimal(rules["services"][service]["rate_cents"])

    # (a) bundled rate
    bundled_rate = bundle_index.get((patient, day), {}).get(service)
    if bundled_rate is not None:
        rate = Decimal(bundled_rate)
        trace.append(("a_bundled_rate", int(rate)))
    else:
        trace.append(("base_rate", int(rate)))

    # (b) facility multiplier
    facility_key = f"{service}||{line.get('facility_code')}"
    facility_multiplier = rules.get("facility_multipliers", {}).get(facility_key)
    if facility_multiplier is not None:
        rate = Decimal(round_half_up_cents(rate * to_decimal(facility_multiplier)))
        trace.append((f"b_facility_x{facility_multiplier}", int(rate)))

    # (c) plan-tier multiplier
    tier_key = f"{service}||{line.get('plan_tier')}"
    tier_multiplier = rules.get("tier_multipliers", {}).get(tier_key)
    if tier_multiplier is not None:
        rate = Decimal(round_half_up_cents(rate * to_decimal(tier_multiplier)))
        trace.append((f"c_tier_x{tier_multiplier}", int(rate)))

    # (d) premium / uplift — threshold premium is assessed on the
    # patient/service/day aggregate, not on this line's own quantity.
    premium = rules.get("threshold_premiums", {}).get(service)
    if premium:
        daily_quantity = aggregates.get((patient, service, day), 0)
        if daily_quantity > premium["threshold"]:
            rate = Decimal(round_half_up_cents(rate * (1 + to_decimal(premium["uplift"]))))
            trace.append((f"d_threshold_premium_+{premium['uplift']}", int(rate)))

    # Business Day = any day other than Saturday or Sunday.
    non_business_day_uplift = rules.get("non_business_day_uplifts", {}).get(service)
    if non_business_day_uplift is not None and day_date and day_date.weekday() >= 5:
        rate = Decimal(round_half_up_cents(rate * (1 + to_decimal(non_business_day_uplift))))
        trace.append((f"d_non_business_day_+{non_business_day_uplift}", int(rate)))

    # (e) cumulative volume discount — 'where two thresholds are met, the
    # deeper discount applies'.
    cumulative_incomplete = False
    tiers = rules.get("volume_discounts", {}).get(service)
    if tiers:
        if service in incomplete_cumulative_services:
            cumulative_incomplete = True
        prior_units = cumulative_before.get(line["line_id"], 0)
        applicable_tiers = [tier for tier in tiers if prior_units > tier["threshold"]]
        if applicable_tiers:
            deepest = max(applicable_tiers, key=lambda tier: tier["discount"])
            rate = Decimal(round_half_up_cents(rate * (1 - to_decimal(deepest["discount"]))))
            trace.append(
                (f"e_volume_discount_-{deepest['discount']}_prior{prior_units}", int(rate)))

    return rate, trace, cumulative_incomplete


def resolve_billable_quantity(
    line: dict, rules: dict, service: str, patient: str, day: str,
    aggregates: dict, cap_allocation: dict, is_excluded: bool,
) -> tuple[int, list[str], list[tuple]]:
    """Returns (billable_quantity, violations, trace_entries).

    A daily cap is an aggregate limit; when exceeded, the pre-computed
    allocation (see build_cap_allocation) says how much of THIS line is
    billable. An excluded line overrides the cap and is never billable.
    """
    billable_quantity = line["quantity"]
    violations = []
    trace = []

    caps = rules.get("daily_caps", {})
    if service in caps:
        daily_quantity = aggregates.get((patient, service, day), 0)
        if daily_quantity > caps[service]:
            violations.append("daily_cap_exceeded")
            billable_quantity = cap_allocation.get(line["line_id"], line["quantity"])
            trace.append((f"cap_{caps[service]}_billable_qty", billable_quantity))

    if is_excluded:
        billable_quantity = 0
        trace.append(("excluded_billable_qty", 0))

    return billable_quantity, violations, trace


def detect_pricing_violations(
    line: dict, service_rules: dict, expected_unit_price_cents: int,
    cumulative_incomplete: bool,
) -> tuple[list[str], tuple | None]:
    """Returns (violations, suppressed_price_check_trace_entry_or_None).

    A price mismatch is not reported when the cumulative running total for
    this service is known to be incomplete: the difference is as likely to
    be our own undercount as the hospital's error, and we cannot tell which.
    """
    violations = []
    suppressed_trace_entry = None

    if expected_unit_price_cents != line["unit_price_cents"]:
        if cumulative_incomplete:
            suppressed_trace_entry = ("price_check_suppressed_incomplete_cumulative", None)
        else:
            violations.append("unit_price_mismatch")

    billed_basis = line.get("unit_basis_as_billed")
    if service_rules.get("unit_basis") and billed_basis:
        if not unit_basis_compatible(billed_basis, service_rules["unit_basis"]):
            violations.append("wrong_unit_basis")

    return violations, suppressed_trace_entry


# --- orchestration -----------------------------------------------------------

def price_line(
    line: dict,
    rules: dict,
    aggregates: dict,
    bundle_index: dict,
    cumulative_before: dict,
    cap_allocation: dict,
    service_dates: dict,
    incomplete_cumulative_services: set,
) -> PricedLine:
    out = PricedLine(
        line_id=line["line_id"],
        invoice_id=line["invoice_id"],
        matched_service=line.get("matched_service"),
        billed_unit_price_cents=line["unit_price_cents"],
        billed_quantity=line["quantity"],
        billed_line_total_cents=line["line_total_cents"],
        record_key=line.get("record_key"),
    )

    service = line.get("matched_service")
    if not service:
        out.priceable = False
        out.reason_unpriceable, out.violations = classify_unmatched_line(line, rules)
        return out

    service_rules = rules["services"].get(service)
    if not service_rules:
        out.priceable = False
        out.reason_unpriceable = f"service not in contract: {service!r}"
        out.violations.append("unknown_service")
        return out

    day = line["service_date"]
    day_date = parse_date(day)
    patient = line["patient_id"]

    out.violations.extend(
        find_date_violations(line, rules, service, patient, day_date, service_dates))
    is_excluded = "exclusion_window_violation" in out.violations

    rate, adjustment_trace, cumulative_incomplete = apply_rate_adjustments(
        line, rules, service, patient, day, day_date,
        aggregates, bundle_index, cumulative_before, incomplete_cumulative_services,
    )
    out.trace.extend(adjustment_trace)
    out.cumulative_incomplete = cumulative_incomplete
    out.expected_unit_price_cents = int(rate)

    billable_quantity, cap_violations, cap_trace = resolve_billable_quantity(
        line, rules, service, patient, day, aggregates, cap_allocation, is_excluded)
    out.violations.extend(cap_violations)
    out.trace.extend(cap_trace)
    out.expected_line_total_cents = int(rate) * billable_quantity

    price_violations, suppressed_trace_entry = detect_pricing_violations(
        line, service_rules, out.expected_unit_price_cents, out.cumulative_incomplete)
    out.violations.extend(price_violations)
    if suppressed_trace_entry:
        out.trace.append(suppressed_trace_entry)

    return out


def price_hospital(lines: list[dict], rules: dict) -> list[PricedLine]:
    """Price every line item for a hospital in one pass.

    `lines` must be the hospital's COMPLETE line-item set: cumulative volume
    discounts are counted across the whole term, so pricing a subset would
    understate cumulative utilisation and silently omit discounts.
    """
    aggregates = build_daily_aggregates(lines)
    bundle_index = build_bundle_index(lines, rules)
    cumulative_before = build_cumulative_index(lines)
    cap_allocation = build_cap_allocation(lines, rules, aggregates)
    service_dates = build_patient_service_dates(lines)
    incomplete_cumulative_services = find_incomplete_cumulative_services(lines, rules)

    return [
        price_line(line, rules, aggregates, bundle_index, cumulative_before,
                  cap_allocation, service_dates, incomplete_cumulative_services)
        for line in lines
    ]