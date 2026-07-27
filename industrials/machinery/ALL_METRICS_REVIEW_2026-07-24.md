# Machinery All-Metrics Review

As-of date: 2026-07-24

This review covers every one of the 28 machinery financial metrics for all 113
active point-in-time members. It also verifies the historical panel and the
resolved inactive/delisted population. Coverage means `REPORTED` or `PROXY`
divided by applicable observations; structural `EXEMPT` and `NOT_APPLICABLE`
observations are classified but excluded from that denominator.

## Acceptance Summary

- Metric contract: PASS. All 3,164 expected ticker/metric observations are
  classified; there are no blank statuses and no `PARSER_FAILURE` rows.
- Availability: 1,146 `REPORTED`, 284 `PROXY`, 25 `EXEMPT`, 767
  `NOT_APPLICABLE`, 939 `NOT_DISCLOSED`, and three
  `DISCLOSED_UNPARSED`.
- Applicable coverage: 1,430/2,372, or 60.29%.
- Calibration gates: 22/22 ready. `book_to_bill` passes exactly at its
  minimum-count gate of 10/89 and `roic` passes at 79/112, including 5/19 in
  the development-stage cohort.
- Limited-universe diagnostics: 3/6 ready. The strict funded-backlog family
  has no approved applicable issuers and remains pending rather than using
  RPO or generic backlog as a substitute.
- Recoverability: all 942 missing applicable observations are classified.
  Three are high recoverability, 794 medium, and 145 low. There are no
  unclassified failures.
- Current portfolio contract: PASS for 113 rows; 99 are rank-ready and
  research-eligible, and the live industrial-family adapter exposes the
  validated 20-name production sleeve.

## Complete Metric Coverage

| Metric | Covered/applicable | Coverage | Gate | Status |
| --- | ---: | ---: | --- | --- |
| Orders | 17/94 | 18.09% | Calibration | Ready |
| Funded backlog | 0/0 | Structural N/A | Limited universe | Pending |
| Reported backlog | 38/95 | 40.00% | Calibration | Ready |
| Remaining performance obligation | 59/91 | 64.84% | Calibration | Ready |
| Current RPO | 37/90 | 41.11% | Calibration | Ready |
| Orders YoY growth | 14/94 | 14.89% | Calibration | Ready |
| Book-to-bill | 10/89 | 11.24% | Calibration | Ready |
| Funded-backlog YoY growth | 0/0 | Structural N/A | Limited universe | Pending |
| Funded-backlog/revenue | 0/0 | Structural N/A | Limited universe | Pending |
| Reported-backlog YoY growth | 34/95 | 35.79% | Calibration | Ready |
| Reported-backlog/revenue | 17/90 | 18.89% | Calibration | Ready |
| RPO YoY growth | 51/91 | 56.04% | Calibration | Ready |
| RPO/revenue | 51/85 | 60.00% | Calibration | Ready |
| RPO-implied orders | 46/85 | 54.12% | Calibration | Ready |
| RPO-implied book-to-bill | 46/85 | 54.12% | Calibration | Ready |
| Contract-load proxy | 75/96 | 78.13% | Limited universe | Ready |
| Contract-load proxy YoY growth | 65/96 | 67.71% | Limited universe | Ready |
| Contract-load proxy/revenue | 47/90 | 52.22% | Limited universe | Ready |
| ROIC | 79/112 | 70.54% | Calibration | Ready |
| Asset turnover | 113/113 | 100.00% | Calibration | Ready |
| Incremental operating margin | 72/108 | 66.67% | Calibration | Ready |
| Inventory/sales growth spread | 106/106 | 100.00% | Calibration | Ready |
| Cash-conversion-cycle change | 87/108 | 80.56% | Calibration | Ready |
| Net debt/EBITDA | 72/92 | 78.26% | Calibration | Ready |
| Interest coverage | 72/111 | 64.86% | Calibration | Ready |
| Cash runway years | 22/33 | 66.67% | Calibration | Ready |
| Capital-raise dependence | 90/113 | 79.65% | Calibration | Ready |
| Diluted-shares YoY growth | 110/110 | 100.00% | Calibration | Ready |

`Ready` means the configured coverage gate passed. It does not mean every
issuer reports the metric or that the signal has passed Stage 8 predictive
calibration.

## Missingness Review

Seven tickers have no missing applicable metric: `AIT`, `DCI`, `ENOV`, `FLS`,
`FSS`, `OUST`, and `WAB`. The largest current missing counts are:

| Ticker | Missing applicable | Applicable |
| --- | ---: | ---: |
| XE | 20 | 22 |
| LASE | 19 | 24 |
| SMR | 18 | 23 |
| GGG | 17 | 24 |
| WTS | 17 | 24 |
| SWK | 16 | 24 |
| BWEN | 15 | 25 |
| PNR | 15 | 24 |
| DDD | 15 | 24 |
| FELE | 15 | 24 |

The exhaustive 113-ticker detail is in
`output/industrials/machinery/stage4/machinery_financial_ticker_coverage.csv`.
The 113 x 28 value/status/provenance matrix is in
`output/industrials/machinery/stage4/machinery_financial_metric_observations.csv`.

The recovery ledger assigns the 942 missing applicable observations to:

| Recovery class | Count |
| --- | ---: |
| No qualifying SEC disclosure | 589 |
| Missing derivation operand | 172 |
| Derivation period/alignment gap | 74 |
| Current-RPO text disaggregation needed | 53 |
| Insufficient comparable history | 37 |
| Registration-statement recovery | 11 |
| Disclosed value requiring review | 3 |
| Disclosure rejected by policy | 3 |

The three high-recoverability observations are `GRC` reported backlog and its
two derived metrics. A historical disclosure exists, but no current comparable
value has yet passed projection and period-alignment policy.

## Dedicated Parser Result

Dedicated-parser release `0.4.6` full-universe run 36 processed the complete
4,403-accession active scope with zero cache gaps and zero failures. That run
was not promoted wholesale: validation detected three false MWA orders
observations from an ASC 606 revenue-contract narrative. Adapter `v3.6`
rejects that pattern deterministically.

Bounded runs 38, 39, and 41 then validated reviewed FLS, DOV, MAIR, OUST, WAB,
and MWA evidence. Promotions 9, 10, and 12 reported zero conflicts and
published only facts that passed the consolidated-scope, period, currency, and
0.90 confidence gates. The current production builder consumes the shared
`dedicated_parser_production` source; no metric is promoted merely to increase
coverage.

## Historical And Delisted Coverage

The global historical coverage index passes for all 1,900 trading dates from
2019-01-02 through 2026-07-24 and contains 214,976 ticker-date observations.
The seed contains 51 delisted candidates: 25 are pre-2019 and out of scope, 23
have resolved in-scope point-in-time memberships, and `ELMS`, `GOEV`, and
`RIDE` remain fail-closed because issuer/Norgate identity cannot be validated.

`GTLS` was removed from the active universe after the 2026-07-16 Chart
Industries acquisition close, retained as a historical member through that
date, and mapped to successor `BKR`. Only the four affected post-close
partitions were rebuilt; 1,894 earlier sidecars were reconciled without
recomputing their financial features.

## Remaining Gates

The fingerprinted bounded materialization is complete: 689/689 affected
partitions passed, 1,211 unaffected partitions were preserved, combined
coverage passed all 1,900 dates, and all 1,900 dated files passed the
industrial-family portfolio adapter.

Stages 8 and 9 passed on 2026-07-25. `book_to_bill` remained diagnostic-only,
as required by the historical-depth preflight. The constrained candidate and
validation-selected `long_only_q20_equal` portfolio passed untouched holdout,
D+1 adjusted-open, net-of-cost, concentration, turnover, and capacity gates.

Stage 12 is active as of 2026-07-24 with 113 production rows, 99 broad
OOS-valid names, and exact 20 selected/20 portfolio-adapter-investable
reconciliation. The selected names implement the validated
`long_only_q20_equal` policy, with exact parity across all 26 Stage 9
validation and holdout periods. The bounded portfolio smoke passed exact
Stage 1 and optimizer membership, equal weights, the 5% cap, all required
downstream groups, and the final-book manifest. The separate
survivorship-corrected sidecar remains the immutable shadow calibration source.

## Reproducible Artifacts

- `output/industrials/machinery/stage4/machinery_financial_metric_coverage.json`
- `output/industrials/machinery/stage4/machinery_financial_metric_coverage.csv`
- `output/industrials/machinery/stage4/machinery_financial_metric_observations.csv`
- `output/industrials/machinery/stage4/machinery_financial_ticker_coverage.csv`
- `output/industrials/machinery/dedicated_parser/2026-07-24/`
- `output/industrials/machinery/historical_backfill/machinery_combined_historical_coverage.json`
- `output/industrials/machinery/historical_backfill/preflight/machinery_historical_preflight_summary.json`
- `output/industrials/machinery/historical_backfill/preflight/machinery_historical_preflight_metric_depth.csv`
- `output/industrials/machinery/historical_backfill/preflight/machinery_historical_preflight_affected_partitions.csv`
- `output/industrials/machinery/historical_backfill/machinery_historical_promotion_materialization.json`
- `output/industrials/machinery/historical_backfill/machinery_historical_promotion_materialization.csv`
- `output/industrials/machinery/dashboard/2026-07-24/`
- `output/industrials/machinery/stage8/`
- `output/industrials/machinery/stage9/`
- `output/industrials/machinery/stage12/`
