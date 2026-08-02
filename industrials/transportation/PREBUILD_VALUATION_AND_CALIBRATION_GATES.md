# Transportation pre-build valuation and calibration gates

Status date: 2026-07-30

## Purpose

This gate sequence prevents another expensive historical rebuild until the transportation universe, point-in-time valuation inputs, and calibration component contract are complete. It uses the shared industrials configuration, database, production-lock, reporting, and OOS research infrastructure while keeping transportation-specific classification and source exceptions inside the transportation package.

The valuation source gate now passes and authorizes one historical valuation/feature rebuild. Calibration remains unauthorized until the rebuilt-panel preflight passes.

## Implemented contracts

1. Economic classification is effective-dated and separates three calibration pools: surface freight and logistics, air transport and aviation services, and marine shipping and maritime.
2. Risk and portfolio roles are separate from economic identity. Development/speculative issuers remain in their economic pool but are research-only. Airlines are calibration research satellites and cannot become portfolio candidates. A reviewed `universe_review` role also fails closed.
3. Operating-core calibration candidates assign zero weight to `development_stage_risk_score`. Candidate scoring requires every positive-weight component; missing components cannot be silently renormalized.
4. Valuation availability distinguishes a missing point-in-time market denominator from an economically meaningless negative-profit multiple.
5. Outstanding-share facts are filtered by both period end and filing date, so facts unavailable on the requested date cannot enter historical market capitalization.
6. ADR, ADS, and direct-share conversions are effective-dated and require a primary-source URL before a row can become reviewed.

## Current evidence

The point-in-time valuation-source audit completed successfully over all 160 active and inactive seed tickers:

- 112 active plus 48 delisted tickers were audited.
- 159 tickers have historical-membership rows.
- RRTS is the sole seed-only approved price exclusion and does not silently disappear.
- CGI/Celadon has a reviewed 2019-12-09 economic-terminal membership and 6,489 loaded `CGIP` bars; ticker `GIB` is never substituted.
- 107 operating issuers overlap the 2019-01-02 through 2026-07-30 research window and require valuation sources.
- All 107 required issuers are source-ready; the blocker count is zero.
- Current active coverage is 112/112 for both shares outstanding and public float.
- Historical endpoint coverage is 138/139 for shares outstanding. The only miss is seed-only CGI; required coverage is complete.
- Historical public-float coverage is 126/139. Missing float remains unavailable and is not fabricated from shares outstanding.

The existing validated OOS panel contains 11,765 eligible train/validation rows at the 63-session horizon. All seven bounded candidates currently fail the new input preflight for one reason only: `valuation_score` coverage is 0%. The development-risk component is now correctly masked and is no longer a false blocker.

## Correct remaining sequence

1. Build historical market capitalization and compute `fcf_yield`, `ev_operating_income`, and `valuation_score` for the full survivorship-corrected panel in one pass. Do not rerun the filing search or specialized-metric parser.
2. Rebuild financial/market feature history, daily score history, and the generic OOS panel once. Rerun `26aa_audit_transportation_calibration_inputs.py`; every candidate must meet the 90% strict complete-row gate.
3. Only after the preflight passes, run the bounded candidate calibration, holdout, and walk-forward gates. A failure remains a legitimate zero-overlay outcome; a pass may proceed through readiness audit, promotion, immutable production lock, portfolio adapter validation, and activation.
4. At the completed batch boundary, commit the scoped source/config/test changes and mint a successor release seal. Do not overwrite the prior immutable release.

## Acceptance gates

| Gate | Requirement | Current result |
| --- | --- | --- |
| Universe census | 160/160 active plus inactive seeds visible | PASS |
| Historical membership reconciliation | Seed-only exclusions explicit | PASS: RRTS only; CGI reviewed terminal override |
| Classification leakage | Speculative and review roles cannot receive valid production OOS portfolio status | PASS |
| Calibration structural mask | Development risk has zero weight in operating-core candidates | PASS |
| Valuation source audit execution | Complete, deterministic report with no parse errors | PASS |
| Valuation rebuild readiness | All required research-window issuers source-ready or explicitly excluded | PASS: 107/107; zero blockers |
| Existing-panel component preflight | Each bounded candidate has at least 90% complete positive-weight rows | FAIL: valuation 0% |
| Historical rebuild | Run once after valuation source readiness passes | AUTHORIZED; NOT YET RUN |
| Calibration/holdout/walk-forward | Run only after rebuilt-panel preflight passes | NOT RUN |
| Promotion and production lock | Require passing OOS evidence and shared governance gates | NOT AUTHORIZED |

## Generated evidence

- `output/industrials/transportation/valuation/transportation_pit_valuation_source_audit.csv`
- `output/industrials/transportation/valuation/transportation_pit_valuation_source_audit.json`
- `output/industrials/transportation/stage3/transportation_share_snapshot_coverage.csv`
- `output/industrials/transportation/historical_load/transportation_historical_share_snapshot_coverage.csv`
- `output/industrials/transportation/generic_oos/transportation_calibration_input_preflight.csv`
- `output/industrials/transportation/generic_oos/transportation_calibration_input_preflight.json`
