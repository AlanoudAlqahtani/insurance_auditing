# Decision Log

This log records the material implementation decisions made during the
invoice-auditing exercise. It focuses on assumptions and trade-offs that
affect reproducibility, generalization, or interpretation of the
submitted results.

## 1. Scope and prioritization

**Decision:** Use Hospital 1 as the labeled development set and submit
Hospitals 4 and 5. Defer Hospitals 2 and 3.

**Rationale:** The assessment explicitly favors strong implementation of
fewer hospitals within the 6--8 hour time limit over thin coverage of
all hospitals. H1 provides labels for measurable development feedback.
H4/H5 were selected after checking contract-rule extraction and
service-resolution coverage.

**Limitation:** No claim is made about performance on H2/H3.

## 2. Deterministic runtime architecture

**Decision:** Separate contract interpretation from invoice execution.
Runtime auditing uses explicit structured rules and deterministic
Python; it does not call an LLM.

**Rationale:** This makes pricing behavior reproducible, testable, and
inspectable. AI assistance was used during development and
interpretation rather than as an unbounded runtime decision-maker.

**Limitation:** Any incorrect contract interpretation encoded in the
structured rules can propagate deterministically.

## 3. Contract parsing and fail-visible safeguards

**Decision:** Parse supported contract terms into explicit rule families
and retain visible indicators for sections that cannot be mapped
reliably.

**Rationale:** A missing parsed rule must not silently be interpreted as
evidence that the contract contains no such rule. `unmapped_sections`
and parse notes provide an escape hatch for unsupported or suspicious
contract structures. Canonical contract number and contract-term dates
are taken from the contract rather than inferred from invoice-majority
behavior.

**Validation:** H4/H5 rule extraction was inspected for suspicious
zero-count rule families and unmapped sections before submission. This
review exposed and corrected an H4 exclusion-window parsing issue.

## 4. Conservative service resolution

**Decision:** Resolve free-text invoice descriptions using deterministic
lexical evidence with conservative thresholds (`MIN_SCORE = 0.60`,
`MIN_MARGIN = 0.08`). Do not use billed price as matching evidence.

**Rationale:** Billed price is itself an audited field and using it to
infer the service would create circular evidence. Unit basis may resolve
a description tie only when exactly one plausible candidate is
compatible. Otherwise ambiguity is retained.

**Limitation:** Conservative matching can leave services unresolved.
Those cases reduce confidence rather than being forced into unsupported
mappings.

## 5. Pricing, ordering, and rounding

**Decision:** Perform monetary calculations with `Decimal` and half-up
cent rounding, applying contract-specific pricing steps explicitly.

**Rationale:** Integer/Decimal arithmetic avoids floating-point drift
and preserves contractual rounding behavior. The engine supports base
rates, premiums, uplifts, cumulative discounts, daily caps, bundles,
exclusion windows, and H5 facility/tier multipliers.

**Assumption:** Where multiple same-day lines affect cumulative
behavior, deterministic line ordering is used. Contract-specific
sequencing is protected by regression tests.

## 6. Uncertainty and confidence

**Decision:** Confidence is derived from observable evidence quality
rather than assigned as one constant score. The final ceilings are 0.95,
0.93, and 0.88.

**Rationale:** Resolutions that depend on secondary unit-basis evidence,
incomplete cumulative history, or unresolved mappings should not receive
the same confidence as fully supported deterministic cases.

**Limitation:** These confidence levels were informed by H1 development
behavior; they are not held-out probability estimates for H4/H5.

## 7. Duplicate invoice reconciliation

**Decision:** Preserve physical-record identity with `record_key`. When
multiple physical records share one invoice ID, use the record with the
latest invoice date as the canonical submitted record while retaining
physical lines where pricing history may depend on them.

**Rationale:** H1 labels supported latest-date canonicalization for the
observed duplicate IDs, while removing noncanonical physical lines
entirely could alter cumulative pricing history.

**Limitation:** Latest-date canonicalization is an H1-informed transfer
assumption rather than an explicit universal contract rule.

## 8. Cross-invoice duplicate remediation

**Decision:** A cross-invoice duplicate may affect the expected monetary
total only when the offending occurrence can be identified
unambiguously. Otherwise the duplicate is flagged without inventing a
monetary correction.

**Rationale:** The contracts support identifying duplicate billing as
invalid, but do not provide a general rule for arbitrarily choosing
which ambiguous occurrence should be removed. `remediation_line_ids`
therefore carries a correction only when evidence identifies the
offending line.

**Limitation:** Ambiguous duplicate cases can remain monetarily
unreconciled even when the error category is correctly detected.

## Final validation

After the final refactor, the implementation reproduced the frozen H1
development results:

-   913 unique H1 invoices; 58 flagged.
-   Flag precision: 1.000.
-   Flag recall: 1.000.
-   Mean category Jaccard: 0.746.
-   Exact category-set match: 0.603.
-   Expected-total exact match: 0.897.
-   Expected-total MAE: 6,316.6 cents.
-   Full discovered regression suite: 43/43 tests passed.

The scored submission contains 835 H4 rows and 1,050 H5 rows, for
**1,885 rows total**, with **139 flagged invoices**. H4/H5 are
unlabeled, so these counts are not accuracy measurements.
