# Transportation v5 efficient implementation sequence

> **Current audited state:** the v6 correction dated 2026-08-16 supersedes
> the earlier v4 diagnostic figures retained later in this document for
> lineage. V6 fixes financial-period/source selection and ranking diagnostics;
> it does not authorize production.

## Objective

Build transportation once from the already-loaded, semantically reviewed data and then evaluate two economically isolated score lanes:

- `north_american_surface_freight_and_logistics_v5`
- `oil_tanker_operators_v5`

The implementation must not re-run the dedicated parser, select issuers from observed returns, admit delisted issuers to the current portfolio, or activate production before untouched evidence passes.

## Governed universe

Current portfolio candidates remain exactly 35 issuers:

- 24 surface-freight issuers
- 11 oil-tanker issuers

The point-in-time reconstruction adds nine outcome-blind historical comparables only:

- Surface: `CGI`, `DSKE`, `ECHO`, `GWR`, `KSU`, `USAK`, `USX`, `YELL`
- Tankers: `OSG`

These rows carry the role `historical_calibration_only_no_portfolio_eligibility`. The score engine admits them only when `membership_mode=pit`; current serving cannot admit them. `YELL` is bounded at 2023-07-28 rather than the provider's 2026 OTC terminal quote.

`NNA` is excluded. It has zero complete required-metric snapshots. A local test of `us-gaap:OperatingCostsAndExpenses` was semantically rejected because it was a partial expense line and implied an implausible 96% operating margin. The test mapping and all rows it created were rolled back.

## Frozen specialized metrics

No additional parsing is authorized for this sequence.

Surface retains only the strict, semantically accepted domains:

- `operating_ratio`
- `purchased_transportation_ratio`
- `freight_weight_per_shipment`
- `shipment_or_load_growth`
- `pricing_or_yield_growth`

Tankers retain only `fleet_age`. Missing optional values receive the fixed neutral score of 50; denominators do not renormalize around missing metrics.

## Acceptance gates

| Gate | Requirement | Current evidence |
|---|---|---|
| Semantic materialization | Reviewed, conflict-free candidates only; no parse | PASS, 1,123 candidates |
| Current input readiness | All 35 names have all nine rank-required metrics | PASS, 315/315 |
| Current score plumbing | Two disjoint policies, 35/35 rank-ready, active v4 unchanged | PASS |
| Prebuild freeze | Exact universe, source slices, code hashes, lifecycle bounds | PASS |
| Surface historical source breadth | At least 20 names on at least 48 dates | PASS, 92 source-ready dates; 80 exact required-metric dates |
| Tanker historical source breadth | At least 8 names on at least 36 dates | PASS, 80 source-ready dates; 76 exact required-metric dates after the DHT source repair |
| Historical reconstruction | All 92 dates, exact 44-name PIT scope, 41 metrics per effective ticker | PASS; surface 80/48 dates, tankers 76/36 dates |
| Historical score history | Exact-date monthly scores, separate surface/tanker policies, no historical portfolio leakage | PASS, 92 snapshots and 3,477 score rows |
| Calibration | Preregistered candidates, three chronological blocks, no reuse of spent promotion evidence | COMPLETE diagnostic; neither cohort passed all three blocks |
| Production | Positive return, nonnegative IC, holdout and walk-forward gates, independent promotion audit and lock | Fail closed until untouched evidence passes |

## Single bounded rebuild

The current immutable contract is:

`output/industrials/transportation/investable_v5/prebuild_contract_v6/2026-08-16/transportation_v5_prebuild_contract.json`

It pins:

- the 44-name current-plus-historical scope;
- both cohort policies;
- the metric registry and current score evidence;
- the exact price and canonical-financial source slices;
- every shared and transportation-specific rebuild entrypoint.

The PIT builder now accepts `--tickers`, passes the same scope to every shared stage, deletes metric-availability rows only for selected tickers, and writes a scope hash into every progress row. It is resumable by date. This changes no default behavior for defense, machinery, or any unfiltered industrials run.

## Correct execution order

1. Freeze the prebuild contract with script `38j`.
2. Run one 44-name, 92-date reconstruction with script `19`.
3. Run script `38k`; do not score if it fails.
4. Run script `38l` once. It materializes monthly positioning through the shared industrials adapter, scores both lanes from exact-date snapshots, and emits an explicit calibration-readiness sidecar. Historical comparables always carry `current_portfolio_eligibility_authorized=0`.
5. Run script `38m`. It independently checks every dated hash, exact PIT scope, cohort isolation, historical-only flags, score-ready breadth, positioning history, and the absence of portfolio leakage.
6. Run script `38n` to freeze the outcome-blind candidate registries and the diagnostic-only research protocol. Positioning candidates are admitted only if the 38m positioning-history gate passed.
7. Run script `38o` to build forward outcomes from the already-pinned raw price slice. It deterministically derives adjusted opens, preserves aliases and reviewed terminal events, and performs no fetch.
8. Run script `38p` to independently reconstruct every available security return, benchmark return, and excess return from the normalized frozen slice before any candidate is evaluated.
9. Run script `38q` to evaluate the preregistered candidates separately for surface freight and tankers at 21- and 63-session horizons, including three chronological stability blocks.
10. Treat all pre-freeze/revealed outcomes as diagnostic only.
11. Promote a lane only through readiness audit, promoter, immutable production lock, activation, and portfolio-layer validation.

Scripts `38l` and `38m` do not parse filings, fetch market data, inspect outcomes, calibrate weights, or authorize production. Scripts `38n` through `38q` reuse those immutable artifacts and the pinned price slice; none can authorize production from historical diagnostics. This keeps the expensive feature reconstruction exactly once and separates data readiness, diagnostic model evidence, and future-only promotion evidence.

Production remains on the existing v4 configuration until the final promotion gates pass. A successful historical rebuild is not itself authorization to allocate capital.

## Completed execution evidence (2026-08-15)

The expensive 44-name, 92-date feature reconstruction was executed once. Two bounded repairs were then applied only to deficient financial/availability snapshots:

- `TRMD`: a ticker-scoped IFRS revenue alias materialized 65 already-loaded SEC CompanyFacts observations. The identical concept remains unmapped for passenger issuers.
- `ASC`: six reviewed annual operating-income bridges for FY2019-FY2024 were derived from seven hash-locked cached annual filing tables and independently reconciled. FY2025 exactly reproduced the existing reviewed value.
- Delta rebuild: 122 snapshots were preserved (51 `TRMD`, 71 `ASC`), including bounded canaries; 70 remaining ASC dates were rebuilt in the final run. Network requests and parser invocations were both zero.

The final immutable prebuild contract is `transportation_v5_bounded_prebuild_v4`. Older contracts remain unchanged as historical records. The v4 contract pins the exact price slice, repaired canonical-financial slice, score policies, ticker-scoped mapping, reviewed ASC policy/evidence, delta reports, and load-bearing rebuild code. V4 also contains the backward-compatible historical-builder API fix verified by the complete transportation regression suite.

PIT scoring and outcome validation results:

- Score history: PASS, 92/92 snapshots, 3,477 rows, 381 historical-only rows.
- Positioning-history gate: PASS for both cohorts, 92/92 eligible dates.
- Current-ticker contribution: PASS, zero current tickers without score or outcome contribution.
- Historical-only noncontributors: `CGI` (never cleared the frozen liquidity gate) and `GWR` (lifecycle ended before the 12-month warm-up produced a cohort-ready date). Both remain explicit, have zero calibration rows, and can never enter a portfolio.
- Outcome panel: PASS, 6,954 rows; 6,740 available outcomes independently reconstructed with maximum absolute error `5.12e-13`.
- 63-session outcome coverage: surface 95.14% across 76 ready dates; tankers 88.04% across 39 ready dates.

Diagnostic 63-session candidate results:

| Cohort | Diagnostic candidate | Mean IC | Mean top-sleeve net excess | Hit rate | Stability verdict |
|---|---:|---:|---:|---:|---|
| Surface freight | `surface_quality_efficiency_v5` | 0.0324 | 0.66% | 51.32% | FAIL; blocks 2 and 3 fail |
| Oil tankers | `tanker_quality_fleet_v1` | 0.0047 | 5.04% | 58.97% | FAIL; blocks 2 and 3 fail |

These aggregates are diagnostic only. Neither lane demonstrated stable ranking power across all three chronological blocks, and the protocol explicitly forbids using this revealed history for production promotion. No additional parsing, historical reconstruction, or in-sample candidate redesign is authorized by these results. The next evidence-producing step is future-only shadow capture after the 2026-07-30 cutoff using the frozen policies and candidates.

## Corrected v6 implementation and audit (2026-08-16)

The prior aggregate/block presentation was not a reliable statement of
cross-sectional ranking power. V6 corrects the data and evaluation contracts
before drawing a conclusion.

### Defects corrected

1. Duration facts embedded in a 10-K were being treated as annual solely from
   their form. Annual duration facts must now span 300-400 days; an unknown
   duration is annual only when both the form and fiscal-period label are
   annual.
2. Previous-period growth inputs were not required to be approximately one
   year apart. Revenue, gross profit, operating income, and free-cash-flow
   growth now fail closed on non-comparable periods. Transportation also
   rejects an absolute annual growth rate above 100% unless a reviewed
   structural bridge exists.
3. A generic `sec-text` fact could compete with a valid fact from the issuer's
   promoted XBRL taxonomy. When IFRS or US-GAAP supplies a metric, duplicate
   generic archive-text facts are now excluded. Non-duplicative supplements,
   reviewed facts, and explicit machinery fallbacks remain available.
4. Diagnostic blocks are now fixed calendar periods, not equal-sized slices of
   whatever observations happen to be available:
   `2019-01-01..2021-12-31`, `2022-01-01..2023-12-31`, and
   `2024-01-01..2026-07-30`.
5. Ranking and investability are separate. A candidate must demonstrate
   nonnegative IC, positive top-minus-cohort net return, and positive
   top-minus-bottom gross return for ranking; positive net excess versus IYT
   and a hit rate of at least 50% test benchmark investability. A block also
   requires at least six non-overlapping 63-session observations.
6. The full candidate path is evaluated once before calendar slicing, so
   turnover and transaction costs do not reset at block boundaries. Aggregate
   history is descriptive only and can never be labeled PASS.

### Efficient execution

The 44-name panel was not reparsed or refetched. After the full corrected
feature build, the remaining DHT defect was repaired across all 92 historical
dates with `--stage-tickers DHT`; the driver retained the complete 44-name
validation scope and wrote the one-name execution scope separately. The
targeted run took about 68 seconds, invoked no parser, and made no network
request. The live 2026-08-13 DHT row was also rebuilt separately.

DHT now has historical revenue between `$295.853M` and `$691.039M`, with zero
tiny-revenue rows and zero revenue or operating-income growth observations
above the 100% automatic threshold through 2026-07-30. Its current 2026-08-13
revenue is `$498.4M` and revenue growth is `-12.83%`; the former false
`$3,000` archive-text value no longer reaches financial features.

### V6 acceptance results

- Prebuild freeze: PASS, 44 tickers and 92 dates.
- Historical rebuild validation: PASS; surface 80 ready dates versus 48
  required, tankers 76 versus 36.
- Score validation: PASS; surface 80 score-ready dates, tankers 55; positioning
  92/92 for both cohorts; zero current noncontributors.
- Outcome reconciliation: PASS; all 6,740 available returns independently
  reconstructed, maximum absolute error `5.12e-13`.
- 63-session outcome breadth: surface 76 ready dates at 95.14% coverage;
  tankers 51 ready dates at 90.62% coverage.
- Production activation: FAIL CLOSED because neither cohort passes all fixed
  calendar blocks. This is now a model-evidence result, not a data-lineage or
  arithmetic failure.

### Corrected 63-session diagnostics

Surface selected candidate: `surface_balanced_v5`.

| Period | IC | Top vs IYT net | Top vs cohort net | Top minus bottom | Non-overlap | Result |
|---|---:|---:|---:|---:|---:|---|
| Aggregate, descriptive | +0.0387 | +1.10% | -0.46% | -0.21% | 19 | No stable ranking power |
| 2019-2021 | +0.0297 | +2.03% | -1.82% | -5.41% | 7 | Ranking FAIL |
| 2022-2023 | +0.0769 | +0.73% | +0.70% | +4.64% | 6 | Investability FAIL; hit rate 45.83% |
| 2024-2026-07-30 | +0.0131 | +0.56% | -0.24% | +0.28% | 7 | Ranking FAIL |

This resolves the apparently contradictory surface result: the top sleeve
could beat IYT while losing to the transportation cohort and/or its bottom
sleeve. That is benchmark exposure, not dependable score discrimination.

Tanker selected candidate: `tanker_quality_fleet_v1`.

| Period | IC | Top vs IYT net | Top vs cohort net | Top minus bottom | Non-overlap | Result |
|---|---:|---:|---:|---:|---:|---|
| Aggregate, descriptive | -0.0175 | +5.33% | -0.30% | +0.15% | 14 | No stable ranking power |
| 2019-2021 | +0.4935 | -24.04% | +4.58% | +16.00% | 2 | Investability and sample FAIL |
| 2022-2023 | +0.0643 | +15.29% | +0.71% | +2.50% | 6 | PASS |
| 2024-2026-07-30 | -0.1538 | +2.31% | -1.77% | -3.94% | 7 | Ranking FAIL |

The legal next gate is future-only shadow evidence after 2026-07-30 using the
frozen cohorts, features, candidates, and verdict criteria. The revealed
historical panel must not be used for further membership selection, weight
optimization, or production promotion.
