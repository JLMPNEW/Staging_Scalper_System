# Basic Materials stage gates

## Stage 0 - independence contract

Pass requires matching model-family and sector constants, `shadow_monitor`
promotion state, false portfolio and out-of-sample validity flags,
output/cache paths under `output/basic_materials`, a database named
`basic_materials.sqlite`, and no imports from another sector package.

## Stage 1 - database and source contract

Pass requires an empty or correctly identified Basic Materials database,
matching append-only migration checksums through schema v7, the package-owned
18-source registry, and byte-for-byte authoritative manifests before mutation.
An unidentified non-empty database is rejected. An older owned database may
advance only through every missing migration in ascending order; a migration
name or checksum mismatch fails before mutation.

## Stage 2 - current-universe contract

Pass requires exactly 134 unique active tickers, all required fields, valid
ten-digit CIKs, exact cohort counts, exact cohort-to-parent mappings, and
`calibration_group = subsector`. Every row remains visible. Current memberships
are `current_source_only=1`, `survivorship_corrected=0`, and
`calibration_eligible=0`.

## Stage 2B - historical candidate intake

Candidate-intake pass requires exactly 72 unique deactivated-security
candidates across all eight cohorts, policy-fixed cohort counts, a matching
SHA-256 manifest, valid provider symbols and asset IDs for every unblocked row,
and at least 16 initial event-source URLs. Every row remains
`candidate_unapproved`, `include_in_historical_universe=0`, and
`calibration_eligible=0`. NSR remains explicitly provider-mapping blocked.

Candidate-intake pass does not promote history. Promotion requires
effective-dated membership, ticker/security lineage, a primary-source terminal
event, adjusted-price continuity, and explicit terminal economics.

## Stage 2B - governed historical reconciliation pilot

Pilot pass requires exactly 20 effective-dated historical memberships across
all eight cohorts, four reviewed aliases, 22 security events, and 20 matching
terminal-event rows. Every promoted membership must reconcile to the immutable
candidate census on ticker, cohort, provider identity, company, industry, and
quoted interval. All four CSV fingerprints and schemas must match the
historical manifest.

The load must preserve all 134 current memberships, store all four raw
payloads, resolve canonical securities without raw-ticker ambiguity, pass
foreign-key checks, rerun idempotently, and roll back on failure. Historical
memberships remain `calibration_eligible=0`. A resolved terminal flag is valid
only when it matches the latest evidence-backed terminal-return calculation;
resolution never activates calibration.

## Stage 3 - adjusted market data and terminal returns

Contract pass requires exactly 162 roles over 158 stable Norgate assets: 134
current, 20 historical, XLB, SPY, and six event-specific stock-successor roles.
Governed hashes, row counts, keys, role counts, asset IDs, and terminal-event
keys must match the Stage 3 manifest. Ticker-only historical joins are
prohibited.

Provider-load pass requires an unchanged Norgate fingerprint from extraction
through publication; contracted symbol-to-asset-ID matches; current major-
exchange status; identical raw/total-return date sets; unique in-window dates;
positive prices; valid OHLC, volume, and dividends; canonical cache hashes; and
atomic publication.

Coverage pass requires:

- complete XLB and SPY benchmark/calendar history;
- fresh, valid current histories bounded by each listing regime;
- at least 95% current-plus-benchmark rank-ready coverage;
- sparse histories accepted only under the governed observation, missing-
  session, and maximum-gap limits; and
- recent listings labeled separately from full-history rows.

Terminal pass requires all 20 events to have an explicit calculation state.
Cash, stock, and mixed consideration use reviewed terms and on-or-before-as-of
quotes. Bankruptcy/liquidation rows without verified old-equity distributions
remain null and unresolved; zero is never inferred.

Feature pass requires one row per current security, adjusted-return inputs,
raw close/volume for liquidity, governed benchmarks, on-or-before-as-of data,
and explicit quality. The clean 2026-09-05 Stage 4C acceptance build passes at
136/136 gate roles with 492,653 bars, 5,388 actions, 4,446 sessions, 134 feature
rows, 16 resolved terminal events, and four pending distributions. Calibration
and portfolio flags remain false.

## Stage 4A - financial contract and reporting-profile census

Contract pass requires immutable policy, manifest, concept map, and a reviewed
census covering exactly 134 current and 20 historical-pilot issuers. The
contract fixes source precedence, filing families, acceptance-time
availability, amendments, reporting cadence, currencies, 22 canonical metrics,
future common features, and closed promotion flags. Listing currency may never
fill reporting currency.

Database pass requires exactly 154 profiles, 22 metrics, and 66 taxonomy links.
Every input and row hash must validate before mutation; loading is atomic and
idempotent; foreign keys pass; and the contract load cannot activate facts,
features, calibration, scoring, or portfolio authority.

## Stage 4B - acceptance-bounded SEC facts, FX, and current common features

Contract pass requires a policy bound to every Stage 4A artifact and immutable
reporting cache. It fixes cutoff, history boundary, source precedence, pilot,
14 exception routes, normalization/TTM rules, FX methods, common features, and
closed promotion flags.

Ingestion must preserve accession, form, exact acceptance timestamp, fiscal
period, period bounds, taxonomy, unit, currency, amendment state, payload hash,
URL, and source lineage. Facts with missing/excess/future timing, wrong period,
dimensions, or invalid currency remain quarantined. Filing date and period end
never substitute for acceptance time.

Structured fallback must produce usable mapped facts from the exact accession
and document. Metadata-only exhibits cannot be manufactured into structured
facts. Normalization requires deterministic precedence, equal-rank conflict
quarantine, amendment supersession, correct signs/units, cadence-aware TTM, and
non-summed weighted-average shares. FX requires effective-dated AUD, CAD, EUR,
KRW, and USD lineage with no required conversion missing.

Current-feature pass requires one feature and coverage row for all 134 current
securities using only facts and prices available by the cutoff. This is a
single-cutoff feature view, not a historical feature panel.

## Stage 4C - OGC audited source and listed-security-unit remediation

Contract pass requires financial policy v2 to bind the exact 47-row security-
ratio CSV and its policy hash. The ratio loader must verify every registered
security title against the exact SEC document. ADS rows must additionally
verify the stated conversion phrase. A ratio is positive, effective-dated,
accepted by the cutoff, immutable, and never inferred. The current contract
does not authorize historical backfill.

The exact ADS conversions, in issuer shares per traded ADS, are:

| Ticker | Ratio |
|---|---:|
| BHP | 2 |
| ELVR | 10 |
| PKX | 0.25 |
| RIO | 1 |
| TX | 10 |

Valuation pass requires the formula
`USD price * diluted issuer shares / issuer shares per traded security`, exact
feature-to-ratio lineage, zero unresolved ratio-required current foreign
listings, and no non-USD price used without conversion. RMIX is an explicit
domestic-interim route and is not a foreign-ratio exception.

OGC pass requires the exact May 1, 2026 SEC-hosted audited IFRS exhibit, exact
acceptance metadata, deterministic table mapping, 21 metrics for each of 2024
and 2025, balance/cash/profit tie-outs, 42 usable raw facts, 42 usable canonical
facts, and a full current feature row. The source remains labeled audited HTML,
not XBRL.

Schema pass requires migration v7, `dim_security_share_ratio`, ratio lineage on
financial features, and `resolved_audited_html` as an allowed explicit profile
state. Source-registry pass requires `sec_audited_filing_html` and
`basic_materials_security_ratio_review`.

The isolated 2026-09-05 acceptance build passes with 5,754 filings, 310,109 raw
facts, 239,747 canonical facts, 22,851 FX rows, 47 ratios, and 134 current
feature/coverage rows. Quality is 95 full, 29 partial, 9 insufficient, and 1
stale; 50 rows are rank-ready and 87 valuation-ready. Validation has zero
errors and zero warnings, the cache-only replay is deterministic, 38 tests
pass, and Ruff/independence checks pass. Live file replacement is a separate
authorization boundary.

## Stage 4D - 2019-forward historical feasibility preflight

Source-history presence is not panel completion. Stage 4D freezes the candidate
monthly or 21-session dates beginning no earlier than 2019-01-01 and performs
read-only membership, filing-availability, amendment, FX, market, security-
ratio, and terminal-event feasibility checks. It retains earlier source
history for warm-up and TTM calculations.

Stage 4D must write no historical feature, score, outcome, or rank rows.
Current 2025/2026 ratio evidence cannot be backfilled to 2019. Every historical
source gap must be classified and included in the eventual Stage 6C partition
plan. Pass requires deterministic counts and input hashes, zero streamed
future-availability violations, and a matching cache-only preflight.

## Stage 5A and Stage 6B - specialized source and coverage closure

Stage 5A identifies and freezes the full specialized discovery set, supporting
operands, formulas, definition variants, table families, exact current-plus-
historical ticker-by-metric applicability, source birthdates, and the complete
declared SEC/IR/local-exchange/technical-report source census. The remaining
deactivated-candidate and terminal-event scope must be closed before the final
parser universe seal.

Stage 6B hydrates the sealed source manifest into Basic Materials-owned
content-addressed storage, compiles each unique document once, and executes one
resumable all-issuer/all-metric parse. Review and policy changes replay stored
evidence without opening documents. At most one consolidated residual source
batch may run, excluding completed content hashes.

Pass requires 100% applicability and source-manifest accounting, 100% completed
or resume-linked parser work, zero unexplained failures, at least 80% current
core applicable-pair coverage in every cohort, at least 70% core historical
issuer-period coverage from 2019 forward and 60% in each fixed chronological
block, explicit evidence states for all gaps, and zero unresolved accepted-
store conflicts. Every specialized score weight remains zero.

## Stage 6C - single unified historical PIT panel

Stage 6C is the first and only model-ready historical feature-panel write. It
joins common financial, market, ratio, positioning, commodity/cycle, and
accepted specialized observations using only information available on each
date. Pass requires Stage 4D partition/hash reconciliation, unchanged Stage 6B
observation and metric-disposition hashes, survivorship-correct membership,
terminal outcomes, zero future availability, published year/cohort/metric
coverage, deterministic row hashes, and byte-identical cache-only replay.

## Promotion gate

`portfolio_candidate_gate` and `oos_score_valid_flag` remain false until the
Stage 4D preflight, Stage 5A applicability/source seal, Stage 6B high-coverage
evidence freeze, Stage 6C unified point-in-time panel,
survivorship correction, purged walk-forward out-of-sample validation,
coverage/staleness controls, and portfolio-layer acceptance tests are all
implemented and reviewed. Stage 0-6C engineering output is research
infrastructure, not an investment recommendation.
