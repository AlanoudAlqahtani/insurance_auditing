#!/usr/bin/env python3
"""
Load invoice JSONL records and run contract-independent validity checks.

The contract document is used only to obtain the canonical contract number.
All other checks operate directly on invoice and line-item data.
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime

import pandas as pd
import re


def load_invoices(path: str) -> list[dict]:
    """Load invoice records with nested line items from JSONL."""
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def parse_date_strict(s: str):
    """Parse YYYY-MM-DD, returning None for invalid calendar dates."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def check_line_total_arithmetic(record: dict) -> list[dict]:
    """quantity * unit_price_cents must equal line_total_cents, exactly."""
    findings = []
    for li in record["line_items"]:
        expected_total = li["quantity"] * li["unit_price_cents"]
        if expected_total != li["line_total_cents"]:
            findings.append({
                "invoice_id": record["invoice_id"],
                "record_key": record.get("_record_key"),
                "category": "line_total_arithmetic",
                "detail": f"line {li['line_no']}: {li['quantity']} x {li['unit_price_cents']} "
                          f"= {expected_total}, billed {li['line_total_cents']}",
                "delta_cents": li["line_total_cents"] - expected_total,
            })
    return findings


def check_invoice_total_mismatch(record: dict) -> list[dict]:
    """Sum of line totals must equal the invoice's stated total.
    Checked independently of line_total_arithmetic — the two can both
    fire on the same invoice without one causing the other."""
    line_sum = sum(li["line_total_cents"] for li in record["line_items"])
    if line_sum != record["invoice_total_cents"]:
        return [{
            "invoice_id": record["invoice_id"],
            "record_key": record.get("_record_key"),
            "category": "invoice_total_mismatch",
            "detail": f"line items sum to {line_sum}, invoice states {record['invoice_total_cents']}",
            "delta_cents": record["invoice_total_cents"] - line_sum,
        }]
    return []


def check_malformed_service_date(record: dict) -> list[dict]:
    """Covers both non-parseable strings and calendar-invalid dates."""
    findings = []
    for li in record["line_items"]:
        if parse_date_strict(li["service_date"]) is None:
            findings.append({
                "invoice_id": record["invoice_id"],
                "record_key": record.get("_record_key"),
                "category": "malformed_service_date",
                "detail": f"line {li['line_no']}: service_date={li['service_date']!r}",
                "delta_cents": None,
                "confidence": 0.95,
            })
    return findings


def check_service_date_after_invoice_date(record: dict) -> list[dict]:
    inv_date = parse_date_strict(record["invoice_date"])
    if inv_date is None:
        return []  # invoice_date itself malformed - not this check's job
    findings = []
    for li in record["line_items"]:
        svc_date = parse_date_strict(li["service_date"])
        if svc_date is not None and svc_date > inv_date:
            findings.append({
                "invoice_id": record["invoice_id"],
                "record_key": record.get("_record_key"),
                "category": "service_date_after_invoice_date",
                "detail": f"line {li['line_no']}: service_date={svc_date} > invoice_date={inv_date}",
                "delta_cents": None,
                "confidence": 0.95,
            })
    return findings


def check_duplicate_invoice_id(records: list[dict]) -> list[dict]:
    """Same invoice_id used for what are clearly two different invoices'
    worth of data (different invoice_date/totals/etc)."""
    findings = []
    by_id = defaultdict(list)
    for r in records:
        by_id[r["invoice_id"]].append(r)
    for invoice_id, recs in by_id.items():
        if len(recs) > 1:
            findings.append({
                "invoice_id": invoice_id,
                "category": "duplicate_invoice_id",
                "detail": f"invoice_id appears {len(recs)} times in source file",
                "delta_cents": None,
                "confidence": 0.95,
            })
    return findings


def extract_canonical_contract_number(contract_text: str) -> str | None:
    """Read the contract's own stated number, e.g. '**Contract number:**
    INS-H1-2024-0417'. This is the ground-truth source — see decision log:
    a majority-vote proxy over the invoices was considered and rejected,
    because it infers correctness from invoice data circularly (it assumes
    the majority is right, using the very data it's meant to check), rather
    than checking against an independent fact. Falls back to None if the
    contract text doesn't contain a parseable line, so the caller can decide
    whether to fall back to the mode-based proxy or skip the check."""
    m = re.search(r"Contract number:\**\s*([A-Z0-9\-]+)", contract_text, re.IGNORECASE)
    return m.group(1) if m else None


def check_contract_number_mismatch(records: list[dict],
                                    canonical: str | None = None) -> list[dict]:
    """Canonical contract number should come from the contract document
    itself (pass it in via `canonical`, extracted with
    extract_canonical_contract_number). If not available, falls back to the
    mode across this hospital's invoices — noted as a weaker proxy: it
    assumes whichever number appears most is correct, which is circular
    (using the invoice data to validate itself) and would silently fail if
    more than half a hospital's invoices shared the same wrong number."""
    used_fallback = canonical is None
    if canonical is None:
        counts = Counter(r["contract_number"] for r in records)
        canonical = counts.most_common(1)[0][0]

    findings = []
    for r in records:
        if r["contract_number"] != canonical:
            findings.append({
                "invoice_id": r["invoice_id"],
                "record_key": r.get("_record_key"),
                "category": "contract_number_mismatch",
                "detail": f"contract_number={r['contract_number']!r}, expected {canonical!r}"
                          + (" [canonical value from mode fallback, not contract file]" if used_fallback else ""),
                "delta_cents": None,
                "confidence": 0.95 if not used_fallback else 0.8,
            })
    return findings


def check_cross_invoice_duplicate(records: list[dict]) -> list[dict]:
    """Detect identical service lines billed on different invoice IDs.

    H1 evidence supports a remediation only when the admission/discharge-window
    check identifies exactly one offending invoice.  In that unambiguous case
    ``remediation_line_ids`` records the exact duplicated occurrence(s) so the
    orchestration layer can exclude them from the expected reimbursable total.
    Ambiguous both/neither-window cases are still flagged, but deliberately do
    not carry remediation line IDs: we do not guess which side should be zeroed.
    """
    by_id = {r["invoice_id"]: r for r in records}

    # key -> [(invoice_id, line_id, line_no), ...]
    seen = defaultdict(list)
    for r in records:
        patient = r["patient_id"]
        for li in r["line_items"]:
            key = (patient, li["service_date"], li["description"],
                   li["quantity"], li["unit_price_cents"], li["line_total_cents"])
            seen[key].append((r["invoice_id"], li["line_id"], li["line_no"]))

    findings_by_invoice = {}

    def add_finding(inv_id, detail, confidence, remediation_line_ids=None):
        """Merge multiple shared lines into one invoice-level finding."""
        if inv_id not in findings_by_invoice:
            findings_by_invoice[inv_id] = {
                "invoice_id": inv_id,
                "category": "cross_invoice_duplicate",
                "detail": detail,
                "delta_cents": None,
                "confidence": confidence,
                "remediation_line_ids": [],
            }
        finding = findings_by_invoice[inv_id]
        finding["confidence"] = min(finding["confidence"], confidence)
        if remediation_line_ids:
            finding["remediation_line_ids"] = sorted(set(
                finding["remediation_line_ids"] + list(remediation_line_ids)))

    for key, occurrences in seen.items():
        invoice_ids = {inv_id for inv_id, _, _ in occurrences}
        if len(invoice_ids) <= 1:
            continue
        service_date = key[1]

        def in_own_window(inv_id):
            rec = by_id[inv_id]
            return rec["admission_date"] <= service_date <= rec["discharge_date"]

        in_window = {inv_id: in_own_window(inv_id) for inv_id in invoice_ids}
        out_of_window = [inv_id for inv_id, ok in in_window.items() if not ok]

        if len(out_of_window) == 1:
            offender = out_of_window[0]
            offending_line_ids = [
                line_id for inv_id, line_id, _ in occurrences if inv_id == offender
            ]
            add_finding(
                offender,
                f"shares service_date {service_date} with another invoice; "
                f"falls outside this invoice's own admission window",
                0.9,
                remediation_line_ids=offending_line_ids,
            )
        else:
            # Detection remains, but reimbursement remediation is unresolved.
            for inv_id in invoice_ids:
                add_finding(
                    inv_id,
                    f"shares service_date {service_date} with another invoice; "
                    f"window check was ambiguous, both/neither side fit",
                    0.5,
                )

    return list(findings_by_invoice.values())


def run_all_checks(records: list[dict], hospital_id: str,
                    canonical_contract_number: str | None = None) -> pd.DataFrame:
    """Run every validity check and return one row per finding."""
    _ = hospital_id  # retained in the public API for hospital-scoped callers
    findings = []
    for record in records:
        findings += check_line_total_arithmetic(record)
        findings += check_invoice_total_mismatch(record)
        findings += check_malformed_service_date(record)
        findings += check_service_date_after_invoice_date(record)
    findings += check_duplicate_invoice_id(records)
    findings += check_contract_number_mismatch(records, canonical=canonical_contract_number)
    findings += check_cross_invoice_duplicate(records)
    return pd.DataFrame(findings)


def main() -> None:
    """Optional CLI for inspecting validity findings independently."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoices", required=True, help="Path to hospital_N_invoices.jsonl")
    parser.add_argument("--hospital-id", required=True, help="e.g. H1")
    parser.add_argument("--contract", default=None,
                         help="Path to the contract .md/.txt file, used to read the canonical "
                              "contract number directly rather than inferring it by majority vote")
    parser.add_argument("--output", default=None, help="Optional path to write findings CSV")
    args = parser.parse_args()

    records = load_invoices(args.invoices)

    canonical_contract_number = None
    if args.contract:
        with open(args.contract) as f:
            contract_text = f.read()
        canonical_contract_number = extract_canonical_contract_number(contract_text)
        if canonical_contract_number:
            print(f"Canonical contract number (from contract file): {canonical_contract_number}")
        else:
            print("WARNING: could not find a contract number in the contract file; "
                  "falling back to mode-based proxy.")

    findings = run_all_checks(records, args.hospital_id, canonical_contract_number)

    print(f"Loaded {len(records)} invoices for {args.hospital_id}")
    print(f"Findings: {len(findings)} rows across {findings['invoice_id'].nunique() if len(findings) else 0} invoices")
    if len(findings):
        print(findings["category"].value_counts())

    if args.output:
        findings.to_csv(args.output, index=False)
        print(f"\nWritten to {args.output}")


if __name__ == "__main__":
    main()
