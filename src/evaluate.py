#!/usr/bin/env python3
"""
evaluate.py — score a submission-shaped CSV against hospital 1's labels.

Usage:
    python evaluate.py --predictions submission.csv --labels hospital_1_labels.csv
    python evaluate.py --predictions submission.csv --labels hospital_1_labels.csv --output report.md

Two DIFFERENT schemas are involved, and this script normalizes between them:

  labels file (ground truth, as actually shipped):
    invoice_id, is_erroneous, error_categories, expected_total_cents, ambiguity_sensitive
    - error_categories is PIPE-DELIMITED and multi-label (e.g. "unit_price_mismatch|volume_discount_incorrectly_applied")
    - no billed_total_cents or confidence column — ground truth doesn't need those

  predictions file (submission_template.csv format, what you produce):
    invoice_id, flagged, error_category, expected_total_cents, billed_total_cents, confidence
    - error_category is free text, your own label. May be single or pipe-delimited if you choose.

`labels` is treated as ground truth. `predictions` may omit rows present in
labels — those are scored as abstentions, not as flagged=0.

Design decisions baked into this script (see decision log for rationale):
  - category scoring is SET-based (Jaccard overlap after mapping to canonical
    categories), because ~70% of erroneous invoices in hospital 1 carry more
    than one error_categories value — single-label matching would silently
    misscore most of the erroneous set.
  - the canonical category list below was built from hospital 1's actual
    label distribution, not guessed keywords. If you see categories in your
    predictions that don't map to anything here, they fall into "unmapped"
    and are reported separately rather than silently miscounted.
  - amount tolerance is 0 cents (exact match); MAE reported separately
  - missing predictions are scored as abstentions, kept separate from flagged=0
  - confidence buckets follow CONFIDENCE_BUCKET_EDGES below; intervals are half-open except the final bucket
"""

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

LABEL_COLS = ["invoice_id", "is_erroneous", "error_categories", "expected_total_cents"]
PRED_COLS = ["invoice_id", "flagged", "error_category", "expected_total_cents",
             "billed_total_cents", "confidence"]

# --- Canonical category taxonomy -------------------------------------------
# Built directly from hospital 1's error_categories distribution (18 values,
# 913 rows). Keys are the canonical (label-side) category names. Values are
# extra free-text keywords that should map a PREDICTION's own phrasing onto
# that canonical category — the canonical key itself always matches too.
# Extend the keyword lists as you see how your own pipeline phrases things.
CANONICAL_CATEGORIES = {
    "unknown_service": ["unknown service", "unmapped service", "no matching service"],
    "wrong_unit_basis": ["unit basis", "wrong unit", "unit mismatch"],
    "unit_price_mismatch": ["unit price", "wrong rate", "rate mismatch", "incorrect rate", "incorrect price"],
    "malformed_service_date": ["malformed date", "invalid service date", "bad date"],
    "premium_incorrectly_applied": ["premium applied", "premium incorrectly", "premium shouldn't"],
    "invoice_total_mismatch": ["total mismatch", "total doesn't match", "total incorrect"],
    "line_total_arithmetic": ["arithmetic", "line total wrong", "calculation error"],
    "service_date_after_invoice_date": ["service date after invoice", "date after invoice"],
    "duplicate_invoice_id": ["duplicate invoice id", "duplicate invoice"],
    "bundle_not_applied": ["bundle not applied", "missing bundle", "bundle omitted"],
    "service_date_out_of_window": ["out of window", "date out of range", "outside contract window"],
    "contract_number_mismatch": ["contract number", "wrong contract"],
    "volume_discount_incorrectly_applied": ["volume discount incorrectly", "discount incorrectly applied", "discount shouldn't"],
    "daily_cap_exceeded": ["daily cap", "quantity limit", "quantity exceeded", "cap exceeded"],
    "exclusion_window_violation": ["exclusion window", "excluded period"],
    "cross_invoice_duplicate": ["cross invoice duplicate", "duplicate across invoices", "duplicate service"],
    "volume_discount_omitted": ["volume discount omitted", "discount missing", "discount not applied"],
    "premium_omitted": ["premium omitted", "premium missing"],
}

CONFIDENCE_BUCKET_EDGES = [0.0, 0.5, 0.7, 0.85, 0.90, 0.94, 1.0]


def parse_category_set(text) -> set:
    """Split a pipe-delimited (or single) category string into a set of
    canonical categories. Anything not recognized is kept as-is, prefixed
    'unmapped:' so it's visible in output rather than silently dropped."""
    if not isinstance(text, str) or not text.strip():
        return set()
    result = set()
    for raw in text.split("|"):
        raw = raw.strip()
        if not raw:
            continue
        low = raw.lower()
        matched = None
        # exact canonical key match first
        if low.replace(" ", "_") in CANONICAL_CATEGORIES:
            matched = low.replace(" ", "_")
        else:
            for canon, keywords in CANONICAL_CATEGORIES.items():
                if any(k in low for k in keywords):
                    matched = canon
                    break
        result.add(matched if matched else f"unmapped:{raw}")
    return result


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _bucket_label(lo: float, hi: float, is_last: bool = False) -> str:
    right = "]" if is_last else ")"
    return f"[{lo:.2f}, {hi:.2f}{right}"


def confidence_bucket(c: float) -> str:
    edges = CONFIDENCE_BUCKET_EDGES
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        is_last = i == len(edges) - 2
        if lo <= c < hi or (is_last and lo <= c <= hi):
            return _bucket_label(lo, hi, is_last)
    return "invalid"


def load_labels(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in LABEL_COLS if c not in df.columns]
    if missing:
        sys.exit(f"ERROR: labels file {path} is missing expected columns: {missing}")
    return df


def load_predictions(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in PRED_COLS if c not in df.columns]
    if missing:
        sys.exit(f"ERROR: predictions file {path} is missing expected columns: {missing}")
    return df


@dataclass
class EvalResult:
    n_labels: int = 0
    n_predicted: int = 0
    n_abstained: int = 0

    flag_precision: float = 0.0
    flag_recall: float = 0.0
    flag_tp: int = 0
    flag_fp: int = 0
    flag_fn: int = 0
    flag_tn: int = 0

    mean_category_jaccard: float = 0.0
    exact_set_match_rate: float = 0.0
    unmapped_prediction_categories: list = field(default_factory=list)

    amount_exact_match_rate: float = 0.0
    amount_mae_cents: float = 0.0

    calibration_table: pd.DataFrame = field(default_factory=pd.DataFrame)


def evaluate(labels: pd.DataFrame, preds: pd.DataFrame) -> EvalResult:
    result = EvalResult()

    labels = labels.set_index("invoice_id")
    preds = preds.set_index("invoice_id")

    result.n_labels = len(labels)
    result.n_predicted = len(preds)

    # --- missing predictions are abstentions, not flagged=0 ---
    missing_ids = labels.index.difference(preds.index)
    result.n_abstained = len(missing_ids)

    covered_ids = labels.index.intersection(preds.index)
    lab = labels.loc[covered_ids]
    pred = preds.loc[covered_ids]

    # --- Flagging: precision / recall (is_erroneous vs flagged) ---
    lab_flag = lab["is_erroneous"].astype(int)
    pred_flag = pred["flagged"].astype(int)

    tp = int(((lab_flag == 1) & (pred_flag == 1)).sum())
    fp = int(((lab_flag == 0) & (pred_flag == 1)).sum())
    fn = int(((lab_flag == 1) & (pred_flag == 0)).sum())
    tn = int(((lab_flag == 0) & (pred_flag == 0)).sum())

    result.flag_tp, result.flag_fp, result.flag_fn, result.flag_tn = tp, fp, fn, tn
    result.flag_precision = tp / (tp + fp) if (tp + fp) else float("nan")
    result.flag_recall = tp / (tp + fn) if (tp + fn) else float("nan")

    # --- Category scoring: set-based (Jaccard), on invoices both sides flag ---
    both_flagged = covered_ids[(lab_flag == 1) & (pred_flag == 1)]
    if len(both_flagged):
        lab_sets = lab.loc[both_flagged, "error_categories"].apply(parse_category_set)
        pred_sets = pred.loc[both_flagged, "error_category"].apply(parse_category_set)

        jaccards = [jaccard(l, p) for l, p in zip(lab_sets, pred_sets)]
        result.mean_category_jaccard = sum(jaccards) / len(jaccards)
        result.exact_set_match_rate = sum(l == p for l, p in zip(lab_sets, pred_sets)) / len(lab_sets)

        unmapped = set()
        for s in pred_sets:
            unmapped |= {x for x in s if x.startswith("unmapped:")}
        result.unmapped_prediction_categories = sorted(unmapped)
    else:
        result.mean_category_jaccard = float("nan")
        result.exact_set_match_rate = float("nan")

    # --- Amount accuracy (on invoices both sides call flagged=1) ---
    if len(both_flagged):
        lab_amt = lab.loc[both_flagged, "expected_total_cents"].astype(int)
        pred_amt = pred.loc[both_flagged, "expected_total_cents"].astype(int)

        result.amount_exact_match_rate = (lab_amt == pred_amt).mean()
        result.amount_mae_cents = (lab_amt - pred_amt).abs().mean()
    else:
        result.amount_exact_match_rate = float("nan")
        result.amount_mae_cents = float("nan")

    # --- Calibration: confidence bucket -> accuracy ---
    # Two views, because they answer different questions.
    #
    # FLAG calibration asks only "was the erroneous/correct call right".
    # ROW calibration asks "was the whole row right" — the flag, and where
    # flagged, the category set and the expected total too. The stated
    # confidence describes the row as a whole, so row calibration is the one to
    # read; flag calibration alone makes the pipeline look underconfident,
    # because most of the residual uncertainty sits in the expected total
    # rather than in the flag.
    pred_correct = (lab_flag == pred_flag)

    row_correct = {}
    for iid in covered_ids:
        if lab_flag[iid] != pred_flag[iid]:
            row_correct[iid] = False
        elif lab_flag[iid] == 0:
            row_correct[iid] = True          # both say clean: nothing else to check
        else:
            cat_ok = (parse_category_set(lab.loc[iid, "error_categories"]) ==
                      parse_category_set(pred.loc[iid, "error_category"]))
            amt_ok = (int(lab.loc[iid, "expected_total_cents"]) ==
                      int(pred.loc[iid, "expected_total_cents"]))
            row_correct[iid] = bool(cat_ok and amt_ok)
    row_correct = pd.Series(row_correct)

    conf = pred["confidence"].astype(float)
    cal_df = pd.DataFrame({"confidence": conf, "flag_correct": pred_correct,
                           "row_correct": row_correct})
    cal_df["bucket"] = cal_df["confidence"].apply(confidence_bucket)

    calib = (
        cal_df.groupby("bucket")
        .agg(n=("flag_correct", "size"),
             flag_accuracy=("flag_correct", "mean"),
             row_accuracy=("row_correct", "mean"))
        .reindex([
            _bucket_label(lo, hi, i == len(CONFIDENCE_BUCKET_EDGES) - 2)
            for i, (lo, hi) in enumerate(
                zip(CONFIDENCE_BUCKET_EDGES[:-1], CONFIDENCE_BUCKET_EDGES[1:])
            )
        ])
    )
    result.calibration_table = calib

    return result


def format_report(result: EvalResult) -> str:
    lines = []
    lines.append("# Evaluation report\n")
    lines.append(f"- Labeled invoices: {result.n_labels}")
    lines.append(f"- Predicted (covered) invoices: {result.n_predicted}")
    lines.append(f"- Abstained (no row submitted): {result.n_abstained}\n")

    lines.append("## Flagging performance")
    lines.append(f"- Precision: {result.flag_precision:.3f}")
    lines.append(f"- Recall: {result.flag_recall:.3f}")
    lines.append(
        f"- TP={result.flag_tp}  FP={result.flag_fp}  "
        f"FN={result.flag_fn}  TN={result.flag_tn}\n"
    )

    lines.append("## Category accuracy (set-based, on invoices both sides flagged)")
    lines.append(f"- Mean Jaccard overlap: {result.mean_category_jaccard:.3f}")
    lines.append(f"- Exact set match rate: {result.exact_set_match_rate:.3f}")
    if result.unmapped_prediction_categories:
        lines.append(
            "- Unmapped prediction categories "
            f"(add these to CANONICAL_CATEGORIES): "
            f"{result.unmapped_prediction_categories}"
        )
    lines.append("")

    lines.append("## Amount accuracy (on invoices both sides flagged)")
    lines.append(f"- Exact match rate: {result.amount_exact_match_rate:.3f}")
    lines.append(f"- Mean absolute error: {result.amount_mae_cents:.1f} cents\n")

    lines.append("## Calibration (confidence bucket -> accuracy)")
    lines.append("flag_accuracy = was the erroneous/correct call right.")
    lines.append("row_accuracy  = flag AND category AND expected total all right.")
    lines.append("")
    lines.append(result.calibration_table.to_string())
    lines.append("")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    """Parse the standalone evaluator CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--predictions",
        required=True,
        help="Path to predictions CSV (submission-template format)",
    )
    parser.add_argument(
        "--labels",
        required=True,
        help="Path to hospital 1 labels CSV",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write the Markdown metrics report",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    labels = load_labels(args.labels)
    preds = load_predictions(args.predictions)

    result = evaluate(labels, preds)
    report = format_report(result)

    print(report)
    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"\nReport written to {args.output}")


if __name__ == "__main__":
    main()
