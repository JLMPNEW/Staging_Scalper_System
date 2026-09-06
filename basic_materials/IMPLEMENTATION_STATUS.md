# Basic Materials implementation status

As of 2026-09-06, the independent implementation is complete through Stage
4B against the governed 2026-09-05 cutoff. `BASIC_MATERIALS_IMPLEMENTATION_PLAN.md`
remains the living design authority and must be updated with every code,
schema, data-contract, command, or gate change.

| Stage | Status | Evidence |
|---|---|---|
| 0 — independence | Implemented | Strict config, forbidden-import scan across 51 Python files, owned database/output/cache paths, closed promotion flags |
| 1 — storage and sources | Implemented | Dedicated SQLite identity, checksummed append-only schema v6 ledger, 16-row package source registry, immutable input fingerprints |
| 2 — current universe | Implemented | Atomic 134-row loader, eight exact cohorts, normalized identifiers, policy-derived calibration groups, validation reports |
| 2B — deactivated candidate intake | Implemented as a review queue | 72 candidates across all eight cohorts; checksummed manifest; 71 provider assets; 16 event URLs; all promotion/calibration flags remain 0 |
| 2B — historical reconciliation pilot | Implemented and calibration-blocked | 20 effective-dated historical memberships; four aliases; 22 security events; 20 terminal terms; all eight cohorts represented |
| 3 — adjusted prices and terminal returns | Implemented; engineering gate passed | 158 assets/162 roles; 537,739 bars; 5,648 actions; XLB/SPY; 134 feature rows; 16 resolved and four pending terminal events |
| 4A — financial contract and reporting profiles | Implemented; checkpointed | Commit `4b6bce4`; immutable policy/manifest/concept map; 154 SEC-backed profiles; 22 metrics; 66 mappings |
| 4B — point-in-time financial facts, FX, and common features | Implemented; engineering gate passed | Schema v5–v6; immutable SEC/fallback cache; 5,754 filings; 310,067 raw facts; 239,705 canonical facts; 22,852 FX rows; 134 feature/coverage rows; zero validation errors |
| 5 — positioning, commodity data, and foundation audit | Next | Freeze measurement-only commodity/positioning contracts and report the earliest reproducible score date |
| 6+ — specialized metrics, panels, calibration, scoring, ranking | Not started | No calibrated score, published rank, or portfolio output exists |

## Stage 4B production architecture

The live snapshot is
`basic_materials_sec:2026-09-05:06030312536c4d01f7ed`. It uses 140 standard
Company Facts routes and 14 governed exception routes:

- inline XBRL: AUGO, BHP, CMCL, PKX, VMET;
- filing-package XBRL instance: MAKO, SCZM, TII;
- metadata/unstructured: historical AGU and POT, plus ASM and CGAU whose older
  Company Facts remain usable while their latest annual exhibits remain an
  explicit structured-data gap;
- interim-only: RMIX; and
- missing required structured source: OGC, which remains blocked.

The canonical layer contains 109,210 usable and 130,495 superseded facts, with
zero conflicts and zero required FX conversions missing. Of 310,067 raw facts,
56,380 remain quarantined—principally because no exact accession-matched SEC
acceptance time exists—and are not silently promoted.

Current feature quality is 94 `full`, 29 `partial`, 9 `insufficient`, 1
`stale`, and 1 `blocked`. There are 49 rank-ready and 84 valuation-ready current
rows. These are data-readiness flags, not investment rankings.

## Controlled limitations

- ARIS, AUGO, CRH, MTA, and TII are active securities. AUGO and TII are now
  financially rank-ready. ARIS and MTA have usable statements but remain gated
  by missing common metrics and foreign share ratios. CRH has all 22 metrics,
  but three are stale at the cutoff, so it remains conservatively non-rank-ready.
- Forty-eight foreign/current securities keep market cap and valuation fields
  null until an explicit ADR/ordinary-share ratio contract is reviewed.
- OGC remains the only source-blocked current profile. RMIX remains
  interim-only; absent annual/TTM values stay null.
- ANV, MCP, GMO, and BIOA retain null terminal values pending verified
  old-equity bankruptcy/liquidation distributions.
- Only 20 of the 72 deactivated candidates are in the governed historical
  pilot; the remaining 52 require separate promotion evidence.
- All 154 memberships remain calibration-ineligible.
  `portfolio_candidate_gate` and `oos_score_valid_flag` remain false.

## Quality and recovery evidence

- The full package suite passes 36 tests and Ruff is clean.
- Independence and every read-only Stage 2, 2B, 3, 4A, and 4B validator pass
  against the live database.
- A scratch run, live load, and live cache-only rerun reproduce the same
  snapshot identity and exact table counts.
- The authoritative 134-row universe hash remains
  `8fe31311a7683e9b207171ace0fe89156fac6154c0a4b40b11c73ed9b9e11be9`.
- The pre-Stage-4B safety backup is
  `C:/Users/josel/Documents/STAGING/DB/basic_materials.pre_stage4b_20260906.sqlite`
  with SHA-256
  `037f65f73e1c93c8e1a11048200d73725ca62625c325945fd1860d9cf8678258`;
  `PRAGMA quick_check` returned `ok` and the backup remains schema v4.

The next bounded slice is Stage 5: add immutable, point-in-time positioning and
commodity exposure contracts as measurement-only features, then run the
foundation-readiness audit. Specialized cohort parsing starts only where that
audit identifies high-value, sourceable gaps.
