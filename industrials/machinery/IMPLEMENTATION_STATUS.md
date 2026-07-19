# Machinery Implementation Status

Status date: 2026-07-18

This document is the authoritative implementation sequence and acceptance
checklist for the machinery model family. `README.md` remains the operating
quick reference. A stage is complete only when its acceptance gate passes on
production data; a dry run proves orchestration only.

## Current State

The universe, identity contracts, family-scoped loaders, enhanced filing-level
financial extraction, metric-availability contract, shadow scoring, dashboard
contract, industrial portfolio adapter, and bootstrap orchestration are
implemented. The current-date production pipeline has passed through the
portfolio-layer handoff. A real 2019-01-02 historical publication smoke also
passed. The complete 2019-to-current panel, OOS calibration, backtests, and
model promotion remain pending.

The production universe contains 114 active members, 136 point-in-time
membership rows, and 50 delisted candidates. All 136 included active and
inactive members have SEC filings, raw facts, and mapped facts. Core statement
coverage is complete for assets, equity, and operating cash flow. Revenue is
present for every operating issuer; the four reviewed development-stage
issuers have validated zero-revenue treatment instead of a missing-data fill.

`INIO` and `MAIR` now use strict historical registration-statement extraction.
Both have complete current financial feature rows, valid TTM revenue and
orders, debt/leverage, and interest coverage. They remain under the
`SEC_ARCHIVE_TEXT_TABLE` review policy until text-table PIT history QC passes;
that is an eligibility gate, not an ingestion gap.

The SEC archive loader now parses standard and extension Inline XBRL footnote
facts, label linkbases, RPO timing axes, narrative current-RPO percentages, and
machinery-only filing-table disclosures for RPO, reported backlog, funded
backlog, and orders. Registration-statement and bounded 8-K supplemental forms
are processed without allowing 8-K volume to displace periodic filings. RPO
remains distinct from backlog and orders. RPO-implied orders and book-to-bill
are explicitly labeled low-confidence proxies. Every one of the 25 required
financial metrics now has an exact PIT status: `REPORTED`, `PROXY`, `EXEMPT`,
`NOT_APPLICABLE`, `NOT_DISCLOSED`, or `PARSER_FAILURE`.

All live dashboard rows remain non-investable until Stage 8 calibration is
sealed and Stage 12 promotion changes the OOS contract flags.

## Stage Checklist

| Stage | Scope | Implementation | Production data | Acceptance gate |
| --- | --- | --- | --- | --- |
| 0 | Seed, source registry, schema | Complete | Passed | Seed validator passes for 114 active and 50 delisted candidates; Ruff and Pyright pass. |
| 1 | Active universe, aliases, listing dates, historical membership | Complete | Passed current date | Exactly 114 active members; PIT intervals do not overlap incorrectly; defense rows unchanged. |
| 2 | Identity and Norgate reconciliation | Complete | Passed current date | Every included historical ticker has separate `actual_ticker` and `norgate_symbol`; ambiguous or reused symbols fail closed. |
| 3 | Adjusted prices and market features | Complete | Passed current date; history pending | Active coverage and each eligible post-2019 historical member have PIT prices or an explicit exclusion; market-stage validator passes. |
| 4 | SEC fundamentals, FX, footnotes, and financial features | Complete | Passed 2026-07-09; optional disclosures remain source-limited | Exactly one status for every required ticker/metric pair; generic and 25-metric audits pass; no future filing or period leakage; units and currencies reconcile. |
| 5 | SEC ownership, 13F, FINRA, IBKR, positioning | Complete | Passed current date | All three databases refresh in order; family-scoped import passes; missing feeds remain explicit. |
| 6 | Scoring feature contract and eligibility | Shadow implementation complete | Passed current date | One row per PIT member; zero is distinct from missing; required portfolio and calibration fields pass validation. |
| 7 | Shadow scores and ranks | Complete | Passed current date | Scores are deterministic, ranks contiguous, missing metrics reduce confidence, and all investment/OOS gates remain closed. |
| 8 | Signal diagnostics, constrained calibration, walk-forward OOS | Pending | Pending | Frozen train/validation/test windows pass IC, turnover, stability, coverage, and leakage gates. |
| 9 | Portfolio backtest and capacity analysis | Pending | Pending | Net-of-cost results pass return, drawdown, turnover, concentration, and cohort stability gates. |
| 10 | Dashboard and portfolio-layer handoff | Shadow contract complete | Current and 2019-01-02 smoke passed | Rank file and calibration sidecar pass the industrial-family adapter with all financial values, provenance, USD fields, and availability statuses required for calibration. |
| 11 | PIT history from 2019-01-02 | Backfill and per-date adapter validation complete | One-date production smoke passed; full panel pending | Every scheduled date publishes an immutable survivorship-corrected file and manifest; each file passes the portfolio adapter; failures are zero. |
| 12 | Governance lock and production promotion | Pending | Pending | Model artifacts are hashed and frozen; OOS flags are valid; portfolio cap changes require explicit approval. |

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
`financial_metric_availability.csv`. For each PIT member and each of the 25
required metrics, the record retains the value/status, unit, accession, filing
and period dates, taxonomy/concept, extraction method, confidence, reason, and
operand provenance. The dated final rank and Stage 11 calibration files also
carry all 25 status columns, the exact availability as-of date, source amounts
in USD, and filing/reporting quality fields. Publication fails if any status is
blank/invalid, any classification is stale, or the classified fraction is not
1.0.

Historical feature rebuilds write coverage reports under
`historical_backfill/stage_reports/<asof>/` and suppress latest-state quality
issue mutations. This prevents a historical run from overwriting current
coverage files or contaminating current acceptance gates.

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

The enhanced current-date extraction, classification, feature rebuild, and
end-to-end portfolio smoke are complete. The remaining sequence is:

1. Complete archive-text PIT history QC for `INIO` and `MAIR`, preserving the
   strict routing override until equivalent tagged periodic history exists.
2. Run the immutable daily PIT backfill from 2019-01-02 through the current
   evaluation date. Every date must pass its integrated portfolio adapter gate.
3. Audit the completed historical coverage/status panel and quarantine dates or
   issuers with parser failures, stale classifications, or non-PIT provenance.
4. Run diagnostics, constrained calibration, walk-forward OOS validation, and
   portfolio backtests.
5. Freeze artifacts and promote only after all gates pass.

## Current Evidence

- Production SEC archive bootstrap: 136 included active/historical issuers,
  132 successful and four expected development-stage reviews, with zero fetch
  failures. Retry reconciliation closed the stale transient SEC issues.
- Production Stage 4 validation passed as of `2026-07-09`: 114 feature rows,
  114 x 25 = 2,850 classified ticker/metric rows, and zero parser failures.
- Availability distribution: 1,033 `REPORTED`, 74 `PROXY`, 25 `EXEMPT`, 67
  `NOT_APPLICABLE`, and 1,651 `NOT_DISCLOSED`.
- All 25 metrics are implemented and audited; 17 currently meet their coverage
  gates. Total RPO coverage is 55/114, current RPO 37/114, RPO YoY 46/114,
  RPO/revenue 40/114, and both RPO-implied proxy metrics 37/114.
- Strict disclosed orders cover 10/114 at the July 9 PIT date. Reported backlog
  covers 46/114, reported-backlog YoY 33/114, and reported-backlog/revenue
  15/114. Strict funded backlog remains 0/114 because no filing facts pass the
  funded-definition contract; it is never filled from RPO or generic backlog.
- Matched-period CCC change now covers 88/114. ROIC covers 56/114 and net
  debt/EBITDA 82/114; both remain below their development-stage cohort gates,
  while undefined economics are carried by explicit flags rather than numeric
  substitutions.
- The current final rank contains 114 rows, 100 rank-ready rows, and all 73
  required calibration financial/provenance fields. The industrial-family
  portfolio adapter passed with zero investable/OOS-valid shadow rows.
- The real `2019-01-02` smoke published 110 survivorship-correct rows and the
  portfolio adapter consumed all 110. Zero were rank-ready because no financial
  facts were public on that first date, which is the required fail-closed result.
- Defense isolation passed against the pre-change database snapshot: all eight
  defense-family tables matched exactly; 8,597 filing rows, 1,623,514 raw facts,
  and 270,954 mapped facts were unchanged; cross-family ticker overlap is zero.
- Full industrial regression suite: 39 passed. Ruff passed. Pyright: 0 errors
  and 0 warnings.
- Full daily history, calibration, backtesting, and production promotion remain
  pending.
