# Med-Devices Analyst Review Workflow

The analyst review workflow is a governed exception process. It is not an ad hoc notes file and it does not change model scores.

## Production Files

- Decision source of truth: `med_devices/data/analyst_review_decisions.csv`
- Daily review queue: `output/med_devices_reports/analyst_review/med_device_analyst_review_queue_latest.csv`
- Production score audit fields: `output/med_devices_reports/med_device_daily_composite_scores.csv`
- QA gate: `output/med_devices_reports/production_qa/med_device_production_qa_latest.csv`

## Decision Schema

`analyst_review_decisions.csv` uses these columns:

- `ticker`
- `calibration_cohort`
- `review_category`
- `decision`
- `decision_reason`
- `review_owner`
- `reviewed_at`
- `expires_at`
- `active`
- `allow_portfolio_candidate_override`
- `max_position_weight_override`
- `source_reference`

Allowed decisions:

- `approve`
- `reject`
- `watchlist`
- `data_fix_needed`
- `defer`

Use `review_category = all` when a decision applies to every open review category for a ticker/cohort. Otherwise use the exact category shown in the queue, such as `high_score_blocked`, `manual_review_regulatory_risk`, `unknown_reimbursement`, `single_product_risk`, or `hard_red_flag`.

### Category And Disposition Semantics

`review_category` records the condition that triggered review. It does not determine
the analyst disposition. Decisions are independent, evidence-based judgments about
severity, persistence, and portfolio impact.

- `tier1_safety_failed` is an umbrella trigger containing heterogeneous failures. A
  valuation miss, a fundamental-quality miss, and a confirmed regulatory event do
  not require the same disposition.
- The same review category may therefore produce different decisions when the
  underlying evidence differs. The decision reason must identify that evidence.
- A category change invalidates a category-scoped decision until it is reviewed
  again. Do not silently preserve a legacy disposition by merely replacing its
  category.
- Use `review_category = all` only for an explicitly ticker-wide decision. Do not
  use it as a convenience to suppress category drift.

## Production Policy

- The scorer reads active, non-expired decisions and writes the audit fields.
- The analyst review queue shows `open`, `decided`, or `expired_decision_needs_review`.
- The production QA gate validates the decision file and score audit columns.
- `reject` and `data_fix_needed` are fail-closed and force the portfolio-candidate
  gate to zero while the matching decision is effective.
- `approve`, `watchlist`, and `defer` do not widen the portfolio-candidate gate.
- Analyst approvals cannot override model hard gates.

Audit fields written to daily scores:

- `analyst_review_decision`
- `analyst_review_reason`
- `analyst_review_owner`
- `analyst_review_expires_at`
- `analyst_portfolio_override_applied`

## Review Process

1. Run the daily med-devices pipeline.
2. Open `med_device_analyst_review_queue_latest.csv`.
3. Prioritize P1 items with high `portfolio_candidate_score`.
4. Add decisions only to `med_devices/data/analyst_review_decisions.csv`.
5. Re-run scoring/review/QA.
6. Confirm production QA passes.

P1 means high-priority review if an override or data correction is being considered. P2 means monitor, watchlist, or lower-priority data-quality review.

## Guardrails

Even after a future override phase is enabled, analyst approval must not bypass:

- inactive or delisted ticker status
- hard-red FDA or reimbursement flags
- confirmed regulatory-risk classification
- liquidity failure
- stale critical data

Applied overrides, if enabled later, must have an owner, reason, active decision, and non-expired `expires_at`.
