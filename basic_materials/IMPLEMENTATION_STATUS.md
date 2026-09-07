# Basic Materials implementation status

As of 2026-09-06, the independent implementation has a fully validated Stage
4C acceptance candidate and a separate Stage 4D/5A F0 extraction-contract
candidate. The F0 candidate passed structural and no-write feasibility checks,
but remains intentionally blocked from historical materialization by seven
open evidence gates. The installed live database remains on the prior Stage 4B
snapshot until replacement is explicitly authorized.
`BASIC_MATERIALS_IMPLEMENTATION_PLAN.md` remains the living design authority
and must be updated with every code, schema, data-contract, command, or gate
change.

| Stage | Status | Evidence |
|---|---|---|
| 0 - independence | Implemented | Strict config, forbidden-import scan across 60 Python files, owned database/output/cache paths, closed promotion flags |
| 1 - storage and sources | F0 candidate implemented | Checksummed schema v8 ledger; 26-row package source registry; owned specialized metric/source, content-addressed document, and resumable parser ledgers |
| 2 - current universe | Implemented | Atomic 134-row loader, eight exact cohorts, normalized identifiers, policy-derived calibration groups, validation reports |
| 2B - deactivated candidate intake | Implemented as a review queue | 72 candidates across all eight cohorts; checksummed manifest; 71 provider assets; 16 event URLs; all promotion/calibration flags remain 0 |
| 2B - historical reconciliation pilot | Implemented and calibration-blocked | 20 effective-dated historical memberships; four aliases; 22 security events; 20 terminal terms; all eight cohorts represented |
| 3 - adjusted prices and terminal returns | Implemented; engineering gate passed | Clean candidate build has 158 assets/162 roles; 492,653 bars; 5,388 actions; XLB/SPY; 134 feature rows; 16 resolved and four pending terminal events |
| 4A - financial contract and reporting profiles | Implemented; checkpointed | Commit `4b6bce4`; immutable policy/manifest/concept map; 154 SEC-backed profiles; 22 metrics; 66 mappings |
| 4B - SEC facts, FX, and one-cutoff common features | Implemented; engineering gate passed | Exact acceptance-time history; 5,754 filings; 310,109 raw facts; 239,747 canonical facts; 22,851 FX rows; 134 current feature/coverage rows |
| 4C - OGC and listed-security-unit remediation | Implemented and independently validated; live promotion pending | OGC audited HTML route; 47 effective-dated ratios; five exact ADS conversions; schema v7; zero validation errors/warnings; deterministic cache-only replay |
| 4D - historical PIT feasibility preflight | Implemented; feasibility passed; PIT write blocked | 93 monthly last-session dates from 2019-01-31 through 2026-09-04; three fixed chronological blocks; deterministic input seals; database unchanged; seven explicit blockers |
| 5A - specialized metric, applicability, and all-source census | Structurally implemented; review/source seal open | 64 metrics, 16 operand links, exact 9,856-row identity-metric matrix, 5,640-row eight-family source census; zero structural errors; 980 applicability reviews and 5,163 source rows remain open |
| 6B - one-pass specialized capture and coverage closure | Not started | Content-address all sources, compile each unique document once, run one resumable all-metric parse, use parse-free review, and meet the high-coverage gate with all weights zero |
| 6C - unified historical point-in-time panel | Blocked by Stage 6B coverage | Materialize common, cycle, positioning, and specialized features together once from 2019-01-01 |
| 7+ - diagnostics, calibration, scoring, ranking | Not started | No calibrated score, published rank, or portfolio output exists |

## Stage 4C acceptance candidate

The validated candidate snapshot is
`basic_materials_sec:2026-09-05:2cf5219d8855588066ba`. It uses 140 standard
Company Facts routes and 14 governed exception routes:

- inline XBRL: AUGO, BHP, CMCL, PKX, and VMET;
- filing-package XBRL instance: MAKO, SCZM, and TII;
- audited filing HTML: OGC;
- metadata/unstructured: historical AGU and POT, plus ASM and CGAU whose older
  Company Facts remain usable while their latest annual exhibits remain an
  explicit structured-data gap; and
- interim-only: RMIX.

OGC is no longer source-blocked. Its SEC-hosted audited IFRS exhibit produces
exactly 42 usable raw and 42 canonical observations: 21 common metrics for each
of 2024 and 2025. The parser requires exact tables and performs balance-sheet,
ending-cash, and net-profit tie-outs. OGC's current feature row is `full`, with
21 of 22 canonical metrics and direct-share ratio 1.

The listed-security-unit contract contains exactly 47 current ratio-required
foreign securities: 42 direct share listings and five ADS listings. The ADS
ratios, expressed as issuer shares per traded ADS, are BHP 2, ELVR 10, PKX
0.25, RIO 1, and TX 10. Market cap uses:

`USD price * diluted issuer shares / issuer shares per traded security`.

No missing ratio is inferred. Each row is effective-dated, bound to an exact
SEC filing, acceptance timestamp, URL, payload hash, reviewed row hash, and
policy hash. The ratio contract is current-snapshot-only and does not authorize
historical backfill.

Candidate feature quality is 95 `full`, 29 `partial`, 9 `insufficient`, and 1
`stale`, with no blocked row. There are 50 financially rank-ready and 87
valuation-ready current rows. These are data-readiness states, not investment
rankings.

## Historical coverage: what is and is not loaded

The source layers do extend past 2019:

- raw SEC facts: 2009-01-01 through 2026-07-05; 147,577 rows and 145 tickers
  with periods on or after 2019-01-01;
- canonical financial facts: 2009-01-03 through 2026-07-05; 134,378 rows and
  145 tickers with periods on or after 2019-01-01;
- adjusted prices: 2009-01-02 through 2026-09-04; and
- FX: 2009-01-01 through 2026-09-04.

That is not yet a longitudinal model-ready feature panel. The
`feature_financial_statement` table has 134 rows for only one as-of date,
2026-09-05. Therefore the answer to "is the historical time series aligned
with other repositories from 2019-01-01?" is **no** at the feature/scoring
panel layer. Stage 4D has now audited each scheduled date using only facts,
membership, prices, FX, and security ratios available on that date without
writing a common-only panel. The current ratio evidence cannot be projected
backward. Stage 6C will write the first and only unified historical panel after
specialized coverage is frozen.

## Stage 4D/5A F0 extraction-contract candidate

F0 is implemented in a new isolated database at
`output/basic_materials/verification_f0_20260906/basic_materials.sqlite`.
It migrated the Stage 4C candidate to schema v8; it did not replace or mutate
the installed live database. The candidate database SHA-256 is
`494d906ac1b968f6c50ff5248998db387ae199d7ad45d3e101b99934a8fa0605`.

The Stage 5A registry freezes 64 cohort-specific fields across all eight
cohorts. It distinguishes direct observations, supporting operands, and
derived metrics; contains 16 explicit operand links; and records formulas,
definition variants, units, periods, dimensions, table families, source
families, and plausibility bounds. Every production weight, scoring flag, and
calibration flag is zero.

The loader created:

- 9,856 identity-metric rows, exactly `154 identities x 64 metrics`;
- 188 policy-reviewed applicable pairs, 8,688 explicit not-applicable pairs,
  and 980 issuer-selective pairs requiring review;
- 5,640 source-census rows across SEC filings, issuer IR, local exchanges,
  archives, technical reports, reserve/resource sources, commodity sources,
  and positioning sources;
- 3,874 identified SEC filing rows from the 2017 warm-up boundary forward,
  1,289 source discoveries still required, and 477 explicit not-applicable
  source dispositions; and
- content-addressed document, source-document bridge, parser work, candidate,
  and accepted-observation tables for the one-pass F1/F2 workflow. These
  parser/evidence tables remain empty until the source and universe seals pass.

The Stage 4D audit froze 93 monthly last-XNYS-session dates from 2019-01-31
through 2026-09-04. It proved the database was byte/row-count unchanged and
that source history is technically feasible. Role-eligible market coverage is
at least 99.89% in each fixed block. Role-eligible common-financial feasibility
is 71.18% in 2019-2020, 70.67% in 2021-2022, and 75.79% from 2023 forward.
These ratios are feasibility diagnostics, not permission to build the panel.

Historical PIT materialization remains blocked by exactly:

1. 52 unresolved deactivated-candidate decisions;
2. four unresolved terminal distributions;
3. historical membership reconstruction for all 134 current-snapshot names;
4. 980 issuer-selective applicability reviews;
5. 5,163 source rows requiring discovery or content hydration;
6. the not-yet-executed all-document parser work ledger; and
7. specialized coverage, which cannot be measured until accepted observations
   exist.

The full evidence packs are under
`output/basic_materials/verification_f0_20260906/stage5a_contract` and
`output/basic_materials/verification_f0_20260906/stage4d_preflight`.

## Controlled limitations and next gates

- ARIS and MTA now have governed direct-share ratios but remain non-rank-ready
  because common financial metrics are missing. CRH remains non-rank-ready due
  to stale observations. AUGO and TII remain rank-ready. All five are active.
- RMIX is an active domestic interim filer and correctly uses direct issuer
  shares; it is not part of the 47 foreign-ratio contract.
- ANV, MCP, GMO, and BIOA retain null terminal values pending verified
  old-equity bankruptcy/liquidation distributions.
- Only 20 of the 72 deactivated candidates are in the governed historical
  pilot; the remaining 52 require separate promotion evidence.
- The formal specialized registry, complete applicability matrix, source
  census, and observation/parser tables are now loaded in the isolated F0
  candidate. Stage 5A is structurally valid but not sealed; measurement-only
  extraction and high-coverage closure remain Stage 6B work. Stage 6C
  historical materialization remains blocked until all F0-F2 gates pass.
- All 154 memberships remain calibration-ineligible.
  `portfolio_candidate_gate` and `oos_score_valid_flag` remain false.

## Quality, recovery, and deployment state

- The full package suite passes 41 tests and Ruff is clean.
- The independence validator passes across 60 Python files.
- The fresh Stage 0-4C build and cache-only replay reproduce snapshot
  `basic_materials_sec:2026-09-05:2cf5219d8855588066ba`; Stage 4C validation
  reports zero errors and zero warnings.
- The authoritative 134-row universe hash remains
  `8fe31311a7683e9b207171ace0fe89156fac6154c0a4b40b11c73ed9b9e11be9`.
- The existing pre-Stage-4B safety backup remains at
  `C:/Users/josel/Documents/STAGING/DB/basic_materials.pre_stage4b_20260906.sqlite`.
- The verified Stage 4C candidate remains at
  `output/basic_materials/verification_stage4c_20260906/basic_materials.sqlite`
  with SHA-256
  `d33d75426aeacde0592871febcf96e4afe9824a04c8af81d221058804dbf1963`.
- The Stage 4D/5A F0 candidate remains at
  `output/basic_materials/verification_f0_20260906/basic_materials.sqlite`
  with SHA-256
  `494d906ac1b968f6c50ff5248998db387ae199d7ad45d3e101b99934a8fa0605`.
- Live replacement was not performed because it requires explicit deployment
  authorization. No Stage 4C claim in this document implies that the current
  live file has already been replaced.

The next bounded implementation slice is F0 closure, not a historical build or
scoring. Resolve/reject the 52 historical candidates and four terminal
distributions first; reconstruct effective-dated history for the 134 current
names; complete the 980 applicability reviews; then discover and hydrate the
5,163 open source rows. Only after those inputs are hash-sealed should F1
compile each unique content hash once and F2 execute one resumable all-metric
parse. Only Stage 6C may build the unified historical PIT panel.
