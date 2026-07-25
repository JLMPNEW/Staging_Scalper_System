# Machinery Implementation Status

Status date: 2026-07-24

This document is the authoritative implementation sequence and acceptance
checklist for the machinery model family. `README.md` remains the operating
quick reference. A stage is complete only when its acceptance gate passes on
production data; a dry run proves orchestration only.

## Current State

The universe, identity contracts, family-scoped loaders, enhanced filing-level
financial extraction, metric-availability contract, shadow scoring, dashboard
contract, industrial portfolio adapter, and bootstrap orchestration are
implemented. The `2026-07-20` production financial, scoring, publishing, and
portfolio-layer chain passed after the parser-version-8 disclosure recovery.
The complete daily 2019-01-02 through 2026-07-20 survivorship-corrected panel
was regenerated from those facts, and the combined active/delisted coverage
report passed across all 1,896 dates. OOS calibration, backtests, and model
promotion remain pending.

The historical panel contains 1,896 validated trading dates and 214,526
ticker-date observations. All date files pass the machinery contract and the
industrial-family portfolio adapter. The 50-name delisted seed contains 25
pre-2019 out-of-scope names and 25 in-scope candidates. Twenty-two in-scope
tickers have resolved point-in-time membership, price, SEC, canonical, and
profile history. `ELMS`, `GOEV`, and `RIDE` remain explicit fail-closed
identity exceptions because a valid Norgate issuer mapping is unavailable.

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
backlog, and orders. Registration statements and all 8-K supplemental forms
since the configured 2019 boundary are processed without allowing event volume
to displace periodic filings. Every eligible 8-K exhibit is scanned before
deduplication, and SEC/issuer PDFs have text extraction plus an optional OCR
fallback. RPO
remains distinct from backlog and orders. RPO-implied orders and book-to-bill
are explicitly labeled low-confidence proxies. Every one of the 28 required
financial metrics now has an exact PIT status: `REPORTED`, `PROXY`, `EXEMPT`,
`NOT_APPLICABLE`, `NOT_DISCLOSED`, `DISCLOSED_UNPARSED`, or `PARSER_FAILURE`.

The filing parser now has a separate candidate lane for explicit narrative
orders, funded backlog, reported backlog, and RPO. Candidates retain evidence,
scope, confidence, accession, accepted timestamp, document, and period. Only
unambiguous consolidated values are promoted into SEC raw/mapped facts;
segment-only or conflicting values remain review rows. Cache backfills commit
per ticker. The normal daily orchestrator runs a bounded 40-ticker, 12-filing
cache pass. Full-history recovery is explicit, local, restartable, includes
inactive members, and keys completion by parser version and scan bounds.

A separate machinery issuer-IR lane accepts only manifest-reviewed HTTPS
releases and presentations from approved domains. It preserves publication and
retrieval timestamps, source/final URLs, content hashes, cached raw documents,
extraction method, and review provenance. It enters canonical processing below
SEC priority and only when consolidated scope is explicit or reviewed.
Transcripts remain `REVIEW_REQUIRED` evidence and cannot create scored facts.

The July 19 exhaustive audit hardened the production contracts. Reporting
profiles are immutable filing/event-date snapshots and cannot inspect
later-accepted SEC facts. Historical feature builds carry the latest snapshot
whose date is on or before the requested as-of date; they never require an
exact-date filing and never look ahead. Historical rebuilds create missing
snapshots locally from stored facts with `--profiles-only`. Missing debt and issuance facts remain null;
absence is never converted to zero. Ranking now enforces the approved financial
eligibility policy, configured source precedence, current-asof staleness, and
availability-count integrity. Dashboard validation independently verifies the
rank table, survivorship sidecar, manifest metadata, row identities, hashes,
and contract versions.

All live dashboard rows remain non-investable until Stage 8 calibration is
sealed and Stage 12 promotion changes the OOS contract flags.

## Stage Checklist

| Stage | Scope | Implementation | Production data | Acceptance gate |
| --- | --- | --- | --- | --- |
| 0 | Seed, source registry, schema | Complete | Passed | Seed validator passes for 114 active and 50 delisted candidates; Ruff and Pyright pass. |
| 1 | Active universe, aliases, listing dates, historical membership | Complete | Passed current date | Exactly 114 active members; PIT intervals do not overlap incorrectly; defense rows unchanged. |
| 2 | Identity and Norgate reconciliation | Complete | Passed current date | Every included historical ticker has separate `actual_ticker` and `norgate_symbol`; ambiguous or reused symbols fail closed. |
| 3 | Adjusted prices and market features | Complete | Passed current date and full daily history | Active coverage and each eligible post-2019 historical member have PIT prices or an explicit exclusion; market-stage validator passes. |
| 4 | SEC fundamentals, FX, footnotes, and financial features | Complete | Passed 2026-07-20; optional disclosures remain source-limited | Exactly one status for every required ticker/metric pair; generic and 28-metric audits pass; the recoverability ledger classifies every missing applicable cell; no future filing or period leakage; units and currencies reconcile. |
| 5 | SEC ownership, 13F, FINRA, IBKR, positioning | Complete | Passed current date | All three databases refresh in order; family-scoped import passes; missing feeds remain explicit. |
| 6 | Scoring feature contract and eligibility | Shadow implementation complete | Passed current date | One row per PIT member; zero is distinct from missing; required portfolio and calibration fields pass validation. |
| 7 | Shadow scores and ranks | Complete | Passed current date | Scores are deterministic, ranks contiguous, missing metrics reduce confidence, and all investment/OOS gates remain closed. |
| 8 | Signal diagnostics, constrained calibration, walk-forward OOS | Pending | Pending | Frozen train/validation/test windows pass IC, turnover, stability, coverage, and leakage gates. |
| 9 | Portfolio backtest and capacity analysis | Pending | Pending | Net-of-cost results pass return, drawdown, turnover, concentration, and cohort stability gates. |
| 10 | Dashboard and portfolio-layer handoff | Shadow contract complete | Current 2026-07-20 and all historical dates passed | Rank file and calibration sidecar pass the industrial-family adapter with all financial values, provenance, USD fields, and availability statuses required for calibration. |
| 11 | PIT history from 2019-01-02 | Complete | Parser-v8 rebuild passed 1,896/1,896 dates | Every scheduled date publishes an immutable survivorship-corrected file and manifest; each file passes the portfolio adapter; combined active/delisted coverage passes. |
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

The enhanced extraction, disclosure classification, reviewed issuer
resolution, current-date feature rebuild, portfolio smoke, full cached filing
recovery, and parser-v8 historical regeneration are complete. The remaining
sequence is:

1. Resolve the remaining reviewed disclosure candidates, starting with the
   three high-recoverability `PLUG` cells, without weakening consolidated-scope
   or period-alignment policy.
2. Complete archive-text PIT history QC for `INIO` and `MAIR`, preserving the
   strict routing override until equivalent tagged periodic history exists.
3. Populate the reviewed issuer-IR manifest for cells classified as having no
   qualifying SEC disclosure, then run the issuer-IR stage. Do not weaken
   source, scope, period, or applicability safeguards to inflate coverage.
4. Run diagnostics, constrained calibration, walk-forward OOS validation, and
   portfolio backtests.
5. Freeze artifacts and promote only after all gates pass.

## Current Evidence

- The targeted SEC production bootstrap processed all 136 active and resolved
  inactive members with zero ticker failures. FilingSummary routing limits
  periodic filings to machinery-relevant footnotes while retaining primary
  filings, XBRL instances, and relevant event exhibits. PDF parsing is bounded
  by payload size and a killable per-document process deadline.
- The parser-v8 full cache recovery completed all 136 tickers, 33,271 relevant
  filing documents, and 2,128 candidates. Candidate reconciliation currently
  contains 813 `ACCEPTED`, 104 `CONSUMED_BY_AGGREGATE`, 31
  `CONSUMED_BY_CONSOLIDATED_TOTAL`, 846 `REJECTED_POLICY`, 91 duplicate-
  provenance suppressions, six semantic suppressions, 188 structured-
  duplicate suppressions, and 51 `REVIEW_REQUIRED` candidates.
- Production Stage 4 passed as of `2026-07-20`: 114 feature rows and
  114 x 28 = 3,192 classified ticker/metric rows. Availability is 1,123
  `REPORTED`, 254 `PROXY`, 25 `EXEMPT`, 774 `NOT_APPLICABLE`, 1,013
  `NOT_DISCLOSED`, three `DISCLOSED_UNPARSED`, and zero `PARSER_FAILURE`.
- All 28 metrics are implemented and audited. Twenty of 22 calibration metrics
  meet their configured coverage gates. `book_to_bill` and `roic` remain
  pending. Three of six limited-universe diagnostics are ready.
- Reported orders cover 16/95 applicable issuers, orders YoY 14/95,
  book-to-bill 8/90, reported backlog 38/96, reported-backlog YoY 33/96, and
  reported-backlog/revenue 17/91. Strict funded backlog has no currently
  approved applicable issuer and is `NOT_APPLICABLE` rather than filled from
  RPO, firm backlog, or generic backlog.
- Total RPO covers 49/91 applicable issuers, current RPO 35/91, RPO YoY 45/91,
  RPO/revenue 43/86, and both RPO-implied proxy metrics 40/86. The canonical
  contract-load diagnostics cover 67/96, 60/96 YoY, and 42/91 relative to
  revenue. Proxy metrics remain labeled and have no scoring weight.
- Asset turnover and inventory/sales spread now cover 114/114 and 107/107
  applicable issuers. Diluted-share growth covers 111/111. CCC change covers
  88/109, net debt/EBITDA 73/93, interest coverage 73/113, capital-raise
  dependence 53/76, and ROIC 78/113.
- The recovery ledger classifies all 1,016 missing applicable cells: 818 medium,
  195 low, and three high recoverability cells, all for `PLUG`. Of these,
  672 have no qualifying SEC disclosure and route to issuer-IR research; 175
  lack a standard-XBRL derivation operand; the remainder are explicit history,
  period-alignment, registration-statement, or policy-rejection cases.
- The downstream current-date rebuild passed all 12 selected stages through
  scoring, publishing, dashboard validation, and the industrial-family
  portfolio adapter (`machinery_refresh_20260724T020213Z`).
- The machinery recovery did not edit defense implementation files. The
  pre-recovery database comparison found no destructive defense mutations;
  newer defense rows were append-only shared-source/independent refresh results
  timestamped before the machinery cache recovery. The nested positioning
  import now passes `--model-family` explicitly, with a regression test, so
  family selection cannot depend on a config default.
- The parser-v8 historical panel passed all 1,896 dates from 2019-01-02 through
  2026-07-20 and contains 214,526 ticker-date observations. Combined coverage
  includes 114 active and 22 resolved delisted tickers. `ELMS`, `GOEV`, and
  `RIDE` remain explicit Norgate identity exclusions, never substituted with
  unrelated securities.
- Combined historical coverage is 96.46% for asset turnover, 93.60% for
  inventory/sales growth spread, 92.36% for diluted-share growth, 42.90% for
  RPO, 32.05% for reported backlog, 25.02% for current RPO, and 10.81% for
  orders. Funded backlog has a zero applicable denominator by reviewed policy.
- Final verification: SQLite `PRAGMA quick_check` returned `ok`; 119 industrial
  tests passed; Ruff passed; pyright reported zero errors and zero warnings.

## Independent Parser Pilot - 2026-07-24

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
