# Consumer Defensive Completion Roadmap

## Purpose And Authority

> **V2 authority update (2026-08-26):** The legacy future-evidence and Stage 12
> routes described below are retired and archived. The authoritative calibration
> and promotion sequence is now `CONSUMER_DEFENSIVE_V2_IMPLEMENTATION_PATH.md`.
> Historical sections remain here only to preserve the implementation record and
> cannot authorize calibration, promotion, orchestration, or portfolio activation.

This document is the authoritative remaining-work sequence for completing the
Consumer Defensive implementation. It consolidates the stage order in
`README.md`, `STAGE_GATES.md`, and `IMPLEMENTATION.md` into one execution path.

If this roadmap conflicts with a stage's frozen acceptance contract in
`STAGE_GATES.md`, the stage gate controls. The Stage 12 runner's explicit step
table becomes the operational authority after Stage 12 is implemented.

The implementation is Consumer Defensive-specific. It may reuse shared kernels
such as `dedicated_parser` and `factor_validation`, but it must not import or run
Technology sector scripts as its implementation path.

## 2026-08-25 Validation Correction

The corrected V6 audit supersedes earlier Stage 8/9 promotion language below.
The reported 24/24 and 31/31 results are internal artifact-integrity checks,
not independent predictive-validation or strict-OOS passes. The authoritative
corrected evidence is `output/consumer_defensive/validation_v4/2026-08-25/v6`;
capital promotion remains fail-closed. The cross-sector capital verdict and
admissible three-authority future protocol are recorded in
[PRODUCTION_PROMOTION_AUDIT_2026-08-25.md](../PRODUCTION_PROMOTION_AUDIT_2026-08-25.md).
The old Stage 10B package remains a shadow record and is superseded for any
promotion decision.

## Current Accepted State

| Workstream | Status | Accepted evidence |
| --- | --- | --- |
| Stages 0-5 | Complete | Production foundation, market, financial, disclosure and positioning gates are implemented. |
| Stage 6A | Complete through isolated acceptance | The full-stack `2026-08-14` rebuild passes 20/20 checks with 96/108 rank-ready inputs. |
| Stage 6B v3.18 | Complete | 14,673 filings and 23,078 documents processed with zero failed work items; validation 10/10 PASS. |
| Stage 6B coverage | Accepted | 542/1,079 all-taxonomy pairs; 541/1,022 SEC-addressable pairs; 504/916 current-live SEC-addressable pairs. |
| Residual PDF recovery | Deferred | At most four SEC-addressable pairs remain recoverable; OCR would remain review-required and is not a material next lever. |
| Stage 6C | Corrected; isolated PASS | Run 3 has 81,221 PIT rows, 28,487 valid numeric rows, 86 monthly dates, and 38 metrics; validation 18/18 PASS. |
| Shared factor validation | Corrected, zero accepted | 174 cells were registered; 90 were testable across eight metrics. One inverse-direction cell passed FDR, so zero cells passed every acceptance gate and specialized weights remain zero. |
| Stage 7 | Corrected v3; isolated PASS | The shadow baseline produces 108/108 deterministic outputs, 94 rank-ready, with all 15 checks passing and all portfolio/OOS gates off. |
| Stage 8 | Legacy replay integrity complete; promotion evidence invalid | The run passes 24/24 internal artifact checks, but 320 candidates touched holdout data, including 318 unauthorized candidates and 5,088 unauthorized period rows. |
| Stage 9 | Legacy replay integrity complete; retrospective only | The run passes 31/31 internal artifact checks, but uses the burned holdout and a fixed 21-session target that does not match Stage 8's 21/63/126-session objective. |
| Stage 10 | Publishing integrity complete; shadow only | The package publishes and replays deterministically, but inherits non-OOS Stage 8/9 evidence and remains non-investable. |
| Stages 10B-12 | Code complete and fail-closed; evidence pending | The operational publisher, cohort-scoped signed-lock bridge, Portfolio adapter, per-cohort calibration/caps, local refresh runner and master non-blocking shadow registration are implemented. All checked-in cohort alpha/caps remain zero. |
| Historical dated-output backfill | Engine implemented; execution pending | The restartable PIT-census sidecar/backfill refuses incomplete historical membership censuses and never labels reconstructed rows strict OOS. |
| Final deployment acceptance | Rehearsal tooling implemented; execution pending | The backup-copy migration rehearsal is fail-closed on a signed activation registry and proves the production database remains byte-unchanged. |

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

Status: complete. Retain the legacy evidence immutably and use the corrected
run 3/v3 campaign as the current research baseline.

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

Status: complete in the isolated `2026-08-14` rehearsal; production unchanged.

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

Status: complete; 108 inputs and 5,940 components pass all 20 checks, with
96/108 (`88.89%`) rank-ready against the frozen `85%` floor.

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

Status: complete; 505 exact-run specialized component measurements were
reapplied at zero weight in the corrected rehearsal. The canonical-lineage,
sealed-run, and future-period defects are fixed and covered by regression tests.

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

Status: complete through isolated acceptance; production migration remains
deferred to final deployment acceptance.

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

Measured acceptance at `2026-08-14`:

- 108/108 current tickers have deterministic shadow outputs;
- 94 are rank-ready and 14 have explicit review reasons (`87.04%`, above the
  frozen `85%` floor);
- cohort readiness is beverages 19/22, distribution/retail 19/22,
  household/personal/tobacco 22/25, and packaged foods/agriculture 34/39;
- all 38 specialized components remain zero-weight under corrected campaign
  `cdfv_20260814_d2c7155be91c_2498172c7161_a6495192b5`; eight metrics were
  testable, and the only FDR-significant cell failed its pre-registered
  direction gate;
- `promotion_state=shadow_monitor`, `portfolio_candidate_gate=0`, and
  `oos_score_valid_flag=0` for every row;
- contract SHA-256 is
  `d5184d007b89f3be62c61277cd4ddcb864f15ff0ccd09d9234de31922cf909c8`;
- baseline-input manifest SHA-256 is
  `ad90697b81c020c3666d47b04aa2ece231a2d8b7793dc00d23e27dd907f2500a`;
- output manifest SHA-256 is
  `abcca120e948d45a440b5f421809f3fb98b656484a4439d8b493e8a852fe93e8`,
  reproduced exactly by the same-date replay; and
- the full Consumer Defensive test suite passes 391 tests with five
  platform-specific skips, and Ruff passes every modified file.

### 6. Implement Stage 8 Constrained Calibration Research

Status: legacy artifact-integrity replay complete; promotion evidence is retrospective and invalid.

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

Measured acceptance at `2026-08-14`:

- immutable run `cds8_2a94264294f4b58b1444fb2d` passes 24/24 internal
  artifact-integrity checks, not statistical-independence or OOS checks;
- the evidence panel retains 9,036 rows, 116 tickers and 86 monthly dates;
- `2026-02-11` is a partial/immature panel date and cannot represent a true
  completed month-end observation;
- a label-blind frozen-baseline census admits 56 dates beginning `2021-07-30`
  and explicitly excludes 30 earlier non-rank-ready dates;
- the chronology is 30 training dates, seven embargo dates, six validation
  dates, seven embargo dates and six final-holdout dates;
- all 320 candidates were exposed to the holdout; only two were authorized,
  leaving 318 unauthorized candidates and 5,088 unauthorized period rows;
- candidate-dependent eligibility compared candidates and baseline on different
  samples;
- stale 13F observations, incomplete source identity, and current/final rather
  than point-in-time sector taxonomy leave freshness and survivorship gates
  false;
- beverages reached the final holdout but improved the weighted IC objective by
  only `0.00005848`, below the registered `0.002` minimum;
- the sector and household/personal/tobacco candidates failed walk-forward
  repeatability, while distribution/retail and packaged foods/agriculture
  failed validation;
- zero candidates and zero specialized overlays were accepted, so the Stage 7
  core baseline is retained; and
- production promotion, Portfolio Layer writes, Stage 7 mutation and OOS claims
  remain disabled.
- an exact same-directory replay reproduced every immutable artifact
  byte-for-byte.

The complete failure history and hashes are recorded in
`STAGE8_CALIBRATION_AUDIT_2026-08-24.md`.

### 7. Implement Stage 9 Report-Only Portfolio Backtests

Status: legacy artifact-integrity replay complete; production remains fail-closed.

Test the Stage 7 baseline and every registered Stage 8 candidate using the same
PIT panel. Produce long-only, long-short, equal-weight, score-weighted and
XLP-relative variants. Include turnover, costs, available borrow costs,
drawdown, volatility, capacity, cohort concentration and delisted terminal
returns. Stage 9 cannot change production weights.

Measured acceptance at `2026-08-14`:

- all 320 Stage 8 candidates were evaluated, confirming retrospective holdout
  exposure rather than an unopened OOS test;
- 56 calendar slots produced 40 greedily selected 21-session windows; the
  remaining 16 schedule slots were discarded, not evaluated as cash;
- the package contains 2,560 summary rows, 46,280 period rows and 377,106
  holding rows, including four selected terminal-return panel rows;
- transaction costs, observed and missing-borrow stress, drawdown, volatility,
  capacity, liquidation time, cohort concentration and name concentration are
  reconciled from holdings through summaries;
- run `cds9_63065740a60179d1a1abc968` and manifest
  `03346ffceb33b9f1c7b974229cad4ec1f5638945422d476f8cbe8aca3b1df183`
  pass all 31 internal artifact-integrity checks; and
- Stage 9's fixed 21-session return target does not match Stage 8's weighted
  21/63/126-session selection objective;
- the decision remains report-only: Stage 7 is retained, OOS validity and
  portfolio gates remain off, and database writes are zero.

The complete contract, caveats and failure history are recorded in
`STAGE9_PORTFOLIO_BACKTEST_AUDIT_2026-08-25.md`.

### 8. Implement Stage 10 Publishing

Status: publishing integrity complete; inherited promotion evidence remains invalid.

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

Measured acceptance at `2026-08-14`:

- 108/108 securities publish a deterministic score or explicit review status;
- 94 are rank-ready and 14 remain in the review queue;
- 543/971 applicable specialized ticker-metric pairs are measurement qualified
  (55.92%), while zero of 38 specialized metrics are model-weight qualified;
- the package contains 5,940 company scorecard rows, 190 metric coverage rows,
  43 risk flags, 40 frozen Stage 9 baseline views, and seven source-ledger rows;
- run `cds10_729cbfd933b3c0ddc912b999`, contract
  `729cbfd933b3c0ddc912b999bd9e5b210e0ec545685e7bb4f970c0cfa764d8cb`
  and manifest
  `1ef5e07f8f33b775574c2a356cddd907dbecae6ebd10c5edc8572d026abc709d`
  pass all 17 publishing checks after a fresh 31/31 artifact-integrity validation;
- desktop and responsive render QA pass; and
- an exact replay leaves all 14 dated and 14 latest file hashes unchanged.

The accepted package remains research-only, shadow-monitor, non-OOS and
non-investable. Details are in `STAGE10_PUBLISHING_AUDIT_2026-08-25.md`.

### 9. Implement Stage 10B Governance

Status: legacy v1 is implemented as shadow governance; it is not a promotion
authority and is superseded for that purpose by the corrected V6 evidence.

The signal registry and governance lockbox record definitions, weights,
evidence hashes, factor verdicts, backtest references, promotion state,
rollback version, reviewer and timestamps. The accepted package is explicitly
`shadow_monitor`, zero-cap, and non-OOS; no statistical process can promote
itself.

Any future promotion candidate must bind the exact passing evaluation hash from
the canonical three-authority protocol and a separate independent-review
receipt. It must not reuse the legacy Stage 10B decision or change production
configuration automatically.

### 10. Implement Stage 11 Portfolio Layer Integration

Status: cohort-aware adapter and optimizer contracts implemented and disabled;
activation remains blocked by promotion evidence and signed change control.

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

Status: implemented. Script `28_run_consumer_defensive_stage12_pipeline.py`
performs local feature/score validation, deterministic Stage 10 publishing and
independent validation, then publishes a separate Stage 12 operational file.
The master registry runs it as a non-blocking shadow lane. Capital activation
remains independently blocked without an effective signed cohort lock.

The implemented runner has an explicit local-only step table, `--asof`,
`--dry-run`, `--skip-local-score-build`, fail-fast subprocess receipts, final
validation and a Stage 12 health manifest. Network work is excluded by
construction. A failed refresh is rerun atomically through the master
orchestrator instead of resuming inside a partially published Stage 12 run.
Dry-run may display clearly marked unresolved Stage 8/9 placeholders, while a
real run fails closed unless accepted roots exist. It supports promoted and
shadow-monitor profiles. Routine refreshes exclude Stage 8 searches, Stage 9
backtests and one-time history imports.
Promotion-facing capture/evaluation additionally requires the fixed canonical
trust registry, independent evidence sealing, external append-only timestamping,
independent market-data export attestation, and exact official XNYS chronology.

### 12. Run The Historical Dated-Output Backfill

Status: restartable engine implemented; historical execution and census
exceptions remain pending.

Generate restartable daily outputs from `2019-01-02` using only information
available at each as-of date. Validate output/manifest dates and preserve
historical scores as non-OOS unless contemporaneous capture is proven. Verify
restartability by date and chunk, then restore the current latest dashboard.

### 13. Run Final Acceptance And Deployment

Status: final external/evidence gate; backup rehearsal code is implemented but
cannot pass until a signed activation registry exists.

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
  -> legacy Stage 10B zero-cap shadow record
  -> Stage 11 Portfolio Layer file adapter
  -> Stage 12 fail-closed shadow refresh
  -> historical dated-output backfill
  -> canonical three-authority prospective registration and capture
  -> matured future-only evaluation
  -> independent promotion review and new lock
  -> final clean-room acceptance
  -> production migration
```

Skipping a dependency invalidates every downstream acceptance claim.

## Prior Usage-Constrained Work Tranche

The earlier 11%-to-5% usage tranche documented the safe stopping boundary before
Stage 7. The user subsequently issued an explicit implementation instruction,
and Stage 7 was completed through its isolated acceptance boundary. Usage
percentages remain UI estimates and are not treated as implementation gates.

## Immediate Next Task

The next implementation task should be narrowly stated as:

> Configure and independently approve the three canonical trust authorities,
> then externally timestamp the exact frozen future-only monthly evidence
> contract before any target access. Run target-blind contemporaneous capture on
> each eligible true month end, evaluate outcomes only after maturity, preserve
> the 12/6/4 independent-observation floors, and keep every capital path fail
> closed until an independent review binds a passing evaluation into a new lock.

The code path through cohort-scoped Portfolio calibration, caps, Stage 12
publishing, historical sidecars, master shadow orchestration, and backup-copy
rehearsal is implemented. Canonical authority configuration, prospective
registration, fresh OOS observations, independent promotion review, a signed
activation registry, and execution of the backup-copy rehearsal remain
outstanding. A historical replay cannot shorten this sequence. The decision on
which elements of the Biotech target framework to adapt is recorded in
`BIOTECH_FRAMEWORK_ADAPTATION_DECISION.md`.
