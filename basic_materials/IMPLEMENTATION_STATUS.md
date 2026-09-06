# Basic Materials implementation status

As of 2026-09-06, the independent implementation has a fully validated Stage
4C acceptance candidate against the governed 2026-09-05 cutoff. The installed
live database remains on the prior Stage 4B snapshot until replacement is
explicitly authorized. `BASIC_MATERIALS_IMPLEMENTATION_PLAN.md` remains the
living design authority and must be updated with every code, schema,
data-contract, command, or gate change.

| Stage | Status | Evidence |
|---|---|---|
| 0 - independence | Implemented | Strict config, forbidden-import scan across 54 Python files, owned database/output/cache paths, closed promotion flags |
| 1 - storage and sources | Stage 4C candidate implemented | Checksummed schema v7 ledger; 18-row package source registry; owned security-ratio and audited-HTML sources |
| 2 - current universe | Implemented | Atomic 134-row loader, eight exact cohorts, normalized identifiers, policy-derived calibration groups, validation reports |
| 2B - deactivated candidate intake | Implemented as a review queue | 72 candidates across all eight cohorts; checksummed manifest; 71 provider assets; 16 event URLs; all promotion/calibration flags remain 0 |
| 2B - historical reconciliation pilot | Implemented and calibration-blocked | 20 effective-dated historical memberships; four aliases; 22 security events; 20 terminal terms; all eight cohorts represented |
| 3 - adjusted prices and terminal returns | Implemented; engineering gate passed | Clean candidate build has 158 assets/162 roles; 492,653 bars; 5,388 actions; XLB/SPY; 134 feature rows; 16 resolved and four pending terminal events |
| 4A - financial contract and reporting profiles | Implemented; checkpointed | Commit `4b6bce4`; immutable policy/manifest/concept map; 154 SEC-backed profiles; 22 metrics; 66 mappings |
| 4B - SEC facts, FX, and one-cutoff common features | Implemented; engineering gate passed | Exact acceptance-time history; 5,754 filings; 310,109 raw facts; 239,747 canonical facts; 22,851 FX rows; 134 current feature/coverage rows |
| 4C - OGC and listed-security-unit remediation | Implemented and independently validated; live promotion pending | OGC audited HTML route; 47 effective-dated ratios; five exact ADS conversions; schema v7; zero validation errors/warnings; deterministic cache-only replay |
| 4D - longitudinal common point-in-time panel | Next | Build monthly/21-session feature rows from 2019-01-01 with filing-availability, membership, ratio-history, and no-lookahead gates |
| 5A - specialized metric definition and source census | Planned after Stage 4D | Freeze metric registry, definition variants, applicability matrix, source birthdates, and bounded parser targets |
| 6B - specialized metric loading | Not started | Load accepted observations as measurement-only features after source/applicability validation; all weights remain zero |
| 6+ - calibration, scoring, ranking | Not started | No calibrated score, published rank, or portfolio output exists |

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
panel layer. Stage 4D must reconstruct each scheduled date using only facts,
membership, prices, FX, and security ratios available on that date. The current
ratio evidence cannot be projected backward.

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
- Candidate specialized metrics are described by cohort in the master plan,
  but no formal specialized registry, applicability matrix, or observation
  table has been loaded. Formal identification is Stage 5A; measurement-only
  loading is Stage 6B, after Stage 4D and the foundation audit.
- All 154 memberships remain calibration-ineligible.
  `portfolio_candidate_gate` and `oos_score_valid_flag` remain false.

## Quality, recovery, and deployment state

- The full package suite passes 38 tests and Ruff is clean.
- The independence validator passes across 54 Python files.
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
- Live replacement was not performed because it requires explicit deployment
  authorization. No Stage 4C claim in this document implies that the current
  live file has already been replaced.

The next bounded implementation slice is Stage 4D, not scoring: build and
validate the 2019-01-01-forward common point-in-time panel. Stage 5A then
formalizes specialized metrics and sourceability; Stage 6B loads only accepted
specialized observations as zero-weight measurement features.
