# Basic Materials implementation status

As of 2026-09-05, the independent implementation is complete through Stage 4A. `BASIC_MATERIALS_IMPLEMENTATION_PLAN.md` remains the living design authority and must be updated with every implementation change.

| Stage | Status | Evidence |
|---|---|---|
| 0 — independence | Implemented | Strict config, forbidden-import scan, owned database/output/cache paths, closed promotion flags |
| 1 — storage and sources | Implemented | Dedicated SQLite identity, checksummed schema-v4 migration ledger, 16-row package source registry, immutable input fingerprints |
| 2 — current universe | Implemented | Atomic 134-row loader, eight exact cohorts, normalized identifiers, policy-derived calibration groups, validation reports |
| 2B — deactivated candidate intake | Implemented as a review queue | 72 candidates across all eight cohorts; checksummed manifest; 71 provider assets; 16 event URLs; all promotion/calibration flags remain 0 |
| 2B — historical reconciliation pilot | Implemented and calibration-blocked | 20 effective-dated historical memberships; four aliases; 22 security events; 20 terminal terms; all eight cohorts represented |
| 3 — adjusted prices and terminal returns | Implemented; engineering gate passed | v2 listing-window contract; 158 assets/162 roles; 537,739 bars; 5,648 actions; XLB/SPY; 134 feature rows; 100% rank-ready coverage; 16 resolved and four pending terminal events; read-only validation passed |
| 4A — financial contract and reporting profiles | Implemented; engineering gate passed | Schema v4; immutable policy/manifest/concept map; 154 SEC-backed profiles; 22 metrics; 66 concept mappings; atomic idempotent load; read-only validation passed |
| 4B — point-in-time financial facts, FX, and common features | Next | SEC filing/fact ingestion, fallback resolution, amendments, units, currencies, lineage, canonical facts, FX, common features, and valuation repricing |
| 5+ — specialized metrics, panels, calibration, scoring, ranking | Not started | No score, calibration result, or portfolio output exists |

Current controlled limitations:

- ARIS, AUGO, CRH, MTA, and TII are active. Their prior sparse-history warnings
  were resolved by excluding pre-major-exchange OTC/ADR sessions from current
  coverage and feature windows.
- ELE, MAKO, OGC, SCZM, SOLS, TII, and VMET have fewer than 253 governed
  major-exchange observations and remain labeled `partial_history`; the
  recent-listing policy permits rank readiness while preserving that label.
- ANV, MCP, GMO, and BIOA retain null terminal values pending verified old-equity bankruptcy/liquidation distributions.
- Only 20 of the 72 deactivated candidates are in the governed historical pilot; the remaining 52 require separate promotion evidence.
- Fourteen reporting profiles are in the explicit Stage 4B review queue:
  ASM, BHP, CGAU, CMCL, PKX, and TII require Company Facts fallback handling;
  AUGO, MAKO, SCZM, VMET, AGU, and POT require taxonomy review; and OGC and
  RMIX require annual-form review. MAKO and SCZM have no SEC Company Facts
  payload. Seven profiles retain unresolved reporting currency rather than using
  listing currency as a substitute.
- All 154 memberships remain calibration-ineligible. `portfolio_candidate_gate` and `oos_score_valid_flag` remain false.

Quality evidence: 29 package tests pass, static checks pass, the Stage 3 full
runner and Stage 4A loader are idempotent, all independent read-only Stage
0–4A validators pass after the schema-v4 load, and the authoritative 134-row
universe hash remains
`8fe31311a7683e9b207171ace0fe89156fac6154c0a4b40b11c73ed9b9e11be9`.

The next implementation slice is Stage 4B. Load SEC submissions and Company
Facts into the schema-v4 raw tables, resolve the 14-profile exception queue,
canonicalize acceptance-bounded US-GAAP/IFRS facts, preserve amendments and
lineage, add point-in-time FX, and then compute common financial features and
daily valuation repricing. Specialized cohort parsing begins only after measured
Stage 4B coverage identifies high-value filing-text gaps.
