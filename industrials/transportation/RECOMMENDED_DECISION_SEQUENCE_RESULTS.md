# Transportation Recommended Decision Sequence Results

Status: complete and passing through DP16 on 2026-07-29.

## Decision

The efficient sequence is complete through the single bounded walk-forward
calibration, independent validation, and portfolio-layer shadow gate. The
system must not run another broad source search,
historical parser batch, market-feature build, generic financial-feature
build, membership rebuild, or v3 panel rebuild for the current contract.

The final calibration subset contains three cohort-specific specialized
metrics:

| Cohort | Specialized metric | Historical value rows | Issuers |
|---|---|---:|---:|
| Marine shipping and maritime | `fleet_utilization` | 485 | 9 |
| Surface freight and logistics | `operating_ratio` | 651 | 14 |
| Air transport and aviation services | `passenger_load_factor` | 381 | 9 |
| Development-stage transport | No eligible specialized overlay | 0 | 0 |

The current-date all-source coverage remains 206 accepted pairs out of 2,526
applicable pairs (8.155%): 71 parser-accepted pairs plus 135 financial-derived
pairs. Resolving the two duplicate accepted values correctly reduced accepted
evidence observations without changing ticker/metric coverage.

## Implemented sequence

1. The exhaustive one-pass corpus and bounded local fixture review were reused.
   No document was retrieved or parsed again.
2. The panel preflight found two accepted same-period conflicts and stopped
   before publication.
3. Cached filing tables established the correct group values:
   - ALK 2023 passenger load factor: 83.7% consolidated; 83.8% was regional.
   - UAL 2017 passenger load factor: 82.4% consolidated; 82.5% was unlabeled.
4. Two exact `SUPPRESSED_SEMANTIC_DUPLICATE` policies were sealed. Only parser
   run 58 required a policy-only replay; runs 59, 60, and 65 reused their
   existing evaluations. The final reviewed lineage is 11/8/5/9.
5. Coverage and financial overrides were regenerated parse-free. Final metric
   dispositions remain 3 calibration candidates, 49 deferred-review metrics,
   17 diagnostic-only metrics, and 21 excluded metrics.
6. DP8 authorized only the new specialized historical partitions.
7. DP9 performed one successful versioned materialization under
   `historical_features/v3_conflict_resolved`.
8. G8 independently validated row order, membership, hashes, finite values,
   and point-in-time dates.
9. DP10 audited the proposed slow-moving flag exception and rejected it under
   the unchanged general gates.
10. DP10 froze the one walk-forward calibration contract; no calibration was
    executed during the contract-freeze gate.
11. DP11 built the 63-session outcome panel from the already-loaded active,
    delisted, benchmark, alias, membership, and continuity histories. The
    shared database was opened read-only.
12. DP12 independently reconciled candidate coverage, recomputed security and
    benchmark returns from their stored price legs, audited terminal events and
    structural boundaries, and measured cohort-specific rank-usable periods.
13. DP13 executed the one authorized bounded calibration. All five permitted
    weights were evaluated on train and validation; holdout saw only zero and
    the one validation-selected weight for each candidate.
14. DP14 independently reproduced the aggregates, selection, transaction-cost
    arithmetic, holdout boundary, hashes, and zero-overlay fallback decisions.
15. DP15 sealed those decisions against the unchanged 112-row transportation
    dashboard and the shared `portfolio_layer` adapter. All rows remain
    optional, non-investable, and OOS-invalid.
16. DP16 froze an outcome-blind, post-refresh monitoring contract and audited
    the initial waiting state. It does not modify the sealed refresh pipeline,
    access outcomes, optimize weights, rebuild history, or authorize
    recalibration.

## Frozen panel

- 92 month-end research observation dates from 2019-01-02 through 2026-07-22.
- 9,496 historical ticker/date memberships.
- 854,640 specialized rows: all 90 finalized metrics for every membership.
- 1,025,568 complete rows: 90 specialized plus 18 frozen generic metrics.
- Complete-panel SHA-256:
  `128594a7356e305a1f7925b9f4feadde66e3a90fe68bf7be262062212d9c72c0`.
- Future availability errors: 0.
- Future period errors: 0.

Month-end is the research observation cadence. It is not a weekly rebalance
rule and does not define a production portfolio rebalance schedule.

## Flag exception decision

No flag-specific threshold exception is authorized:

- `going_concern_flag` has 126 historical value rows across six issuers, but
  fails issuer breadth, has a 1.5-period median, and contains only positive
  flag observations. It does not fail only on depth and cannot estimate a
  cross-sectional weight.
- `pre_revenue_flag` passes the current-date breadth/precision contract, but
  has no frozen point-in-time flag values and no observed binary variation.
  Missing revenue is not treated as zero.

Changing the general metric gates or adding a flag exception would therefore
admit no new trustworthy weight-estimation information.

## Frozen calibration contract

The single authorized run was bound to the complete-panel hash and the
three-metric subset.

- Outcome: 63-trading-day forward excess return.
- Primary benchmark: IYT.
- Robustness benchmarks: XTN and SPY.
- Boundary purge: forward window plus a 21-day embargo.
- Split counts: 52 train, 15 validation, 19 untouched holdout, 6 embargo.
- Cost gate: 20 bps per one-way turnover and a 40 bps stress case.
- Optimization: bounded per-cohort overlay grid
  `[0, 0.025, 0.05, 0.075, 0.10]`.
- Generic component weights remain frozen.
- Holdout is confirmatory and cannot select weights.
- A failing specialized candidate retains a zero overlay weight.
- Production promotion remains false.

## Frozen outcome panel

The read-only DP11 build produced:

- 6,152 applicable candidate rows, exactly reconciled to the frozen historical
  coverage report.
- 1,517 rows with a specialized metric value.
- 5,835 rows with a security return and all IYT, XTN, and SPY benchmark
  outcomes.
- 1,260 calibration-eligible rows after metric-value, cohort, return, split,
  and embargo controls.
- 56 row-level terminal outcomes: 52 acquisition rows using the last verified
  adjusted close and four reviewed wipeout rows using an explicit zero.
  Terminal proceeds are then treated as zero-return cash through the remaining
  63-session benchmark horizon; no unapproved interim rebalance is assumed.
- Zero structural-break or cross-listing stitches.
- Zero parser runs, source-document opens, network requests, feature rebuilds,
  membership rebuilds, database writes, portfolio writes, or calibration runs.

The outcome-panel SHA-256 is
`de5e929490243e6db04dcb1a0ade4fd1ab779758aa60b2e098358dd4cfc2fefd`.
Recent observations without a complete 63-session outcome remain explicitly
right-censored. They are not filled with a shorter return or synthetic price.

Applicable discovery rows outside a candidate's one frozen calibration cohort
remain in the audit panel but are not calibration-eligible. This preserves the
full coverage reconciliation without silently broadening the overlay contract.

## Outcome-readiness results

The unchanged gate requires at least three issuers with varying values in a
rank-usable period and at least 12 such periods in holdout.

| Candidate | Cohort | Train usable periods | Validation usable periods | Holdout usable periods | Holdout issuers | Gate |
|---|---|---:|---:|---:|---:|---|
| `fleet_utilization` | Marine | 38 | 15 | 15 | 4 | PASS |
| `operating_ratio` | Surface freight | 52 | 15 | 12 | 6 | PASS |
| `passenger_load_factor` | Air transport | 48 | 15 | 15 | 6 | PASS |

All three candidates are authorized for the one bounded walk-forward
calibration. `operating_ratio` passes exactly at the 12-period holdout minimum,
so its later validation should preserve a zero overlay unless every frozen
performance and cost gate passes.

## Bounded calibration results

DP13 used 5,577 intended-cohort observations, including all 1,260 rows that
passed metric-value, baseline-score, return, split, and embargo controls. The
18 generic metrics and their component weights remained frozen. No dashboard,
portfolio, database, parser, feature, or membership state was changed.

Each candidate selected the maximum permitted 10% overlay using train and
validation only. The untouched holdout then rejected all three:

| Candidate | Selected weight | Holdout periods | Mean rank IC | Net spread at 20 bps | Net spread at 40 bps | Failure | Final weight |
|---|---:|---:|---:|---:|---:|---|---:|
| `fleet_utilization` | 0.10 | 15 | -0.1733 | 0.0147 | 0.0131 | Negative rank IC | 0.00 |
| `operating_ratio` | 0.10 | 12 | -0.3274 | -0.1005 | -0.1020 | Negative rank IC and net spreads | 0.00 |
| `passenger_load_factor` | 0.10 | 15 | 0.1410 | -0.0241 | -0.0250 | Negative net spreads | 0.00 |

The IYT, XTN, and SPY conclusions are identical. Within a research date the
benchmark return is a common subtraction across issuers, so rank IC and the
top-minus-bottom spread are invariant to that benchmark subtraction; all
three benchmark rows were nevertheless generated and gated as required.

Turnover passed for every selected candidate. Average one-way turnover ranged
from 0.125 to 0.3333, below the unchanged 0.75 maximum. History depth also
passed. The zero decisions therefore reflect holdout performance, not a data
coverage or turnover shortcut.

DP14 passed with no errors and reproduced the validation-selected 10% weights,
the three zero final weights, all 108 grid summaries, and all 2,997
period/benchmark rows. Its holdout-selection audit confirmed that no
unselected nonzero weight was evaluated on holdout.

A repeat invocation reused the sealed DP13 outputs: no artifact hash or
timestamp changed, and the recorded calibration invocation count remains
exactly one.

## Portfolio-layer shadow result

The existing shared `industrial_family` adapter passed for the sealed
2026-07-22 transportation snapshot:

- 112 transportation rows were read.
- Investable rows: 0.
- OOS-score-valid rows: 0.
- Research-calibration-eligible rows: 0.
- Survivorship-corrected current-dashboard rows: 0.

DP15 hash-binds the DP13 manifest, DP14 validation, rank table and validation,
portfolio-adapter validation, and `portfolio_layer` configuration. The
transportation source remains enabled but optional, requires a valid OOS score,
and fails closed. Production promotion remains unauthorized.

## Zero-overlay monitoring

The monitoring policy is fixed in
`data/transportation_zero_overlay_monitoring_policy.yaml` and is bound to the
exact DP15 SHA-256. It independently verifies that the research challenger
weights are the 10% weights selected by DP13/DP14, while all portfolio overlay
weights remain zero.

Monitoring uses an immutable post-refresh companion rather than changing the
sealed industrials refresh process:

1. After a future month-end current refresh, export one outcome-free source
   snapshot with exactly these fields:
   `asof_date,ticker,metric_id,calibration_cohort,baseline_score,specialized_percentile`.
2. Run `21_capture_transportation_candidate_shadow_signals.py` with that
   source. The collector recomputes the fixed 10% challenger score and ranks,
   rejects outcome-like fields, and refuses to overwrite a non-identical
   snapshot.
3. Run `21a_audit_transportation_zero_overlay_monitor.py` to validate every
   immutable snapshot and update monitoring progress.

The first permitted signal date is 2026-07-31. The first DP16 audit on
2026-07-29 correctly records zero signals for all three candidates. It passes
with `CONTINUE_ZERO_OVERLAY_SHADOW_MONITORING`; it does not represent missing
work or a stopped process.

A separate outcome-audit protocol cannot even be requested until both of these
conditions hold:

- at least 12 new monthly, rank-usable signals exist for every candidate; and
- the date is no earlier than 2027-09-30, allowing the twelfth 63-session
  outcome window to mature.

Even then, DP16 does not authorize recalibration or promotion. It changes the
next gate only to `REQUEST_SEPARATE_OUTCOME_AUDIT_PROTOCOL`, preserving a
second explicit decision boundary before any future outcomes are opened.

## Acceptance gates

| Gate | Result |
|---|---|
| Exact conflict set resolved from cached filing scope | PASS |
| Review-policy golden validation | PASS |
| Additional broad parser batches required | 0 |
| DP8 historical-impact preflight | PASS |
| DP9 one-time v3 materialization | PASS |
| G8 point-in-time panel validation | PASS |
| Flag-specific exception justified | No |
| DP10 calibration-contract freeze | PASS |
| DP11 survivorship-safe outcome build | PASS |
| DP12 outcome/readiness validation | PASS |
| Candidates ready under unchanged holdout gate | 3 of 3 |
| DP13 single bounded calibration | PASS; exactly 1 invocation |
| DP14 independent calibration validation | PASS |
| Holdout-confirmed specialized overlays | 0 of 3 |
| DP15 zero-overlay portfolio shadow gate | PASS |
| DP16 outcome-blind zero-overlay monitor | PASS; waiting 0 of 12 signals |
| Calibration executed | Yes; research only |
| Portfolio/production writes | 0 |

## Next authorized action

At the first eligible month-end on or after 2026-07-31, capture one immutable
outcome-blind candidate signal snapshot after the ordinary current refresh,
then rerun only the DP16 monitor. Do not reparse historical filings, rebuild
the frozen panel, open forward outcomes, rerun calibration, or promote the
three rejected overlays. A future outcome audit requires 12 new monthly
signals, 63-session maturity, and a separate approved protocol. Production
promotion remains unauthorized.

## 2026-07-30 immediate production-readiness batch

Historical note: the membership counts in this frozen batch predate the 2026-07-31 reviewed
Celadon lifecycle amendment. Current contracts are 159 usable mappings and 47 delisted histories.

The immediate batch is implemented without reopening source discovery,
historical parsing, feature materialization, calibration, or outcomes.

### Stage 0-4 foundation gate

`04b_validate_transportation_stage0_4_production_readiness.py` passed all 22
required checks at the frozen 2026-07-22 as-of date. The validator opens the
shared industrials database read-only and confirms the universe contracts,
158-row historical membership after the approved CGI/RRTS price exclusions,
all 160 database identities, shared schema, historical raw-load seal, market
and financial feature breadth, source freshness, and family isolation.

This gate originally surfaced two validator-contract errors. It now checks
the actual `fact_financial_statement_canonical` source-of-record table and
derives the 158-row historical expectation from the 160 active-plus-delisted
identities less the two explicit price exclusions. No data or acceptance
threshold was changed. The gate explicitly records:

- `production_promotion_authorized=false`
- `oos_score_valid_authorized=false`
- next gate: `IMPLEMENT_AND_VALIDATE_STAGE5_POSITIONING`

### Transportation-scoped Stage 5 positioning

The shared positioning infrastructure is now exposed through transportation
wrappers and a fail-closed family configuration. The implementation reuses
the independent shared scripts; it does not copy them into the subsector.
Paths and wrapper arguments are pinned to `model_family=transportation`, and
all issuer routes and exemptions are explicit, dated, and reviewable in the
transportation positioning override file.

The initial bounded local import exposed that the earlier completion label was
too permissive: it did not require 13F and treated a 50% Form 4 routing floor
as sufficient. The recovery batch corrected the contract before promotion:

- Form 4 is source-aware. A ticker is covered by eligible transactions,
  covered by submissions with no eligible non-derivative transactions, or
  explicitly not applicable; a zero-row ticker can no longer be silently
  called complete.
- 13F is required for Stage 5, with a pinned 2026-03-31 period, ten anchor
  issuers, and a minimum of ten available anchors.
- The non-exempt Form 4 floor is 100%.

The 13F recovery then scanned all 30 already-downloaded SEC archives exactly
once. It made zero network requests, refreshed 753,324 matched holdings for
147 active-plus-historical transportation tickers, and produced 4,034
industrials snapshot rows. This avoids both a repeated SEC download and
metric-by-metric reparsing.

The final local import used the recovered upstream database, imported facts
for all 160 identities, and built the 2026-07-22 feature snapshot for exactly
112 current transportation members:

- 39,635 Form 4 rows;
- 2,147 13F rows;
- 18,087 short-interest rows;
- 152,658 borrow rows;
- 85 active rows with `positioning_quality=complete`;
- 27 active rows with `positioning_quality=policy_exempt`;
- zero unresolved required-source failures.

The Stage 5 validator passed. Form 4 covers 87 of 87 non-exempt active
issuers at the 100% gate: 85 have eligible transactions, while DSX and TOPP
have SEC ownership submissions but no eligible non-derivative transaction.
The remaining 25 active names are predominantly foreign/private issuers with
no SEC ownership submissions and are explicitly classified
`not_applicable`.

Active 13F coverage is 109 of 112. ELOG and NCEW have no holdings in the
cached 2026-03-31 archive, while FDXF listed/spun off after that quarter; the
three absences are explicit, time-bounded review exemptions through
2026-10-15. All ten required anchor issuers are present.

The subsequent FINRA-only recovery used the existing cache and explicitly
skipped 13F and IBKR. It processed 176 available settlement files, recorded
five unavailable file dates, matched 18,087 transportation rows, and
backfilled 21,051 database-wide short-interest rows from already-local share
proxies. Active raw short-interest coverage is 112/112 and is now required by
the transportation Stage 5 gate. At the frozen 2026-07-22 snapshot, all 112
active issuers have short shares, 110 have percent-of-float, and 111 have a
three-month change. ELOG lacks a float proxy; newly listed FDXF lacks a float
proxy and sufficient three-month depth. Percent-of-float remains diagnostic,
matching the defense contract.

The subsequent IBKR-only recovery explicitly skipped FINRA, 13F, float
proxies, downstream import, and the separate live shortable-share snapshot.
All 112 active contracts qualified, zero tickers failed, and 150,744
historical `FEE_RATE` rows were written or refreshed through 2026-07-30. The
transportation universe has 152,419 active-ticker upstream observations; the
active-plus-historical industrials import contains 152,658 rows. At the
frozen 2026-07-22 snapshot, all 112 active features contain a non-stale
borrow rate. IBKR borrow is now required by the transportation Stage 5 gate.
The report retains only three transparent source-coverage issues: the
existing time-bounded 13F exceptions for ELOG, FDXF, and NCEW.

This run also found and fixed a shared cross-family state bug. `FLY` is active
in defense but delisted in transportation; the old shared query used the
global company active flag and incorrectly created a 113th transportation
feature row. Import and validation now use family-scoped
`dim_universe_membership.is_current_member`, and exact family/source/date
snapshots are replaced before insertion so departed rows cannot persist.
Regression coverage reproduces this collision. The non-transportation
`feature_positioning` fingerprint remained unchanged at 153,198 rows and
SHA-256
`3e13b110222911421faa9c948e04e7ab17f2a9d9a486d3d1eeed354ccefdfb6e`.

### Automatic DP16 source and month-end runner

`21b_export_transportation_monitoring_source.py` now reconstructs the frozen
generic baseline from the complete panel, applies the policy-bound direction
for each specialized candidate, computes cross-sectional percentiles, and
writes only the six permitted outcome-free source fields. Its manifest binds
the complete panel, metric registry, component weights, and monitoring
policy. Existing identical artifacts are reused; non-identical overwrites are
refused.

The exporter validation at 2026-07-22 produced ten rows: four
`fleet_utilization`, three `operating_ratio`, and three
`passenger_load_factor`. That date is pre-monitoring and was used only to
validate the exporter; no signal was captured. The one-command
`21c_run_transportation_month_end_monitoring.py` now refuses a date before
2026-07-31 before writing anything, then performs only source export,
immutable signal capture, and the outcome-blind DP16 audit.

The refreshed 2026-07-30 DP16 audit passes under policy SHA-256
`55190a4bf896af82ad523b65b86ffb9e6a5c6c04d8bda09c78ced5e877ce92af`.
It correctly records zero of 12 new signals, zero outcome access, zero parser,
calibration, database, portfolio, and production writes, and
`CONTINUE_ZERO_OVERLAY_SHADOW_MONITORING`.

### Why the generic model has no production OOS seal

The DP10-DP15 contract treats the generic model as the frozen control arm for
testing incremental specialized overlays. It does not authorize optimizing,
promoting, or certifying the generic model itself. A zero overlay means only
that no specialized candidate beat the frozen baseline under every holdout
gate; it is not an absolute production test of the baseline.

The available cohort-level zero-overlay diagnostics would not support a
shortcut. On IYT holdout data, the generic control had mean rank IC/net
20-bps spread of -0.2067/-0.0131 for marine, -0.4000/-0.0837 for surface
freight, and 0.0971/-0.0329 for air transport. More importantly, those are
three candidate-cohort diagnostics, not a full 112-name, full-history
production qualification covering absolute OOS performance, stability,
capacity, liquidity, concentration, risk, stress, and governance.

Consequently the rank and portfolio artifacts correctly retain zero
`oos_score_valid_flag` rows and zero investable transportation rows. Granting
a production OOS seal from the overlay experiment would be a false
certification. A separate generic-model promotion contract and independent
validation are required.
