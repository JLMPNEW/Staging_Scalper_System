# Machinery Implementation Status

Status date: 2026-07-09

This document is the authoritative implementation sequence and acceptance
checklist for the machinery model family. `README.md` remains the operating
quick reference. A stage is complete only when its acceptance gate passes on
production data; a dry run proves orchestration only.

## Current State

The universe, identity contracts, family-scoped loaders, shadow scoring,
dashboard contract, industrial portfolio adapter, and 25-step bootstrap
orchestration are implemented. The bootstrap has passed in dry-run mode only.
Production data, historical panels, OOS calibration, backtests, and model
promotion remain pending.

All live dashboard rows remain non-investable until Stage 8 calibration is
sealed and Stage 12 promotion changes the OOS contract flags.

## Stage Checklist

| Stage | Scope | Implementation | Production data | Acceptance gate |
| --- | --- | --- | --- | --- |
| 0 | Seed, source registry, schema | Complete | Pending load | Seed validator passes for 114 active and 50 delisted candidates; Ruff and Pyright pass. |
| 1 | Active universe, aliases, listing dates, historical membership | Complete | Pending load | Exactly 114 active members; PIT intervals do not overlap incorrectly; defense rows unchanged. |
| 2 | Identity and Norgate reconciliation | Complete | Pending import | Every included historical ticker has separate `actual_ticker` and `norgate_symbol`; ambiguous or reused symbols fail closed. |
| 3 | Adjusted prices and market features | Complete | Pending bootstrap | Active coverage and each eligible post-2019 historical member have PIT prices or an explicit exclusion; market-stage validator passes. |
| 4 | SEC fundamentals, FX, and financial features | Special metric schema, extraction, derivation, scoring, and coverage audit implemented | Archive bootstrap in progress | Generic and machinery-specific coverage reports pass; no future filing or period leakage; units and currencies reconcile. |
| 5 | SEC ownership, 13F, FINRA, IBKR, positioning | Complete | Pending bootstrap | All three databases refresh in order; family-scoped import passes; missing feeds remain explicit. |
| 6 | Scoring feature contract and eligibility | Shadow implementation complete | Pending real smoke | One row per PIT member; zero is distinct from missing; required portfolio and calibration fields pass validation. |
| 7 | Shadow scores and ranks | Complete | Pending real smoke | Scores are deterministic, ranks contiguous, missing metrics reduce confidence, and all investment/OOS gates remain closed. |
| 8 | Signal diagnostics, constrained calibration, walk-forward OOS | Pending | Pending | Frozen train/validation/test windows pass IC, turnover, stability, coverage, and leakage gates. |
| 9 | Portfolio backtest and capacity analysis | Pending | Pending | Net-of-cost results pass return, drawdown, turnover, concentration, and cohort stability gates. |
| 10 | Dashboard and portfolio-layer handoff | Shadow contract complete | Pending full real-data smoke | Rank file and calibration sidecar pass every portfolio-layer stage with no missing contract fields. |
| 11 | PIT history from 2019-01-02 | Backfill script complete | Pending | Every scheduled date publishes an immutable survivorship-corrected file and manifest; failures are zero. |
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

## Correct Execution Sequence

1. Run the full production bootstrap through 2026-07-09, including Norgate,
   SEC ownership, FINRA, 13F, IBKR, and generic financial facts.
2. Audit raw XBRL concepts and filing-table labels for orders, backlog,
   depreciation, EBITDA, and interest expense across all five cohorts.
3. Implement reviewed concept mappings and derived metrics with provenance and
   PIT-safe period alignment.
4. Rebuild Stage 4 financial features and pass machinery metric coverage gates.
5. Rebuild and validate Stage 6 scoring features; confirm explicit missingness
   and confidence behavior.
6. Produce immutable PIT historical files from 2019-01-02 and validate their
   portfolio-layer calibration fields.
7. Run diagnostics, constrained calibration, walk-forward OOS validation, and
   portfolio backtests.
8. Run the full real-data portfolio-layer smoke test across every stage.
9. Freeze artifacts and promote only after all gates pass.

## Current Evidence

- Focused machinery tests: 5 passed.
- Ruff: passed.
- Pyright: 0 errors.
- Bootstrap dry run: 25 of 25 planned steps, no failures before the special-metric audit stage was added.
- Production market, generic financial, ownership, FINRA, 13F, IBKR, positioning, scoring, dashboard, and portfolio handoff: passed through 2026-07-09.
- Resumable SEC archive bootstrap for special financial disclosures: in progress.
