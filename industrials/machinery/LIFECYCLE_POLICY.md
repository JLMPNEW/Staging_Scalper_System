# Machinery Lifecycle Policy

## Purpose

The lifecycle policy separates three concepts that were previously conflated:

1. **Calibration cohort** controls percentile peers and remains frozen until a
   separately governed cohort re-curation.
2. **Lifecycle class** controls whether a ticker may enter an investable
   universe.
3. **Hard-event overlay** can veto investability without rewriting either the
   calibration cohort or lifecycle history.

The active production universe remains `operating_only`. Lifecycle v1 runs in
shadow until reviewed transitions and the parallel acceptance process pass.

## Classes

| Class | Mechanical entry evidence | Production right |
|---|---|---|
| `pre_commercial` | Revenue TTM below $10 million or validated noncommercial revenue | Research only |
| `commercial_emerging` | Revenue TTM at least $10 million for four distinct fiscal quarters, confidence at least 0.55 | Eligible only when every emerging risk gate passes; 2.5% maximum weight |
| `established_operating` | Revenue TTM at least $50 million for eight distinct fiscal quarters, confidence at least 0.70, complete data, listed at least two years | Full existing eligibility path |

The current thresholds are in `config.yaml` under `machinery_lifecycle`.
Profitability is scored rather than used as a lifecycle entry requirement.

## Hysteresis And Vetoes

An established ticker is a demotion candidate only after four quarters below
$25 million revenue TTM. An emerging ticker is a demotion candidate only after
four quarters below $5 million.

Accepted bankruptcy, definitive acquisition, delisting notice, filing
deficiency, and going-concern events veto investability immediately from their
reviewed `valid_from` date. Parser evidence creates a review candidate only; it
never creates an automatic veto.

## Point-In-Time Rules

- Quarter counts use distinct fiscal period ends reconstructed from canonical
  facts. Repeated daily snapshots do not increase a streak.
- Filing evidence is usable only after its SEC availability date.
- Human decisions are never retroactive: `valid_from` must be on or after
  `reviewed_at`.
- Commercial customer revenue must be explicitly ratified.
- Missing emerging-stage dependence, runway, dilution, or liquidity inputs fail
  closed.
- Accepted transitions, revenue classifications, and hard events require
  evidence hashes and record hashes.

## Operating Sequence

1. Run the dedicated parser shadow for candidate tickers when filing evidence
   needs enrichment.
2. Run `08f_generate_machinery_lifecycle_candidates.py`. Review
   `machinery_lifecycle_transition_review.csv`,
   `machinery_lifecycle_revenue_review.csv`, and
   `machinery_lifecycle_hard_event_review.csv`.
3. Fill the final decision, reason, reviewer, review timestamp, and a
   non-retroactive `valid_from`. Revenue decisions must choose
   `validated_customer_revenue` or
   `validated_noncommercial_revenue`.
4. Run `08h_apply_machinery_lifecycle_reviews.py` without `--apply`. It validates
   every decision, evidence hash, transition chain, and resulting ledger.
5. Re-run the same command with `--apply` only after the dry run passes.
6. Run `08g_validate_machinery_lifecycle_policy.py`, regenerate candidates, and
   build `10c_build_machinery_lifecycle_shadow.py`.
7. Build a PIT lifecycle-universe shadow history without modifying the frozen
   `operating_only` evidence series. Run the existing Stage 8 and Stage 9
   acceptance protocol on the new universe version.
8. Only after acceptance, explicitly version and activate the
   `production_universe.py` lifecycle switch, re-seal Stage 12, and run the full
   machinery-to-portfolio-layer smoke test.

The review command is intentionally not part of the unattended orchestrator.
Human approval is a required state transition.

## Acceptance Gates

- Policy validator passes with no broken transition chains, retroactive dates,
  missing evidence, or hash mismatches.
- Candidate generation uses complete PIT evidence and identifies all parser
  hard-event candidates for review.
- Shadow output preserves the frozen production result while reporting every
  eligibility difference.
- Calibration cohorts do not change as a side effect. A warning is emitted if a
  future cohort re-curation would leave fewer than 15 precommercial peers.
- Lifecycle-universe Stage 8 and Stage 9 tests pass under a preregistered
  specification.
- Stage 12 source lock, output contract, and portfolio-layer end-to-end smoke
  test pass before nonzero capital is assigned.

## Current 2026-07-24 State

The mechanical pass found ten transition candidates. None is automatically
accepted. Commercial revenue classification remains a required review for all
ten, emerging risk gates remain binding, and parser evidence surfaced one
going-concern review candidate for `XOS`. Until those reviews are ratified, the
shadow universe is intentionally identical to the active `operating_only`
universe.
