#!/usr/bin/env python3
"""
M3 (deterministic path) — parse markdown-table contracts into the unified schema.

Covers hospitals whose contracts present rules as markdown pipe tables:
H1 (provider_services_agreement.md), H4 (conditional_reimbursement_agreement.md),
H5 (network_reimbursement_agreement.md).

NOT for H2 (prose, no tables — needs LLM extraction) or H3 (whitespace-aligned
text tables across 3 documents — needs its own parser plus amendment handling).

Key design point: section NUMBERS and TITLES differ across hospitals
(H1 calls caps "8. Daily Quantity Caps", H4 calls them "6. Daily Quantity
Limits", H5 puts base rates under "4. Table 1 — Base Rates"). So sections are
matched on keywords in the heading, not on number. Any heading that holds a
table but matches no known rule type is recorded in `unmapped_sections` rather
than silently dropped — that is the escape hatch that stops a contract with a
rule we didn't anticipate from being quietly mispriced.

Usage:
    python contract_parser.py --contract path/to/contract.md --hospital-id H1
    python contract_parser.py --contract ... --hospital-id H1 --output rules_h1.json
"""

import argparse
import json
import re
from datetime import datetime
from dataclasses import asdict, dataclass, field


# --- money / number parsing -------------------------------------------------

def parse_money_to_cents(text: str) -> int | None:
    """'GBP 1,301.25' -> 130125. Returns None for '—' or unparseable."""
    if not text or text.strip() in {"—", "-", ""}:
        return None
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not cleaned:
        return None
    # exact integer cents, no float rounding surprises
    if "." in cleaned:
        whole, frac = cleaned.split(".", 1)
        frac = (frac + "00")[:2]
        return int(whole) * 100 + int(frac)
    return int(cleaned) * 100


def parse_quantity(text: str) -> int | None:
    """'6 days' -> 6. 'more than 100 procedures' -> 100. '—' -> None."""
    if not text or text.strip() in {"—", "-", ""}:
        return None
    m = re.search(r"(\d+)", text.replace(",", ""))
    return int(m.group(1)) if m else None


def parse_percent(text: str) -> float | None:
    """'+20%' -> 0.20. '10%' -> 0.10. Returns the fractional adjustment."""
    if not text or text.strip() in {"—", "-", ""}:
        return None
    m = re.search(r"([\d.]+)\s*%", text.replace(",", ""))
    return float(m.group(1)) / 100.0 if m else None


def parse_multiplier(text: str) -> float | None:
    """'1.15' -> 1.15. Used for facility/tier multiplier tables."""
    if not text or text.strip() in {"—", "-", ""}:
        return None
    m = re.search(r"([\d.]+)", text.replace(",", ""))
    return float(m.group(1)) if m else None


# --- markdown structure -----------------------------------------------------

@dataclass
class MarkdownTable:
    heading: str
    headers: list[str]
    rows: list[list[str]]


def split_into_sections(text: str) -> list[tuple[str, str]]:
    """Return [(heading, body), ...] split on markdown '##' AND '###' headings.

    Sub-headings matter: H5 puts its facility-multiplier and tier-multiplier
    tables under '### Table 2 —' / '### Table 3 —' nested inside the '## 4.
    Table 1 — Base Rates' section. Splitting on '##' alone swallowed them into
    the parent heading, so they classified as unknown. Splitting on both levels
    gives every table its own nearest heading for classification.
    """
    parts = re.split(r"^#{2,3}\s+(.+)$", text, flags=re.MULTILINE)
    sections = [("__preamble__", parts[0])]
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i].strip(), parts[i + 1]))
    return sections


def extract_tables(heading: str, body: str) -> list[MarkdownTable]:
    """Pull every pipe table out of a section body."""
    tables = []
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|") and i + 1 < len(lines) and re.match(
            r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]
        ):
            headers = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                if len(cells) == len(headers):
                    rows.append(cells)
                j += 1
            tables.append(MarkdownTable(heading=heading, headers=headers, rows=rows))
            i = j
        else:
            i += 1
    return tables


# --- unified schema ---------------------------------------------------------

@dataclass
class ContractRules:
    hospital_id: str
    contract_number: str | None = None
    provider: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    # ISO-normalised copies of the term dates. The header states them in prose
    # ('1 January 2024'); downstream date comparisons need real dates, so they
    # are normalised here rather than re-parsed at every use site.
    effective_from_iso: str | None = None
    effective_to_iso: str | None = None
    currency: str | None = None
    rounding_convention: str | None = None

    # service -> {"unit_basis": str, "rate_cents": int, "daily_cap": int | None}
    services: dict = field(default_factory=dict)

    # service -> {"threshold": int, "uplift": float}
    threshold_premiums: dict = field(default_factory=dict)

    # service -> float
    non_business_day_uplifts: dict = field(default_factory=dict)

    # service -> [{"threshold": int, "discount": float}, ...] sorted deepest-last
    volume_discounts: dict = field(default_factory=dict)

    # service -> int (max units per patient per service day)
    daily_caps: dict = field(default_factory=dict)

    # [{"service_a","service_b","rate_a_cents","rate_b_cents"}, ...]
    bundles: list = field(default_factory=list)

    # [{"service","window_days","relative_to_service"}, ...]
    exclusion_windows: list = field(default_factory=list)

    # "service||facility_code" -> float  (H5 only; empty means all 1.0)
    facility_multipliers: dict = field(default_factory=dict)

    # "service||tier" -> float  (H5 only; empty means all 1.0)
    tier_multipliers: dict = field(default_factory=dict)

    # headings holding tables we could not map to a known rule type
    unmapped_sections: list = field(default_factory=list)

    # per-field notes for the decision log / confidence composition
    parse_notes: list = field(default_factory=list)


# --- header metadata --------------------------------------------------------

HEADER_FIELDS = {
    "contract_number": r"Contract number:\**\s*([A-Z0-9\-]+)",
    "provider": r"Provider:\**\s*(.+)",
    "effective_from": r"Effective from:\**\s*(.+)",
    "effective_to": r"Effective to:\**\s*(.+)",
    "currency": r"Currency:\**\s*([A-Z]{3})",
    "rounding_convention": r"Rounding convention:\**\s*(\S+)",
}


def normalise_date(text: str | None) -> str | None:
    """'1 January 2024' -> '2024-01-01'. Returns None if unparseable, so a
    failure surfaces as a missing field rather than a wrong date."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_header(text: str, rules: ContractRules) -> None:
    head = text[:2000]
    for field_name, pattern in HEADER_FIELDS.items():
        m = re.search(pattern, head, re.IGNORECASE)
        if m:
            setattr(rules, field_name, m.group(1).strip().rstrip("*").strip())

    rules.effective_from_iso = normalise_date(rules.effective_from)
    rules.effective_to_iso = normalise_date(rules.effective_to)
    if rules.effective_from and not rules.effective_from_iso:
        rules.parse_notes.append(
            f"could not normalise effective_from: {rules.effective_from!r}")
    if rules.effective_to and not rules.effective_to_iso:
        rules.parse_notes.append(
            f"could not normalise effective_to: {rules.effective_to!r}")


# --- table classification ---------------------------------------------------
# Matched on keywords in the heading + column headers, NOT on section number,
# because numbering differs across hospitals.

def classify_table(table: MarkdownTable) -> str:
    h = table.heading.lower()
    cols = " ".join(table.headers).lower()

    if "facility" in cols and "code" in cols and len(table.headers) == 2:
        return "facility_list"          # F-MAIN -> Main Campus, not a rule
    if "plan tier" in cols and len(table.headers) == 2:
        return "tier_list"

    # multiplier tables: service rows, one column per facility/tier
    if "table 2" in h or ("facility" in h and "multiplier" in h):
        return "facility_multipliers"
    if "table 3" in h or ("tier" in h and "multiplier" in h):
        return "tier_multipliers"

    if "bundle" in h:
        return "bundles"
    if "exclusion" in h:
        return "exclusion_windows"
    if "business" in h and ("uplift" in h or "day" in h):
        return "non_business_day_uplifts"
    if "premium" in h or ("threshold" in h and "premium" in h):
        return "threshold_premiums"
    if "volume" in h or ("discount" in h and "cumulative" in cols) or "discount" in h:
        return "volume_discounts"
    if "cap" in h or "limit" in h or "quantity" in h:
        return "daily_caps"
    # base-rate table: has a rate column plus a unit-basis column
    if ("rate" in cols or "base rate" in cols) and "unit basis" in cols:
        return "base_rates"

    return "unknown"


def col_index(headers: list[str], *keywords: str) -> int | None:
    """Find the first column whose header contains all keywords."""
    for i, h in enumerate(headers):
        low = h.lower()
        if all(k in low for k in keywords):
            return i
    return None


def first_column(headers: list[str], *keyword_groups: tuple[str, ...]) -> int | None:
    """Return the first matching column index, including index 0."""
    for keywords in keyword_groups:
        index = col_index(headers, *keywords)
        if index is not None:
            return index
    return None


# --- ingest each table type -------------------------------------------------

def ingest_base_rates(t: MarkdownTable, rules: ContractRules) -> None:
    i_svc = col_index(t.headers, "service")
    if i_svc is None:
        i_svc = 0
    i_unit = col_index(t.headers, "unit")
    i_rate = col_index(t.headers, "rate")
    i_cap = col_index(t.headers, "cap")

    for row in t.rows:
        service = row[i_svc]
        rate = parse_money_to_cents(row[i_rate]) if i_rate is not None else None
        if rate is None:
            rules.parse_notes.append(f"base_rates: no rate parsed for {service!r}")
            continue
        entry = {
            "unit_basis": row[i_unit] if i_unit is not None else None,
            "rate_cents": rate,
            "daily_cap": parse_quantity(row[i_cap]) if i_cap is not None else None,
        }
        rules.services[service] = entry
        # a cap stated inline in the rate table is the same rule as a dedicated
        # cap table; record it in both places so downstream only reads one
        if entry["daily_cap"] is not None:
            rules.daily_caps[service] = entry["daily_cap"]


def ingest_threshold_premiums(t: MarkdownTable, rules: ContractRules) -> None:
    i_svc = col_index(t.headers, "service")
    if i_svc is None:
        i_svc = 0
    i_thr = first_column(
        t.headers, ("threshold",), ("exceeds",), ("quantity",)
    )
    i_up = first_column(t.headers, ("uplift",), ("premium",))

    for row in t.rows:
        service = row[i_svc]
        threshold = parse_quantity(row[i_thr]) if i_thr is not None else None
        uplift = parse_percent(row[i_up]) if i_up is not None else None
        if threshold is None or uplift is None:
            rules.parse_notes.append(f"threshold_premiums: incomplete row for {service!r}")
            continue
        rules.threshold_premiums[service] = {"threshold": threshold, "uplift": uplift}


def ingest_non_business_day(t: MarkdownTable, rules: ContractRules) -> None:
    i_svc = col_index(t.headers, "service")
    if i_svc is None:
        i_svc = 0
    i_up = col_index(t.headers, "uplift")
    if i_up is None and len(t.headers) == 2:
        i_up = 1
    for row in t.rows:
        pct = parse_percent(row[i_up]) if i_up is not None else None
        if pct is None:
            rules.parse_notes.append(f"non_business_day: no uplift for {row[i_svc]!r}")
            continue
        rules.non_business_day_uplifts[row[i_svc]] = pct


def ingest_volume_discounts(t: MarkdownTable, rules: ContractRules) -> None:
    i_svc = col_index(t.headers, "service")
    if i_svc is None:
        i_svc = 0
    i_thr = first_column(
        t.headers, ("cumulative",), ("utilisation",), ("exceeds",)
    )
    i_disc = col_index(t.headers, "discount")

    for row in t.rows:
        service = row[i_svc]
        threshold = parse_quantity(row[i_thr]) if i_thr is not None else None
        discount = parse_percent(row[i_disc]) if i_disc is not None else None
        if threshold is None or discount is None:
            rules.parse_notes.append(f"volume_discounts: incomplete row for {service!r}")
            continue
        # a service can have several tiers; keep them all, sorted by threshold
        rules.volume_discounts.setdefault(service, []).append(
            {"threshold": threshold, "discount": discount}
        )
    for service in rules.volume_discounts:
        rules.volume_discounts[service].sort(key=lambda d: d["threshold"])


def ingest_daily_caps(t: MarkdownTable, rules: ContractRules) -> None:
    i_svc = col_index(t.headers, "service")
    if i_svc is None:
        i_svc = 0
    i_max = first_column(
        t.headers, ("maximum",), ("max",), ("cap",), ("limit",)
    )
    if i_max is None and len(t.headers) == 2:
        i_max = 1
    for row in t.rows:
        cap = parse_quantity(row[i_max]) if i_max is not None else None
        if cap is None:
            rules.parse_notes.append(f"daily_caps: no cap parsed for {row[i_svc]!r}")
            continue
        rules.daily_caps[row[i_svc]] = cap


def ingest_bundles(t: MarkdownTable, rules: ContractRules) -> None:
    i_a = col_index(t.headers, "service a")
    i_b = col_index(t.headers, "service b")
    i_ra = first_column(t.headers, ("bundled rate a",), ("rate a",))
    i_rb = first_column(t.headers, ("bundled rate b",), ("rate b",))
    if None in (i_a, i_b, i_ra, i_rb):
        rules.parse_notes.append(f"bundles: unexpected columns {t.headers}")
        return
    for row in t.rows:
        rules.bundles.append({
            "service_a": row[i_a],
            "service_b": row[i_b],
            "rate_a_cents": parse_money_to_cents(row[i_ra]),
            "rate_b_cents": parse_money_to_cents(row[i_rb]),
        })


def ingest_exclusion_windows(t: MarkdownTable, rules: ContractRules) -> None:
    i_svc = col_index(t.headers, "service")
    if i_svc is None:
        i_svc = 0
    # The window column is headed differently per contract: H1 'Not billable
    # within', H4 'Window', H5 'Within'. Matching only on 'within' left H4's
    # windows with window_days=None, and the pricing engine then skipped all 15
    # of them silently — no error, no findings, indistinguishable from a
    # contract with no exclusions.
    i_win = first_column(
        t.headers, ("within",), ("window",), ("days",)
    )
    i_of = first_column(t.headers, ("of this",), ("excluded by",))
    if i_of is None:
        i_of = len(t.headers) - 1
    for row in t.rows:
        window_days = parse_quantity(row[i_win]) if i_win is not None else None
        if window_days is None:
            rules.parse_notes.append(
                f"exclusion_windows: could not read a window length for "
                f"{row[i_svc]!r} (columns {t.headers}) - this rule will NOT be enforced")
        rules.exclusion_windows.append({
            "service": row[i_svc],
            "window_days": window_days,
            "relative_to_service": row[i_of],
        })


def ingest_multiplier_table(t: MarkdownTable, rules: ContractRules, kind: str) -> None:
    """Service rows, one column per facility code (or per plan tier)."""
    i_svc = col_index(t.headers, "service")
    if i_svc is None:
        i_svc = 0
    target = rules.facility_multipliers if kind == "facility" else rules.tier_multipliers
    for row in t.rows:
        service = row[i_svc]
        for i, header in enumerate(t.headers):
            if i == i_svc:
                continue
            mult = parse_multiplier(row[i])
            if mult is None:
                rules.parse_notes.append(
                    f"{kind}_multipliers: unparseable {row[i]!r} for {service!r}/{header!r}")
                continue
            target[f"{service}||{header.strip()}"] = mult


INGESTORS = {
    "base_rates": ingest_base_rates,
    "threshold_premiums": ingest_threshold_premiums,
    "non_business_day_uplifts": ingest_non_business_day,
    "volume_discounts": ingest_volume_discounts,
    "daily_caps": ingest_daily_caps,
    "bundles": ingest_bundles,
    "exclusion_windows": ingest_exclusion_windows,
}


def parse_contract(text: str, hospital_id: str) -> ContractRules:
    rules = ContractRules(hospital_id=hospital_id)
    parse_header(text, rules)

    for heading, body in split_into_sections(text):
        for table in extract_tables(heading, body):
            kind = classify_table(table)

            if kind in ("facility_list", "tier_list"):
                continue  # reference lists, not pricing rules
            if kind == "facility_multipliers":
                ingest_multiplier_table(table, rules, "facility")
            elif kind == "tier_multipliers":
                ingest_multiplier_table(table, rules, "tier")
            elif kind in INGESTORS:
                INGESTORS[kind](table, rules)
            else:
                rules.unmapped_sections.append(
                    f"{heading} | columns={table.headers} | {len(table.rows)} rows")

    return rules


def summarise(rules: ContractRules) -> str:
    lines = [
        f"hospital_id:        {rules.hospital_id}",
        f"contract_number:    {rules.contract_number}",
        f"provider:           {rules.provider}",
        f"term:               {rules.effective_from} -> {rules.effective_to}",
        f"currency / rounding:{rules.currency} / {rules.rounding_convention}",
        "",
        f"services (base rates):     {len(rules.services)}",
        f"threshold premiums:        {len(rules.threshold_premiums)}",
        f"non-business-day uplifts:  {len(rules.non_business_day_uplifts)}",
        f"volume discount services:  {len(rules.volume_discounts)}"
        f" ({sum(len(v) for v in rules.volume_discounts.values())} tiers)",
        f"daily caps:                {len(rules.daily_caps)}",
        f"bundled pairs:             {len(rules.bundles)}",
        f"exclusion windows:         {len(rules.exclusion_windows)}",
        f"facility multipliers:      {len(rules.facility_multipliers)}",
        f"tier multipliers:          {len(rules.tier_multipliers)}",
    ]
    if rules.unmapped_sections:
        lines.append("")
        lines.append("UNMAPPED SECTIONS (read these before trusting the extraction):")
        for s in rules.unmapped_sections:
            lines.append(f"  - {s}")
    if rules.parse_notes:
        lines.append("")
        lines.append(f"parse notes ({len(rules.parse_notes)}):")
        for n in rules.parse_notes[:10]:
            lines.append(f"  - {n}")
        if len(rules.parse_notes) > 10:
            lines.append(f"  ... and {len(rules.parse_notes) - 10} more")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--hospital-id", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    with open(args.contract) as f:
        text = f.read()

    rules = parse_contract(text, args.hospital_id)
    print(summarise(rules))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(asdict(rules), f, indent=2)
        print(f"\nWritten to {args.output}")


if __name__ == "__main__":
    main()
