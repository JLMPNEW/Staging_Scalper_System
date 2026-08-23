# Consumer Defensive Completion Roadmap

## Purpose And Authority

This document is the authoritative remaining-work sequence for completing the
Consumer Defensive implementation. It consolidates the stage order in
`README.md`, `STAGE_GATES.md`, and `IMPLEMENTATION.md` into one execution path.

If this roadmap conflicts with a stage's frozen acceptance contract in
`STAGE_GATES.md`, the stage gate controls. The Stage 12 runner's explicit step
table becomes the operational authority after Stage 12 is implemented.

The implementation is Consumer Defensive-specific. It may reuse shared kernels
such as `dedicated_parser` and `factor_validation`, but it must not import or run
Technology sector scripts as its implementation path.

## Current Accepted State

| Workstream | Status | Accepted evidence |
| --- | --- | --- |
| Stages 0-5 | Complete | Production foundation, market, financial, disclosure and positioning gates are implemented. |
| Stage 6A | Implemented | Scoring-feature definition v2 is frozen. A full-stack rehearsal remains required before Stage 7. |
| Stage 6B v3.18 | Complete | 14,673 filings and 23,078 documents processed with zero failed work items; validation 10/10 PASS. |
| Stage 6B coverage | Accepted | 542/1,079 all-taxonomy pairs; 541/1,022 SEC-addressable pairs; 504/916 current-live SEC-addressable pairs. |
| Residual PDF recovery | Deferred | At most four SEC-addressable pairs remain recoverable; OCR would remain review-required and is not a material next lever. |
| Stage 6C | Complete | 81,221 PIT rows, 30,309 numeric rows, 86 monthly dates, 38 metrics; validation 10/10 PASS. |
| Shared factor validation | Complete, negative result | 276 hypotheses tested; none passed the sealed 5% BH-FDR gate. Specialized scoring weights remain zero. |
| Stage 7 | Not implemented | Immediate next stage. |
| Stages 8-12 | Not implemented | Must follow the dependency order below. |
| Historical dated-output backfill | Not implemented | Begins only after Stage 12 is stable. |
| Final deployment acceptance | Not started | Requires all prior gates and a production-backup migration rehearsal. |

## Non-Negotiable Modeling Contracts

1. The Stage 7 baseline is built and frozen before Stage 8 calibration.
2. Cohort-specific optimization belongs to Stage 8, not Stage 7.
3. Specialized weights remain zero until evidence passes the registered shared
   factor-validation and governance gates.
4. A metric that is not applicable to a ticker is excluded from that ticker's
   specialized completeness denominator.
5. A missing applicable metric remains null, contributes zero, and does not
   redistribute its weight to available metrics.
6. More complete disclosure does not create an artificial score bonus.
7. Historical inputs must be point-in-time and must not use future SEC
   acceptance, membership, prices, terminal returns, or source revisions.
8. Research stages cannot promote their own weights or write investable
   Portfolio Layer artifacts.
9. The Portfolio Layer consumes dated files only. It must not import Consumer
   Defensive code or open the Consumer Defensive database.
10. Every stage publishes deterministic hashes, lineage, explicit failure
    reasons, and a fail-closed validation result.

## Required Execution Order

### 1. Preserve The Accepted Stage 6 Evidence

Status: complete; retain as an immutable baseline.

Required retained evidence:

- Stage 6B v3.18 source manifest and exact SEC seal;
- accepted specialized observations and derivations;
- coverage by cohort, metric, ticker, evidence state and historical/current
  scope;
- Stage 6C panel manifest and content hash;
- factor-validation campaign registry, ledger and negative verdict;
- golden positive, negative and prohibited examples; and
- the pre-run transaction-consistent database backup.

Do not rerun broad alias expansion or OCR before a specific, economically useful
gap justifies it.

### 2. Create The Full-Stack Stage 7 Rehearsal Database

Status: next prerequisite.

The retained Stage 6B extraction database is not suitable because it has no
Stage 5 `feature_positioning` rows. Create a disposable, transaction-consistent
clone containing the complete accepted Stage 0-5 state. Production remains
unchanged.

Acceptance checks:

- database identity and schema versions match the intended source;
- foreign keys and migration ledgers are valid;
- all 108 currently eligible issuers have the required Stage 5 positioning
  state or an explicit ineligibility reason;
- the accepted Stage 6B source artifacts remain available by immutable hash;
- the rehearsal has an explicit as-of date and rollback path; and
- the clone is clearly labeled non-production and non-investable.

### 3. Rebuild Stage 6A v2 In The Full Stack

Status: pending the full-stack clone.

Actions:

1. Rebuild the common Stage 6A feature rows using definition v2.
2. Validate component completeness, freshness, lineage and PIT timestamps.
3. Require at least the frozen rank-readiness floor.
4. Investigate every missing positioning component rather than replacing it
   with an implicit zero.
5. Retain deterministic feature and manifest hashes.

Stop condition: do not begin Stage 7 if Stage 6A does not pass its complete
readiness gate.

### 4. Reapply And Validate The Stage 6B Overlay

Status: pending Stage 6A full-stack success.

Actions:

1. Reapply only accepted Stage 6B observations to the rebuilt Stage 6A rows.
2. Recompute full-data confidence from available core plus applicable
   specialized inputs.
3. Preserve the frozen non-applicable and missing-applicable behavior.
4. Confirm every specialized production weight remains zero.
5. Run the unchanged Stage 6B validator and compare hashes/counts with the
   retained accepted evidence where identity should be unchanged.

Stop condition: any unexplained loss, new observation, lineage difference or
future-data violation blocks Stage 7.

### 5. Implement Stage 7 Baseline Scoring

Status: immediate implementation stage after the full-stack readiness steps.

Scope:

1. Add an additive, versioned Stage 7 schema and migration ledger.
2. Create a reviewed baseline weight registry.
3. Freeze component definitions, directions, caps and cohort normalization.
4. Keep all specialized weights at zero because the accepted factor campaign
   produced no qualifying evidence.
5. Build dated PIT scores and ranks without overwriting Stage 6 feature rows.
6. Give every eligible current security either a valid score or a stable,
   explicit review/demotion reason.
7. Publish shadow scores with `portfolio_candidate_gate=0`.
8. Add build and validation scripts with `--asof`, deterministic manifests and
   zero-network replay support.

Required Stage 7 tests:

- unknown components and unknown weights fail fast;
- weights sum and component caps reconcile exactly;
- missing applicable inputs produce neutral zero contribution without weight
  redistribution;
- non-applicable inputs are excluded from completeness denominators;
- cohort normalization is PIT-safe and does not compare an issuer against a
  future or different cohort;
- every eligible issuer is scored or explicitly rejected;
- no future acceptance, membership, market or terminal data is consumed;
- identical replay preserves score, rank, lineage and manifest hashes;
- Stage 6 rows remain unchanged; and
- no Portfolio Layer or production promotion artifact is written.

Stage 7 completion artifact: a frozen, reviewed core baseline suitable for
comparison in Stage 8. It is not a claim that specialized metrics have predictive
value.

### 6. Implement Stage 8 Constrained Calibration Research

Status: blocked by Stage 7 completion.

Actions:

1. Register the Stage 7 baseline and every candidate before testing.
2. Define chronological training, embargo, validation and final holdout blocks.
3. Test sector-wide and cohort-specific candidates only where the registered
   minimum history and breadth are satisfied.
4. Use hierarchical shrinkage toward the sector baseline for small cohorts.
5. Enforce component, turnover, factor-breadth and cohort-concentration caps.
6. Perform walk-forward refits without touching Stage 7 weights.
7. Compare core-only and core-plus-specialized candidates.
8. Publish accepted, rejected and inconclusive results with immutable evidence.

Because the current specialized campaign is negative, the expected initial
Stage 8 outcome is retention of the Stage 7 core baseline. A nonzero specialized
candidate requires new accepted evidence; coverage by itself is insufficient.

### 7. Implement Stage 9 Report-Only Portfolio Backtests

Status: blocked by Stages 7 and 8.

Test the Stage 7 baseline and every registered Stage 8 candidate using the same
PIT panel. Produce long-only, long-short, equal-weight, score-weighted and
XLP-relative variants. Include turnover, costs, available borrow costs,
drawdown, volatility, capacity, cohort concentration and delisted terminal
returns. Stage 9 cannot change production weights.

### 8. Implement Stage 10 Publishing

Status: blocked by Stage 9.

Publish deterministic current and dated:

- final-rank tables;
- company scorecards;
- cohort summaries;
- specialized coverage and data-quality status;
- risk flags and review queues;
- factor-validation and Stage 9 evidence links; and
- non-investable historical sidecars.

Every current security must have a score or explicit review status. Historical
rows remain non-investable unless strict contemporaneous OOS capture is proven.

### 9. Implement Stage 10B Governance

Status: blocked by Stage 10.

Create the signal registry and governance lockbox. Record definitions, weights,
evidence hashes, factor verdicts, walk-forward results, backtest references,
promotion state, rollback version, reviewer and timestamps. Keep `promoted`,
`shadow_monitor` and `deferred` distinct. No statistical process can promote
itself.

### 10. Implement Stage 11 Portfolio Layer Integration

Status: blocked by Stage 10B approval.

Actions:

1. Add a neutral dated-file adapter.
2. Map the sector to Consumer Staples and benchmark to XLP.
3. Require valid OOS state, freshness, finite scores and the portfolio candidate
   gate.
4. Reject historical sidecars as investable inputs.
5. Add the approved sector cap.
6. Update risk, optimizer, macro-taxonomy, valuation and cross-sector contracts.
7. Run collection, contract, calibration, risk, optimizer and end-to-end tests.

The Portfolio Layer must never import Consumer Defensive modules or open its
database.

### 11. Implement Stage 12 Refresh Orchestration

Status: blocked by Stage 11.

Create one independent runner with an explicit step table, `--asof`,
`--dry-run`, `--skip-network`, bounded step selection, restart/resume,
fail-fast behavior, per-step logs, final validation and a health manifest.
Support promoted and shadow-monitor profiles. Routine refreshes must exclude
Stage 8 searches, Stage 9 backtests and one-time history imports.

### 12. Run The Historical Dated-Output Backfill

Status: blocked by stable Stage 12 orchestration.

Generate restartable daily outputs from `2019-01-02` using only information
available at each as-of date. Validate output/manifest dates and preserve
historical scores as non-OOS unless contemporaneous capture is proven. Verify
restartability by date and chunk, then restore the current latest dashboard.

### 13. Run Final Acceptance And Deployment

Status: final gate.

Actions:

1. Run a clean-room chronological replay on a disposable copy.
2. Run all unit, integration, cross-sector and static checks.
3. Compare database and artifact hashes against the retained acceptance run.
4. Rehearse every migration on a production backup copy.
5. Verify rollback.
6. Obtain explicit governance decisions for weights, promotion state, Portfolio
   Layer cap and remaining exceptions.
7. Migrate and refresh production only after every gate passes.
8. Monitor initial live runs and retain the prior production state for rollback.

## Stage Dependency Chain

```text
Accepted Stages 0-6C
  -> full-stack rehearsal clone
  -> Stage 6A v2 rebuild
  -> Stage 6B overlay reapplication
  -> Stage 7 frozen baseline scoring
  -> Stage 8 report-only calibration
  -> Stage 9 report-only portfolio backtest
  -> Stage 10 publishing
  -> Stage 10B governance decision
  -> Stage 11 Portfolio Layer file adapter
  -> Stage 12 refresh orchestration
  -> historical dated-output backfill
  -> final clean-room acceptance
  -> production migration
```

Skipping a dependency invalidates every downstream acceptance claim.

## Usage-Constrained Work Tranche

At the time this roadmap was requested, the Codex UI showed 11% remaining for
GPT-5.6 Sol at extra-high reasoning, with a requested reserve of 5%. This leaves
a nominal six-percentage-point margin. OpenAI's official model documentation
confirms the model and `xhigh` reasoning mode, but does not define a deterministic
conversion from the Codex UI percentage to files, tool calls, tests or elapsed
work. The reserve therefore cannot be guaranteed programmatically.

The safe tranche within that margin is:

1. complete and validate this authoritative roadmap;
2. freeze the Stage 7 implementation and acceptance contract in this document;
3. perform only a read-only inventory of candidate full-stack rehearsal inputs;
4. record the exact Stage 7 prerequisite and stop condition; and
5. stop before editing Stage 7 schema, scoring code or migrations.

Do not begin the Stage 7 engine under this usage constraint. A partial schema or
half-tested scoring path would be worse than a clean documented boundary. Start
Stage 7 in the next sufficiently funded task and implement it through its full
test and acceptance boundary.

## Immediate Next Task

The next implementation task should be narrowly stated as:

> Create and validate the full-stack isolated Stage 7 rehearsal database,
> rebuild Stage 6A v2, reapply the accepted Stage 6B overlay, and stop unless all
> Stage 6 readiness gates pass. If they pass, implement the complete versioned
> Stage 7 baseline scoring layer with specialized weights frozen at zero.

This keeps the path coherent and prevents Stage 8 calibration, Stage 9
backtesting or Portfolio Layer work from starting before the baseline exists.
