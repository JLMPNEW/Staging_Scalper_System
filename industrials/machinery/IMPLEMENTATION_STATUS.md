# Machinery Implementation Status

Status date: 2026-07-25

This document is the authoritative implementation sequence and acceptance
checklist for the machinery model family. `README.md` remains the operating
quick reference. A stage is complete only when its acceptance gate passes on
production data; a dry run proves orchestration only.

## Current State

The universe, identity contracts, family-scoped loaders, enhanced filing-level
financial extraction, metric-availability contract, shadow scoring, dashboard
contract, industrial portfolio adapter, and bootstrap orchestration are
implemented. The production refresh passed all 29 stages through the portfolio
adapter as of `2026-07-24`. The active universe contains 113 issuers after
`GTLS` was correctly ended on its 2026-07-16 acquisition close and retained in
historical membership through that date.

The all-metrics audit covers every active ticker and all 28 required metrics.
All 3,164 expected ticker/metric rows are classified: 1,146 `REPORTED`, 284
`PROXY`, 25 `EXEMPT`, 767 `NOT_APPLICABLE`, 939 `NOT_DISCLOSED`, three
`DISCLOSED_UNPARSED`, and zero `PARSER_FAILURE`. Applicable coverage is
1,430/2,372 (60.29%). All 22 calibration metrics pass their configured
coverage gates. `book_to_bill` passes exactly at its minimum-count gate of
10/89 and `roic` passes at 79/112, including 5/19 in the development-stage
cohort. Three of six
limited-universe diagnostics are ready; strict funded-backlog metrics remain
structurally inapplicable to the current reviewed universe.

The historical panel contains 1,900 validated trading dates from 2019-01-02
through 2026-07-24 and 214,976 ticker-date observations. The 51-name delisted
seed contains 25 pre-2019 out-of-scope names and 26 in-scope candidates.
Twenty-three in-scope tickers have resolved point-in-time membership.
`ELMS`, `GOEV`, and `RIDE` remain explicit fail-closed identity exceptions
because a valid Norgate issuer mapping is unavailable.

The recovery ledger classifies all 942 missing applicable observations:
three high recoverability, 794 medium, and 145 low. The high-recoverability
cells are `GRC` reported backlog and its two derived metrics. Most remaining
gaps are issuer non-disclosure or missing derivation operands, not unclassified
parser failures.

Dedicated-parser release `0.4.6` is production-enabled for machinery. Full
active-universe regression run 36 covered all 4,403 cached accessions for 113
active tickers with zero cache gaps and zero failures. It was not promoted
wholesale because validation found three false MWA orders observations.
Adapter `v3.6` now rejects that ASC 606 narrative pattern deterministically.
Bounded runs 38, 39, and 41 then validated the reviewed MWA, FLS, DOV, MAIR,
OUST, and WAB corrections with zero failures and zero promotion conflicts.
Promotions 9, 10, and 12 published only high-confidence consolidated facts and
persisted the deterministic suppressions. No structural applicability
override was created without a reviewed policy.

The read-only historical promotion preflight passed with fingerprint
`bc2f7a17a2df2c6e27a951e2a608a53c11c8b105aee7607e39ff18d9233a8b34`.
It credits none of the unmaterialized promotion gains and removes every
observation potentially exposed to a selected suppression. Twenty-one of 22
calibration metrics retain production-candidate historical depth across all
10 signal categories. `book_to_bill` remains diagnostic-only because only 27
dates in one calendar year meet its 10-name/10% cross-sectional gate.
Promotions 9, 10, and 12 can affect 689 partitions from 2023-10-24 through
2026-07-24 for `DOV`, `FLS`, `MAIR`, `MWA`, `OUST`, and `WAB`; the other
1,211 validated partitions do not require recomputation. The bounded
materializer subsequently passed all 689/689 affected partitions, preserved
the 1,211 unaffected partitions, passed combined coverage across all
1,900 dates, and passed the industrial-family portfolio adapter on all
1,900 dated files. Eleven bounded smoke/benchmark partitions were reused by
matching the same fingerprint; 678 were newly materialized.

The full coverage table, per-ticker missingness, parser comparison, historical
result, and remaining gates are recorded in
`ALL_METRICS_REVIEW_2026-07-24.md`.

Stage 8 calibration and Stage 9 portfolio validation are sealed and pass.
Stage 12 is active as of 2026-07-24. The production contract separates 99
broad OOS-valid names from the validated top-quintile sleeve and maps exactly
20 selected candidates to 20 investable portfolio-adapter rows.

## Stage Checklist

| Stage | Scope | Implementation | Production data | Acceptance gate |
| --- | --- | --- | --- | --- |
| 0 | Seed, source registry, schema | Complete | Passed | Seed validator passes for 113 active and 51 delisted candidates; Ruff and Pyright pass. |
| 1 | Active universe, aliases, listing dates, historical membership | Complete | Passed 2026-07-24 | Exactly 113 active members; PIT intervals do not overlap incorrectly; defense rows unchanged. |
| 2 | Identity and Norgate reconciliation | Complete | Passed current date | Every included historical ticker has separate `actual_ticker` and `norgate_symbol`; ambiguous or reused symbols fail closed. |
| 3 | Adjusted prices and market features | Complete | Passed current date and full daily history | Active coverage and each eligible post-2019 historical member have PIT prices or an explicit exclusion; market-stage validator passes. |
| 4 | SEC fundamentals, FX, footnotes, and financial features | Complete | Passed 2026-07-24; 22/22 calibration coverage gates pass | Exactly one status for every required ticker/metric pair; generic and 28-metric audits pass; the recoverability ledger classifies every missing applicable cell; no future filing or period leakage; units and currencies reconcile. |
| 5 | SEC ownership, 13F, FINRA, IBKR, positioning | Complete | Passed current date | All three databases refresh in order; family-scoped import passes; missing feeds remain explicit. |
| 6 | Scoring feature contract and eligibility | Production implementation complete | Passed current date | One row per PIT member; zero is distinct from missing; required portfolio and calibration fields pass validation. |
| 7 | Scores and ranks | Complete and activated | Passed 2026-07-24 | Scores are deterministic, ranks contiguous, missing metrics reduce confidence, and the sealed production selection policy controls investment/OOS gates. |
| 8 | Signal diagnostics, constrained calibration, walk-forward OOS | Complete | Passed 2026-07-25; Stage 9 readiness `READY` | The 2019-01-02 through 2025-12-31 development panel contains 366 weekly snapshots and 41,464 rows; the 2026 lockbox was not read; 96 constrained trials and seven expanding walk-forward blocks pass the configured IC, stability, turnover, coverage, and leakage gates. |
| 9 | Portfolio backtest and capacity analysis | Complete | Passed 2026-07-25; Stage 12 readiness `READY` | D+1 adjusted-open, non-overlapping, net-of-cost replays pass return, drawdown, turnover, concentration, cohort breadth, ADV coverage, and 5x deployed-AUM capacity gates. The production policy exactly matches all 26 validation/holdout period memberships and weights. |
| 10 | Dashboard and portfolio-layer handoff | Production contract active; shadow sidecar retained | Passed current 2026-07-24 and all 1,900 dated files | Production rank file and shadow calibration sidecar pass their contracts; all financial values, provenance, USD fields, and availability statuses required for calibration are present. |
| 11 | PIT history from 2019-01-02 | Complete | Passed 1,900/1,900 dates after 689/689 targeted promoted-fact materializations | Every scheduled date publishes an immutable survivorship-corrected file and manifest; each file passes the portfolio adapter; combined active/delisted coverage passes using the finalized promoted source facts. |
| 12 | Governance lock and production promotion | Complete and active | `ACTIVE` as of 2026-07-24 | The sealed dashboard passes 113 rows/99 broad eligible/20 selected/20 adapter-investable. The bounded strategic smoke passed exact membership, equal weights, the 5% cap, required downstream groups, and the final-book manifest. The persistent daily state is hash-sealed and fails closed on source or evidence changes. |

## Machinery Financial Metrics

### Existing Generic Metrics

The shared industrial financial layer already produces revenue, gross profit,
operating income, net income, operating cash flow, capex, free cash flow,
cash, debt, inventory, receivables, payables, TTM values, margins, growth,
cash conversion cycle, valuation ratios, RPO, and confidence/provenance fields.

### Priority 0: Required Before Calibration

These metrics are implemented in the financial and scoring contracts and must
pass the coverage audit before Stage 8:

| Metric | Definition | Permitted source | Required safeguards |
| --- | --- | --- | --- |
| Orders/bookings | New firm orders accepted during the fiscal period | Explicit issuer XBRL fact or filing table/text disclosure | Do not infer from revenue or backlog change; preserve period and filing dates. |
| Orders growth | PIT YoY change in comparable-period orders | Derived from validated orders | Comparable duration and currency; denominator must be non-zero. |
| Funded backlog | Firm unfilled orders at period end | Explicit backlog disclosure | RPO is not a substitute; distinguish total, funded, cancellable, and unfunded backlog. |
| Backlog growth | PIT YoY change in comparable backlog | Derived from validated backlog | Same backlog definition and reporting scope across periods. |
| Book-to-bill | Period orders divided by comparable-period revenue | Derived from orders and revenue | Numerator and denominator must share period duration, currency, and consolidation scope. |
| Backlog/revenue | Funded backlog divided by TTM revenue | Derived | Same currency; denominator quality must pass. |
| Invested capital | Debt plus equity less excess cash, with documented policy | Standard statements | Use PIT balance-sheet values and explicit FX treatment. |
| ROIC | NOPAT divided by average invested capital | Derived | Tax-rate policy, average-capital periods, and negative-capital handling must be explicit. |
| Asset turnover | TTM revenue divided by average assets | Derived | Average beginning/end assets; no mixed periods. |
| Incremental margin | Change in operating income divided by change in revenue | Derived | Comparable periods; suppress immaterial or negative revenue denominators according to policy. |
| Inventory-sales spread | Inventory growth minus revenue growth | Derived | Comparable periods and currencies; retain inventory write-down quality flags. |
| Cash-conversion-cycle change | Current CCC minus prior comparable CCC | Derived | All three CCC operands must pass period-alignment checks. |
| Net leverage | Net debt divided by EBITDA or approved operating-profit proxy | Standard statements plus derived EBITDA | Never silently substitute operating income for EBITDA; label proxy use. |
| Interest coverage | EBIT or EBITDA divided by interest expense | Standard statements | Sign normalization and near-zero denominator policy required. |
| Capital-raise dependence | Gross TTM equity and debt issuance proceeds divided by TTM cash burn | Explicit cash-flow-statement issuance facts | Require both issuance components for scoring; preserve a one-component lower bound only as a flagged diagnostic; do not infer absent proceeds as zero; undefined for non-burning firms. |

### Development-Stage Metrics

The development-stage cohort additionally requires cash burn, cash runway,
share-count dilution, SBC/revenue, and capital-raise dependence. Capital-raise
dependence uses only explicit equity/debt issuance proceeds and is null unless
both component classes are available. These metrics enter only the development-stage
risk modifier and that cohort's confidence denominator; they are not mixed into
mature-company peer percentiles without cohort-specific calibration.

### Research-Only Metrics

Aftermarket/service revenue share, organic growth, price/cost realization,
end-market mix, geographic mix, and customer concentration remain research
signals until extraction coverage and definition stability are demonstrated.

## Metric Contract Rules

1. Every raw metric must retain ticker, accession, form, filing/acceptance date,
   fiscal period, unit, currency, source concept/label, and source detail.
2. Facts become visible only when the filing was public on or before the score
   date. Later restatements cannot overwrite prior PIT knowledge.
3. Missing is stored as null/blank, never zero. Zero is a valid observation and
   must survive CSV serialization.
4. RPO, deferred revenue, bookings, and funded backlog are separate concepts.
   No fallback may relabel one as another.
5. Derived metrics must validate period, duration, unit, currency, and
   consolidation scope for every operand.
6. A sparse metric may contribute only when present. Missingness lowers metric
   and component confidence; it must not create a favorable neutral score.
7. A signal receives production weight only after PIT coverage, IC, stability,
   and walk-forward gates pass. Otherwise it remains diagnostic or shadow-only.
8. ROIC remains null and carries `roic_not_meaningful_flag=1` when invested
   capital is non-positive. Net debt/EBITDA remains null and carries
   `negative_ebitda_leverage_flag=1` when EBITDA is non-positive. Neither case
   receives a fabricated zero or sentinel ratio.

## Metric Availability Contract

Stage 4 writes `feature_financial_metric_availability` and
`financial_metric_availability.csv`. For each PIT member and each of the 28
required metrics, the record retains the value/status, unit, accession, filing
and period dates, taxonomy/concept, extraction method, confidence, reason, and
operand provenance. The dated final rank and Stage 11 calibration files also
carry all 28 status columns, the exact availability as-of date, source amounts
in USD, and filing/reporting quality fields. Publication fails if any status is
blank/invalid, any classification is stale, or the classified fraction is not
1.0.

Historical feature rebuilds write coverage reports under
`historical_backfill/stage_reports/<asof>/` and suppress latest-state quality
issue mutations. This prevents a historical run from overwriting current
coverage files or contaminating current acceptance gates. Each rebuild first
creates an exact reporting-profile snapshot from locally stored SEC data; it
does not perform one SEC network refresh per historical date. Historical
research applies the policy table frozen at
`machinery_scoring.historical_policy_lock_date`, and records that lock date in
the backfill report.

Stage 4 also writes the machinery-only
`fact_machinery_metric_recovery_evidence` ledger and the
`machinery_metric_recovery_evidence.csv`,
`machinery_metric_recovery_queue.csv`, and
`machinery_metric_recovery_summary.json` reports. Every missing applicable
cell is assigned a deterministic evidence class, recoverability level, and
source lane. The ledger distinguishes a current accepted fact that failed
projection from old history, period-alignment gaps, registration-statement
opportunities, unresolved filing prose, issuer-IR searches, and source/parser
failures. This classification is diagnostic: it never fabricates a metric or
weakens a scoring guardrail.

## Reviewed Issuer Overrides

The July 10, 2026 SEC review establishes two fail-closed handling classes:

- `Is_Development_Stage`: `AIRJ`, `FISN`, `NNE`, and `OKLO`. Missing revenue
  may become an explicit zero only when the issuer is in the development-stage
  cohort and the comparable operating-cash-flow fact is negative. Otherwise
  revenue remains missing and the row fails financial review.
- `Ingestion_Gap_Pending`: `INIO` and `MAIR` retain this routing override so
  future refreshes continue to parse their registration/predecessor statements
  in strict mode. The July 13 extraction succeeded and promoted both effective
  profiles to `SEC_ARCHIVE_TEXT_TABLE`; neither receives a development-stage
  or zero-revenue fallback. Ranking and calibration remain review-gated until
  the archive-text PIT history QC gate passes.

`FISN` membership begins on its verified Nasdaq trading date, `2026-06-18`.
`MAIR` has no eligibility before IPO pricing on `2026-04-15`; its first quoted
and eligible date remains `2026-04-16`.

## Correct Execution Sequence

The enhanced extraction, historical reconciliation, Stage 8/9 validation,
portfolio-policy parity, governance lock, publisher, and fail-closed
activation transaction are complete. The continuing sequence is:

1. Do not rerun the 2026-07-24 activation or historical snapshots. The active
   state and completed portfolio smoke are hash-sealed.
2. Run normal incremental machinery refreshes for later completed trading
   dates. The scorer reconstructs the approved policy from the sealed state
   and fails closed if activation evidence or production source code changes.
3. Continue optional-disclosure recovery as maintenance work without changing
   the sealed model unless a new version repeats Stages 8 and 9. `book_to_bill`
   remains diagnostic-only until a later fingerprinted history preflight
   clears its depth gate.

## Current Evidence

- Current rebuild `machinery_refresh_20260725T180923Z` passed all 12 selected
  financial, scoring, publishing, dashboard, and industrial-family portfolio
  smoke stages after production promotion.
- Production Stage 4 contains 113 feature rows and 3,164 classified metric
  rows. The strict audit passes all 22/22 calibration metrics. The full
  all-metrics result is `ALL_METRICS_REVIEW_2026-07-24.md`.
- The production dashboard has 113 rows, including 99 rank-ready and
  research-eligible rows and 20 investable rows. The retained shadow
  calibration sidecar includes every required financial value, provenance
  field, USD amount, and all 28 availability statuses.
- Dedicated-parser full-universe run 36 completed the 4,403-accession scope
  with zero failures. Bounded production runs 38, 39, and 41 passed both
  reviewed golden corpora; promotions 9, 10, and 12 reported zero conflicts.
  The MWA revenue-contract narrative is now deterministically suppressed.
- The historical promotion preflight passed without publishing or rebuilding
  any partition: 21 production-candidate metrics, one diagnostic-only metric
  (`book_to_bill`), 10/10 signal categories represented, 689 affected
  partitions, and 1,211 unaffected partitions.
- The bounded promotion materializer passed 689/689 affected partitions with
  zero failures under the approved fingerprint. Combined coverage passed
  1,900/1,900 dates and the industrial-family portfolio adapter passed all
  1,900 dated files. The materialization report records 678 newly processed
  dates and 11 fingerprint-matched reused dates.
- Source coverage improved from the pre-promotion snapshot as follows:
  orders 14/94 to 17/94, total RPO 47/90 to 59/91, current RPO 34/90
  to 37/90, reported backlog 37/95 to 38/95, book-to-bill 6/89 to 10/89,
  ROIC 78/112 to 79/112, RPO YoY 44/90 to 51/91, and RPO-implied
  orders/book-to-bill 40/85 to 46/85.
- The historical panel passes 1,900/1,900 dates and contains 214,976
  ticker-date observations. Combined coverage includes 113 active and 23
  resolved delisted tickers. Finalized promoted parser facts are materialized
  in every preflight-identified partition, so Stage 8 may begin.
- Stage 8 passed on 366 weekly snapshots and 41,464 survivorship-corrected
  rows. All 96 constrained trials and seven expanding walk-forward blocks are
  hash-sealed under `output/industrials/machinery/stage8`. The selected
  candidate passed validation and untouched holdout gates; the walk-forward
  candidate win rate is 71.43% with positive mean objective improvement.
- Stage 9 passed 2,000 non-overlapping portfolio periods and 56,494
  holdings/trade rows under `output/industrials/machinery/stage9`. Strategy
  choice used validation data only and selected `long_only_q20_equal`.
  Holdout D+1 adjusted-open annualized return was 14.95% versus 11.11% for
  XLI, with 3.84 percentage points annualized excess, -23.73% maximum
  drawdown, 36.05% average one-way turnover, 100% ADV coverage, and $5.52M
  10th-percentile capacity versus $300K configured AUM.
- Both strict Stage 8/9 validators report zero issues. Those calibration and
  backtest stages remained report-only and accessed no 2026 lockbox outcome;
  the later approved Stage 12 transaction performed production activation.
- Stage 9 production-policy parity passes all 26 validation and holdout
  periods with exact membership and weight reconstruction.
- Stage 12 passed with 113 production rows, 99 broad OOS-valid rows, and exact
  20 selected/20 adapter-investable reconciliation. The optimizer retained all
  20 selected names at equal 0.20817883% portfolio weights, totaling 4.1635766%
  under the 5% machinery cap.
- Script 25 completed the 2026-07-24 activation using hash-validated portfolio
  prefix evidence and a bounded ledger-through-final smoke. The final manifest
  passed, the persistent daily-policy state is `ACTIVE`, and no historical or
  macro rebuild was needed. The dashboard/sidecar contract reseal also passed
  independently with 76 required calibration financial fields.
- `GTLS` ends on 2026-07-16 and is absent from later active partitions. It is
  retained as historical/delisted with successor `BKR`.
- SQLite `PRAGMA quick_check` returned `ok`; the database contains 113
  distinct current machinery members, 113 financial rows, and 3,164
  availability rows as of 2026-07-24.
- Release validation passed: the prior 165 dedicated-parser/machinery tests
  plus the current 115-test machinery/portfolio suite, Ruff, explicit Pyright,
  strict Stage 8/9 validators, and the 22/22 current metric audit completed
  without errors.
- No defense implementation file was modified by this update.

## Independent Parser Development History

The pilot and 50-ticker benchmark notes below are retained as implementation
history. Their universe counts and next-step statements are superseded by
active-universe run 36, bounded production runs 38/39/41, and
`ALL_METRICS_REVIEW_2026-07-24.md`.

The repository-level `dedicated_parser` package is implemented in shadow mode.
It reuses the existing SQLite facts and 20.6 GB SEC archive rather than
creating another filing store. EdgarTools reads local SGML attachment
structure, Arelle supplies XBRL contexts and dimensions, and the machinery
adapter retains issuer-specific acceptance and rejection policy. Release
`0.2.3` added semantic HTML tables, XBRL concept metadata, current-RPO explicit
amount/percentage recovery, timing-dimension aggregation, PDF failure
classification, source-window completeness, and exhaustive recovery classes.
Release `0.3.0` adds exact versioned review policies to work hashes,
policy-generated golden expectations, assessment-only execution, an extraction
funnel, a fast complete-cache gate, and explicit cache hydration through the
existing machinery SEC synchronizer. All 17 present reviewed decisions from
the manual corpus are registered; seven rejections also generate explicit
prohibited-acceptance checks, producing 24 generated expectations.

The final ten-ticker production-cache pilot processed 353 accessions and 803
documents with four workers: 353 completed and zero failed. All 19 positive
and prohibited-row golden expectations passed. The reviewed 13-accession
subset produced exactly 98 evidence keys with both one and four workers; the
parallel run took 15.3 seconds versus 49.3 seconds serially.

Current source-metric coverage before and after shadow policy is:

| Source metric | Baseline | Corrected shadow | Interpretation |
|---|---:|---:|---|
| Orders | 3/9 | 2/9 | FLS baseline orders is a rejected segment/adjustment value |
| Total RPO | 6/9 | 6/9 | No current-cell gain |
| Reported backlog | 7/9 | 7/9 | No current-cell gain |
| Current RPO | 4/8 | 5/8 | POWL recovered from explicit 12-month amounts |
| Funded backlog | 0/0 | 0/0 | Structural N/A for this pilot |

Across all 50 ticker/source-metric cells, the final run classified 16 as
confirmed reported, one as newly recovered, 15 as structural N/A, four as
ambiguous, six as not found in searched documents, three as source-document
incomplete, three as baseline reported but unconfirmed, one as policy-rejected,
and one as a baseline policy correction. The 12 missing cache accessions are
all BLDP (eight) or SHMD (four) within the ten-ticker pilot; no cell for those
incomplete source windows is treated as conclusive issuer non-disclosure. The
The initial full-active-universe cache audit found 33 missing accessions across ATS,
BLDP, KRNT, SHMD, and SSYS. This is a scope difference, not a regression in the
pilot count.

The pilot proves the shared parser architecture and accuracy controls, but it
does not show a broad automatic coverage uplift. Net current coverage is flat:
one legitimate POWL recovery is offset by removal of the invalid FLS orders
baseline. The broader hydration and 50-ticker benchmark described below
completed the next evaluation gate. Repeated full-history parsing without new
source documents or reviewed policies is not justified.

The `0.3.0` assessment-only smoke test reassessed run 12 in 1.3 seconds,
preserved its 353 completed/zero-failed work counts, and reproduced all 50
recovery classifications. Its funnel contains 986 evidence rows: 200 XBRL
mapping, 512 semantic-table, 33 semantic-text-derivation, and 241 prose-text
rows. A full 114-ticker cache audit completed without parsing or run
allocation and identified the initial 33 source gaps above.

The broad parser benchmark is complete. A deterministic cohort selected the
50 active tickers with the most unresolved parser-supported metrics as of
`2026-07-22`, totaling 155 unresolved source-metric cells. Its initial cache
audit found 27 missing accessions across `ATS`, `BLDP`, `KRNT`, and `SHMD`.
The existing machinery SEC synchronizer hydrated all 27 after the archive
window was aligned with the parser's 40-filing window. The complete run
processed 1,915 accessions and 5,277 documents with zero failures. The six
remaining full-universe cache gaps belong to `SSYS`, which was outside the
frozen 50-ticker benchmark.

The run produced 1,009 evidence rows and classified all 250 ticker/source-
metric pairs. Real-filing validation found one false total-RPO promotion:
VRT's `$107.6M` represented only noncurrent deferred revenue timing buckets.
Machinery adapter `v2.4` now leaves future-only or otherwise incomplete timing
schedules in `REVIEW_REQUIRED`. A fail-fast runtime gate also prevents Arelle
or EdgarTools from being silently omitted when enabled.

Corrected current coverage across the benchmark's 200 applicable
non-funded-backlog cells improves from 38/200 to 39/200. TTC total RPO is the
only new current recovery, five metric chains have historical-only evidence,
19 cells remain ambiguous, and six baseline-reported cells are unconfirmed.
This is a 0.65% automatic current recovery rate across the 155 initially
unresolved cells. The shared parser remains useful for accurate
classification, provenance, and targeted recovery, but this result does not
support broad automatic parser expansion as a coverage strategy. The next
bounded action is review of those 25 priority cells, not another unchanged
historical parse.

The machinery orchestrator exposes this as the opt-in
`08d_dedicated_parser_shadow` step. It does not update production financial
features, scoring, dashboard files, portfolio inputs, or historical snapshots.
Promotion remains blocked until the source gaps and ambiguous cells are
resolved, shadow additions/corrections are approved, and the full current-date
machinery and portfolio-layer smoke passes with the shared backend enabled.

### Fifty-Ticker Priority Review - 2026-07-24

The bounded review of all 25 ambiguous or baseline-unconfirmed benchmark cells
is complete. Adapter `machinery_specialized_metrics_v2.7` and 41 reviewed
policies now classify every priority cell. Targeted runs 18 and 19 processed
27 affected accessions and 168 documents with zero failures; the 1,915-
accession benchmark was not rerun.

Accuracy-adjusted coverage is 42/200 applicable source-metric cells (21.0%),
versus 38/200 (19.0%) before the dedicated parser. Six current observations
were recovered, while two invalid baseline observations were identified:
`AEBI` reported backlog is acquisition purchase-accounting fair value and
`ASTE` orders covers only the subset recognized over time. Total RPO improved
from 17/50 to 22/50; reported backlog remains 11/50 after one recovery and one
removal; orders declines from 4/50 to 3/50 because the ASTE value is invalid;
current RPO remains 6/50.

The reviewed output remains shadow-only. The next gate is a controlled,
auditable promotion path that applies accepted additions and explicit
corrections, regenerates only affected ticker/date partitions by filing
acceptance date, and runs the complete machinery and portfolio-layer smoke
without modifying defense data.

### Active-Universe Result

Run 25 supersedes the pilot counts above. It evaluated all 113 active tickers
and all five parser-owned source families against a complete local source
window. The work ledger contains 4,403 completed accession jobs; the final
increment processed 210 accessions with zero failures. All 74 reviewed
golden-corpus expectations passed.

Production covers 134/369 applicable source cells; reviewed parser evidence
predicts 149/369. The 15 additions consist of one orders observation, nine RPO
observations, two reported-backlog observations, and three current-RPO
observations. Eleven observations remain ambiguous, 30 production observations
were not independently confirmed, eight candidates were policy-rejected, and
six recoveries are historical-only. These classifications make a controlled
promotion review necessary; they do not justify automatic replacement of
production values.
