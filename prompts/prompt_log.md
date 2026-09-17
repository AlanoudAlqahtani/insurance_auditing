# AI Prompt Log

This log records the main AI-assisted development prompts used during
the invoice-auditing exercise. It is intentionally curated rather than a
verbatim chat transcript: repeated debugging exchanges and minor wording
iterations are omitted, while material changes in reasoning,
implementation, and validation are retained.

## 1. Problem framing and prioritization

**Prompt**

Review the assessment brief and break down what must be delivered within
the 6--8 hour time limit. Identify what is scored, what can be deferred,
and which parts require the strongest evidence of correctness.

**Outcome**

The work was scoped around a strong Hospital 1 development
implementation and two scored hospitals rather than thin coverage of
every hospital. Hospital 1 was used for measurable development feedback;
Hospitals 4 and 5 were selected for the final submission. Hospitals 2
and 3 were deferred.

------------------------------------------------------------------------

## 2. Architecture boundary

**Prompt**

Design an architecture for auditing invoices where contract language
needs interpretation but the final pipeline should be reproducible.
Compare using an LLM at runtime with using AI during development to
convert contracts into explicit structured rules.

**Outcome**

The selected architecture separated interpretation from execution: AI
assistance was used during development and contract analysis, while
runtime auditing remained deterministic Python over explicit structured
rules. No external LLM/API call is required to reproduce the submission.

------------------------------------------------------------------------

## 3. Contract rule schema

**Prompt**

Define a structured representation that can capture the contract terms
needed for invoice validation: services and rates, unit bases, premiums,
uplifts, cumulative volume discounts, daily caps, bundles, exclusion
windows, facility multipliers, tier multipliers, contract dates, and
rounding rules.

**Outcome**

A normalized rules structure was established so the pricing engine could
operate on explicit terms rather than raw contract prose.

------------------------------------------------------------------------

## 4. Contract parsing

**Prompt**

Implement and review contract parsing for differently formatted contract
documents. Make parsing fail visibly when a section cannot be
interpreted instead of silently assuming that no rule exists.

**Outcome**

The parser extracts the supported rule families and retains
`unmapped_sections` / parse notes as safeguards. Later review also fixed
column-selection logic so a valid column at index 0 could not be
discarded by Python truthiness.

------------------------------------------------------------------------

## 5. Pipeline decomposition

**Prompt**

Break the audit workflow into readable modules with clear
responsibilities: contract parsing, validity checks, service matching,
pricing, invoice reconciliation, evaluation, and orchestration.

**Outcome**

The implementation was separated into `contract_parser.py`,
`validity_checks.py`, `service_matcher.py`, `pricing_engine.py`,
`evaluate.py`, and `run_pipeline.py`. The final cleanup consolidated
orchestration into one reproducible entry point.

------------------------------------------------------------------------

## 6. Invoice validity checks

**Prompt**

Implement contract-independent and invoice-level checks such as
malformed dates, line arithmetic, invoice-total reconciliation,
duplicate invoice IDs, cross-invoice duplicate services, contract-number
mismatch, and service-date validity. Keep distinct failure categories
where they represent different conditions.

**Outcome**

Validity checks were kept separate from contractual pricing. In
particular, `service_date_after_invoice_date` and
`service_date_out_of_window` remained distinct categories.

------------------------------------------------------------------------

## 7. Service matching

**Prompt**

Design a conservative matcher from free-text billed descriptions to
canonical contract services. It must not use billed price as matching
evidence. Prefer deterministic lexical evidence, preserve ambiguity, and
avoid forcing weak matches.

**Outcome**

The matcher uses normalized lexical similarity with conservative score
and margin thresholds. It supports explicit semantic aliases, including
ENT terminology, and retains unresolved candidates rather than guessing.

------------------------------------------------------------------------

## 8. Unit-basis evidence and ambiguity

**Prompt**

Use `unit_basis_as_billed` only as a tie-break when description matching
leaves multiple plausible services. If exactly one candidate is
compatible, resolve to it; otherwise preserve ambiguity. Distinguish a
true unknown service from an ambiguous description or wrong unit basis.

**Outcome**

Unit basis became secondary matching evidence rather than a primary
shortcut. Ambiguous matches are not automatically labeled
`unknown_service`, and wrong-basis findings require sufficient candidate
evidence.

------------------------------------------------------------------------

## 9. Deterministic pricing engine

**Prompt**

Implement pricing from structured contract rules using integer cents /
Decimal arithmetic and the contract's rounding convention. Cover base
rates, premiums, non-business-day uplifts, volume discounts, daily caps,
bundles, exclusions, and facility/tier multipliers.

**Outcome**

Pricing uses deterministic `Decimal` arithmetic with half-up cent
rounding. Contract-specific ordering and per-step rounding are
explicitly tested, including Hospital 5 multiplier behavior.

------------------------------------------------------------------------

## 10. Hospital 1 evaluation loop

**Prompt**

Evaluate Hospital 1 predictions against the provided labels. Measure
binary flag precision/recall, multi-label category overlap, exact
category-set accuracy, expected-amount accuracy, MAE, and confidence
calibration. Use the results to identify systematic errors rather than
patching individual invoices.

**Outcome**

Hospital 1 became the development benchmark. Iterations focused on
general failure modes such as duplicate reconciliation and diagnostic
specificity rather than hard-coded invoice exceptions.

------------------------------------------------------------------------

## 11. Hospital prioritization

**Prompt**

Given the time limit and the assessment instruction that two hospitals
done well are better than four done thinly, compare the remaining
hospitals by contract complexity, parse completeness, and implementation
reuse. Select the strongest pair for scored submission.

**Outcome**

Hospitals 4 and 5 were selected. Their rule extraction and
service-matching coverage were validated before generating the final
scored submission.

------------------------------------------------------------------------

## 12. Generalization checks for H4 and H5

**Prompt**

Before trusting transfer from Hospital 1, inspect Hospital 4 and
Hospital 5 rule extraction and matcher coverage. Check for unmapped
contract sections, unexpected zero-count rule families, unresolved
descriptions, and contract-specific multipliers or exclusions.

**Outcome**

This check caught a Hospital 4 exclusion-window parsing issue and
prompted investigation rather than accepting a suspicious zero count.
Final parsing reported no unmapped sections for H4/H5, with high
service-matching coverage.

------------------------------------------------------------------------

## 13. Duplicate invoice IDs

**Prompt**

Investigate Hospital 1 duplicate invoice IDs. Determine how labels treat
multiple physical records sharing one invoice ID and implement a
defensible reconciliation rule without deleting historical lines needed
by cumulative pricing.

**Outcome**

Physical records received `record_key` identity. The latest invoice-date
record was used as the canonical submitted record, while physical
records remained available to pricing prepasses where history could
matter. This was treated as an H1-informed transfer assumption rather
than a universally established contract rule.

------------------------------------------------------------------------

## 14. Cross-invoice duplicates

**Prompt**

Investigate cross-invoice duplicate findings where the invoice is
correctly flagged but expected totals remain wrong. Only remove a
duplicated line from expected aggregation when the offending occurrence
can be identified unambiguously; otherwise flag the ambiguity without
inventing a monetary correction.

**Outcome**

Validity findings can carry `remediation_line_ids`. Expected totals
exclude a duplicate only when the offending occurrence is identifiable;
ambiguous duplicate cases retain the unreconciled amount.

------------------------------------------------------------------------

## 15. Regression testing

**Prompt**

Add regression tests around contract terms and the most fragile
pricing/reconciliation assumptions. Prioritize rounding, thresholds,
cumulative history, caps, bundles, exclusion windows, multipliers,
unknown/ambiguous services, duplicate invoice IDs, and cross-invoice
remediation.

**Outcome**

The final repository contains pricing-engine and pipeline-level
regression tests. The completed local suite passed 43/43 tests.

------------------------------------------------------------------------

## 16. Confidence calibration

**Prompt**

Assign confidence from observable uncertainty rather than using one
constant score. Confidence should decrease for unresolved service
mappings, unit-basis-assisted resolutions, incomplete cumulative
history, and other assumptions that weaken certainty. Validate the
buckets on H1.

**Outcome**

The final pipeline uses conservative confidence ceilings of 0.95, 0.93,
and 0.88 depending on evidence quality. These are development-informed
confidence levels, not claims of held-out accuracy.

------------------------------------------------------------------------

## 17. Evaluation and error analysis

**Prompt**

Summarize Hospital 1 performance by category and identify three or four
systematic failure modes with representative examples. Focus on patterns
such as generic versus specific pricing categories, cumulative/premium
attribution, residual amount reconstruction, and interacting
date/service conditions.

**Outcome**

The final H1 run produced 58/58 erroneous invoices detected, precision
1.000, recall 1.000, mean category Jaccard 0.746, exact category-set
rate 0.603, expected-amount exact rate 0.897, and MAE 6,316.6 cents. The
remaining weakness is primarily diagnostic specificity rather than
binary error detection.

------------------------------------------------------------------------

## 18. Reproducibility and final submission review

**Prompt**

Refactor the repository for readability without changing frozen
behavior. Provide one obvious command that reproduces H1 evaluation,
H4/H5 predictions, the combined submission, and regression tests. Verify
final row counts and metrics after refactoring.

**Outcome**

`python src/run_pipeline.py` is the final execution path. The
post-refactor run reproduced the frozen H1 metrics, generated 835 H4
rows and 1,050 H5 rows, and produced `outputs/submission.csv` with 1,885
unique submission rows and 139 flagged invoices. The full discovered
test suite passed 43/43 tests.

------------------------------------------------------------------------

## AI Use Disclosure

AI tools were used throughout the project as development and documentation aids, with different models used for distinct stages of the work:

* **Claude Sonnet 5** — used for initial problem framing, assessment interpretation, scope prioritization, and development planning.
* **Claude Opus 5** — used to assist with code generation and implementation during development.
* **ChatGPT (GPT-5.6 Sol)** — used for post-implementation technical review, technical write-up and documentation, consolidation of the development prompt history into the prompt log, and final formatting.

The submitted auditing pipeline itself is deterministic Python. No external LLM or AI API is called during pipeline execution or required to reproduce the submitted results.
