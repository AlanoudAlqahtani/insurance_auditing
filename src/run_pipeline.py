#!/usr/bin/env python3
"""
Run the deterministic invoice-audit pipeline and reproduce the submission.

With no arguments this processes H1, H4 and H5, combines H4/H5 into
outputs/submission.csv, evaluates H1, and runs the pricing tests.

    python src/run_pipeline.py
"""

import csv
import json
import os
import sys
import subprocess
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contract_parser import parse_contract                       # noqa: E402
from pricing_engine import price_hospital                        # noqa: E402
from service_matcher import ServiceMatcher                       # noqa: E402
from validity_checks import (                                    # noqa: E402
    extract_canonical_contract_number,
    load_invoices,
    run_all_checks,
)

import evaluate as evaluate_module                               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSPITALS = [
    ("H1", "hospital_1", "provider_services_agreement.md"),
    ("H4", "hospital_4", "conditional_reimbursement_agreement.md"),
    ("H5", "hospital_5", "network_reimbursement_agreement.md"),
]
SUBMITTED_HOSPITALS = ("H4", "H5")
SUBMISSION_FIELDS = [
    "invoice_id", "flagged", "error_category",
    "expected_total_cents", "billed_total_cents", "confidence",
]


# --- confidence -----------------------------------------------------------
# Confidence describes the ROW as a whole: the flag, category set, and expected
# total together. Multiple uncertainty conditions can overlap, so confidence is
# implemented as conservative ceilings and the lowest applicable ceiling wins.
#
# The ceilings below are informed by H1 development-set row accuracy after the
# final H1 remediation patches. They are deliberately conservative; they are
# not independent class measurements and should not be read as probabilities
# estimated for mutually exclusive populations.
#
# H1 is a development set, not independent validation. In particular, these
# ceilings may not transfer unchanged to H4/H5 because their contracts and error
# mixes differ. They are the best available calibration signal, not measured
# accuracy for the scored hospitals.

CONF_HAS_VALIDITY_FINDING = 0.95
# At least one contract-independent validity finding is present. Validity checks
# can coexist with pricing/matching uncertainty, so this is a ceiling rather
# than a standalone measured class accuracy.

CONF_FULLY_PRICED = 0.95
# Baseline ceiling when the invoice has priced lines and no lower-confidence
# condition below applies.

CONF_USED_UNIT_BASIS = 0.93
# At least one otherwise ambiguous description required billed unit basis as a
# tiebreak. H1 investigation did not identify the tiebreak itself as the source
# of residual errors, but the invoice carries more identification uncertainty.

CONF_CUMULATIVE_INCOMPLETE = 0.93
# At least one price depends on cumulative utilisation known to be incomplete.
# Unsupported price-mismatch assertions are suppressed in this state.

CONF_UNRESOLVED_LINE = 0.88
# At least one line could not be identified confidently; its expected amount
# falls back to the billed amount rather than asserting an unsupported rate.


def compose_confidence(priced_lines, validity_findings) -> float:
    ceilings = []

    if validity_findings:
        ceilings.append(CONF_HAS_VALIDITY_FINDING)

    if priced_lines:
        ceilings.append(CONF_FULLY_PRICED)
        if any(not p.priceable for p in priced_lines):
            ceilings.append(CONF_UNRESOLVED_LINE)
        if any(p.cumulative_incomplete for p in priced_lines):
            ceilings.append(CONF_CUMULATIVE_INCOMPLETE)
        if any(getattr(p, "used_unit_basis", False) for p in priced_lines):
            ceilings.append(CONF_USED_UNIT_BASIS)

    return min(ceilings) if ceilings else 0.5


# --- submission rows ------------------------------------------------------

def _missing_record_key(value):
    """True for invoice-level findings whose record_key is None or pandas NaN."""
    return value is None or value != value

def build_submission_rows(priced_by_invoice, validity_by_invoice,
                          billed_totals, canonical_record_key):
    """One submission row per invoice, unioning every finding about it.

    An invoice can trip several independent checks — a malformed date AND a
    wrong rate are separate injected errors, not one cascading from the other —
    so categories are unioned and emitted pipe-delimited, matching the shape of
    `error_categories` in the labels.
    """
    rows = []
    for invoice_id in sorted(billed_totals):
        canonical_key = canonical_record_key[invoice_id]
        priced = [
            p for p in priced_by_invoice.get(invoice_id, [])
            if getattr(p, "record_key", None) == canonical_key
        ]
        # Record-specific findings come only from the canonical physical record.
        # Invoice-level findings (e.g. duplicate_invoice_id) have no record_key
        # and are intentionally preserved.
        validity = [
            f for f in validity_by_invoice.get(invoice_id, [])
            if _missing_record_key(f.get("record_key")) or f.get("record_key") == canonical_key
        ]

        categories = set()
        for f in validity:
            categories.add(f["category"])
        for p in priced:
            categories.update(p.violations)

        # Expected total. A line we could not price contributes what was
        # billed: with no contracted rate for it there is no basis to assert a
        # different figure. Checked against the hospital-1 labels, where an
        # invoice whose only fault is an uncontracted service has an expected
        # total equal to its billed total.
        # H1 labels establish that, when cross-invoice duplicate detection
        # identifies one specific offending occurrence, that occurrence is
        # non-reimbursable.  The validity finding carries the exact line ID(s).
        # Ambiguous duplicate findings intentionally carry no remediation IDs.
        duplicate_line_ids = set()
        for f in validity:
            if f.get("category") == "cross_invoice_duplicate":
                ids = f.get("remediation_line_ids")
                if isinstance(ids, (list, tuple, set)):
                    duplicate_line_ids.update(ids)

        expected = 0
        for p in priced:
            if p.line_id in duplicate_line_ids:
                continue
            if p.expected_line_total_cents is not None:
                expected += p.expected_line_total_cents
            else:
                expected += p.billed_line_total_cents

        billed = billed_totals[invoice_id]

        # `invoice_total_mismatch` means the stated invoice total disagrees with
        # its own line items. The billed figure reported is the stated total,
        # since that is what the payer would actually be asked for.
        flagged = 1 if categories else 0

        rows.append({
            "invoice_id": invoice_id,
            "flagged": flagged,
            "error_category": "|".join(sorted(categories)),
            "expected_total_cents": expected,
            "billed_total_cents": billed,
            "confidence": compose_confidence(priced, validity),
        })
    return rows



def write_submission(rows: list[dict], output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUBMISSION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def run_hospital(hospital_id: str, invoices_path: str,
                 contract_path: str, output_path: str) -> list[dict]:
    """Run the complete audit pipeline for one hospital."""
    with open(contract_path) as f:
        contract_text = f.read()

    rules_obj = parse_contract(contract_text, hospital_id)
    rules = json.loads(json.dumps(rules_obj, default=lambda o: o.__dict__))

    if rules_obj.unmapped_sections:
        print("WARNING: contract has unmapped sections; rules may be incomplete:")
        for section in rules_obj.unmapped_sections:
            print(f"  - {section}")

    records = load_invoices(invoices_path)

    records_by_invoice = defaultdict(list)
    for record_key, record in enumerate(records):
        record["_record_key"] = record_key
        records_by_invoice[record["invoice_id"]].append(record)

    # H1-derived remediation assumption, not a contract rule.
    canonical_record_key = {
        invoice_id: max(recs, key=lambda r: r["invoice_date"])["_record_key"]
        for invoice_id, recs in records_by_invoice.items()
    }

    # Validity checks.
    canonical_contract = extract_canonical_contract_number(contract_text)
    findings = run_all_checks(records, hospital_id, canonical_contract)
    validity_by_invoice = defaultdict(list)
    for finding in findings.to_dict("records") if len(findings) else []:
        validity_by_invoice[finding["invoice_id"]].append(finding)

    # Flatten line items while retaining invoice-level fields.
    lines = []
    for record in records:
        for item in record["line_items"]:
            lines.append({
                "line_id": item["line_id"],
                "invoice_id": record["invoice_id"],
                "record_key": record["_record_key"],
                "patient_id": record["patient_id"],
                "service_date": item["service_date"],
                "description": item["description"],
                "quantity": item["quantity"],
                "unit_price_cents": item["unit_price_cents"],
                "line_total_cents": item["line_total_cents"],
                "unit_basis_as_billed": item.get("unit_basis_as_billed"),
                "facility_code": record.get("facility_code"),
                "plan_tier": record.get("plan_tier"),
            })

    billed_totals = {
        invoice_id: next(
            record["invoice_total_cents"]
            for record in recs
            if record["_record_key"] == canonical_record_key[invoice_id]
        )
        for invoice_id, recs in records_by_invoice.items()
    }

    # Service matching.
    matcher = ServiceMatcher(list(rules["services"]), rules["services"])
    matches = matcher.match_all(
        [line["description"] for line in lines],
        [line["unit_basis_as_billed"] for line in lines],
    )
    for line in lines:
        match = matches[(line["description"], line["unit_basis_as_billed"])]
        line["matched_service"] = match.matched_service
        line["match_candidates"] = match.candidates

    # Contractual pricing.
    priced = price_hospital(lines, rules)
    used_basis_by_line = {
        line["line_id"]:
        matches[(line["description"], line["unit_basis_as_billed"])].used_unit_basis
        for line in lines
    }
    for priced_line in priced:
        priced_line.used_unit_basis = used_basis_by_line.get(
            priced_line.line_id, False
        )

    priced_by_invoice = defaultdict(list)
    for priced_line in priced:
        priced_by_invoice[priced_line.invoice_id].append(priced_line)

    rows = build_submission_rows(
        priced_by_invoice, validity_by_invoice,
        billed_totals, canonical_record_key,
    )
    write_submission(rows, output_path)

    flagged = sum(row["flagged"] for row in rows)
    print(f"{hospital_id}: {len(rows)} invoices, {flagged} flagged "
          f"({flagged / len(rows):.1%})")
    confidence_counts = defaultdict(int)
    for row in rows:
        confidence_counts[row["confidence"]] += 1
    print("confidence distribution:")
    for confidence in sorted(confidence_counts, reverse=True):
        print(f"  {confidence:.2f}  {confidence_counts[confidence]:5d}")
    print(f"Written to {output_path}")
    return rows


def require(path: str, description: str) -> str:
    if not os.path.exists(path):
        sys.exit(
            f"\nMissing file: {path}\n({description})\n\n"
            "Place the exercise data under data/ as described in the README "
            "'Setup' section, then run this script again."
        )
    return path


def run_configured_hospital(hospital_id: str, folder: str,
                            contract_filename: str) -> str:
    hospital_number = folder.rsplit("_", 1)[-1]
    invoices_path = require(
        os.path.join(ROOT, "data", "invoices",
                     f"hospital_{hospital_number}_invoices.jsonl"),
        f"{hospital_id} invoices",
    )
    contract_path = require(
        os.path.join(ROOT, "data", "contracts", folder, contract_filename),
        f"{hospital_id} contract",
    )
    output_path = os.path.join(ROOT, "outputs", f"submission_{hospital_id}.csv")
    print(f"\n{'=' * 60}\n{hospital_id}\n{'=' * 60}")
    run_hospital(hospital_id, invoices_path, contract_path, output_path)
    return output_path


def combine_submission(per_hospital_paths: dict[str, str]) -> str:
    rows = []
    for hospital_id in SUBMITTED_HOSPITALS:
        with open(per_hospital_paths[hospital_id]) as f:
            rows.extend(csv.DictReader(f))

    output_path = os.path.join(ROOT, "outputs", "submission.csv")
    write_submission(rows, output_path)
    print(f"\n{'=' * 60}\nCombined submission "
          f"({' + '.join(SUBMITTED_HOSPITALS)})\n{'=' * 60}")
    print(f"{len(rows)} rows -> {output_path}")
    return output_path


def score_h1(h1_submission: str) -> None:
    labels_path = require(
        os.path.join(ROOT, "data", "labels", "hospital_1_labels.csv"),
        "hospital 1 labels",
    )
    report_path = os.path.join(ROOT, "outputs", "evaluation_report_h1.md")
    labels = evaluate_module.load_labels(labels_path)
    predictions = evaluate_module.load_predictions(h1_submission)
    result = evaluate_module.evaluate(labels, predictions)
    report = evaluate_module.format_report(result)

    print(f"\n{'=' * 60}\nScoring H1 against labels\n{'=' * 60}")
    print(report)
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\nReport written to {report_path}")


def run_tests() -> None:
    print(f"\n{'=' * 60}\nRunning test suite\n{'=' * 60}")
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "src", "test_pricing_engine.py")],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if result.returncode != 0:
        sys.exit("Test suite failed - stopping before reporting results as valid.")


def main() -> None:
    paths = {
        hospital_id: run_configured_hospital(hospital_id, folder, contract_filename)
        for hospital_id, folder, contract_filename in HOSPITALS
    }
    combine_submission(paths)
    score_h1(paths["H1"])
    run_tests()

    print(f"\n{'=' * 60}\nDone\n{'=' * 60}")
    print("outputs/submission.csv           <- deliverable (H4 + H5)")
    print("outputs/submission_H1.csv        <- H1 development predictions")
    print("outputs/evaluation_report_h1.md  <- H1 evaluation metrics")


if __name__ == "__main__":
    main()
