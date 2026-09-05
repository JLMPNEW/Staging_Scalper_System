# Basic Materials implementation status

As of 2026-09-05, the independent implementation is complete through Stage 3. `BASIC_MATERIALS_IMPLEMENTATION_PLAN.md` remains the living design authority and must be updated with every implementation change.

| Stage | Status | Evidence |
|---|---|---|
| 0 — independence | Implemented | Strict config, forbidden-import scan, owned database/output/cache paths, closed promotion flags |
| 1 — storage and sources | Implemented | Dedicated SQLite identity, checksummed schema-v3 migration ledger, 10-row package source registry, immutable input fingerprints |
| 2 — current universe | Implemented | Atomic 134-row loader, eight exact cohorts, normalized identifiers, policy-derived calibration groups, validation reports |
| 2B — deactivated candidate intake | Implemented as a review queue | 72 candidates across all eight cohorts; checksummed manifest; 71 provider assets; 16 event URLs; all promotion/calibration flags remain 0 |
| 2B — historical reconciliation pilot | Implemented and calibration-blocked | 20 effective-dated historical memberships; four aliases; 22 security events; 20 terminal terms; all eight cohorts represented |
| 3 — adjusted prices and terminal returns | Implemented; engineering gate passed | 158 assets/162 roles; 537,739 bars; 5,648 actions; XLB/SPY; 134 feature rows; 96.32% rank-ready coverage; 16 resolved and four pending terminal events; read-only validation passed |
| 4 — financial facts, reporting profiles, and FX | Next | Contract, schema v4, SEC/IFRS ingestion, acceptance-time availability, amendments, currencies, units, lineage, common features, and valuation repricing |
| 5+ — specialized metrics, panels, calibration, scoring, ranking | Not started | No score, calibration result, or portfolio output exists |

Current controlled limitations:

- ARIS, AUGO, CRH, MTA, and TII are loaded but non-rank-ready because their quote histories exceed the sparse-session limits.
- SOLS and VMET have shorter histories and remain labeled `partial_history`; the recent-listing policy permits rank readiness while preserving that label.
- ANV, MCP, GMO, and BIOA retain null terminal values pending verified old-equity bankruptcy/liquidation distributions.
- Only 20 of the 72 deactivated candidates are in the governed historical pilot; the remaining 52 require separate promotion evidence.
- All 154 memberships remain calibration-ineligible. `portfolio_candidate_gate` and `oos_score_valid_flag` remain false.

Quality evidence: 22 package tests pass, static checks pass, the Stage 3 full runner is idempotent, the independent read-only Stage 3 validator passes, Stage 2A/2B validators still pass after Stage 3 writes, and the authoritative 134-row universe hash remains `8fe31311a7683e9b207171ace0fe89156fac6154c0a4b40b11c73ed9b9e11be9`.

The next implementation slice is Stage 4. Build the issuer reporting-profile census and contract first, then SEC submissions and Company Facts, point-in-time canonical facts, IFRS/issuer-extension fallback, FX, common financial features, and daily valuation repricing. Specialized cohort parsing should begin only after Stage 4 produces measured coverage gaps.
