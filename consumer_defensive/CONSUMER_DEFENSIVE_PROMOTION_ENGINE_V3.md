# Consumer Defensive Four-Layer Promotion Engine v3

## Status and boundary

The v3 engine is implemented and registered in `consumer_defensive/config.yaml`, but running the decision engine alone is not a production activation. The engine is report-only: it cannot edit a database, Portfolio Layer configuration, or live weights. The separately reviewed registry dated `2026-08-27` is now pinned by exact path and SHA, effective `2026-08-28` through `2026-10-29`; this is the current production authority.

Consumer Defensive owns this policy and its evidence. It does not import another sector's promotion code. Shared infrastructure remains limited to explicit service boundaries such as Dedicated Parser, factor validation, Portfolio Layer, and the global orchestrator.

The v2 foundation, preregistration, and calibration entry points remain available for compatibility and evidence generation. The retired v2 promotion protocol stays archived. V3 is the promotion and capital-allocation decision contract.

## The four layers

1. **Data and safety validation.** All required attestations and the complete 21-, 63-, and 126-session evidence paths must pass. The engine rejects incomplete chronology, look-ahead, unmatched benchmarks, unverified hashes, unreconciled corporate actions or terminal events, non-net returns, unsafe concentration, drawdown, expected-shortfall, turnover, cost, or capacity evidence. A hard failure produces no deployable capital.

2. **Economic performance score.** Each cohort receives its own 0–100 score. The weighted blocks are benchmark-relative return (35%), absolute profitability (25%), risk efficiency (20%), and deployability (20%). Horizon weights are 25% / 50% / 25% for 21 / 63 / 126 sessions. LCB and profit factor are inputs to this score rather than isolated vetoes.

3. **Confidence adjustment.** The score is shrunk toward 50 using deflated Sharpe and inverse probability of backtest overfitting. Confidence is applied once; it is not also multiplied into the capital cap.

4. **Controlled standard allocation.** Safety, account-adjusted capacity of at least 1.0x, and a confidence-adjusted economic score of at least 60 form a binary production gate. A qualifying cohort receives its complete equal slot of the Portfolio-owned Consumer Defensive sector budget. There is no canary/pilot fraction and no cohort-level diversification haircut. An ineligible slot remains cash. Cohorts are calibrated and monitored independently; hard failures roll active authority back, material model changes reset it to benchmark-only monitoring, and every activation lock expires after 63 days.

## Account-relative liquidity capacity

The calibration's `liquidity_capacity_ratio` is raw evidence measured against
the sealed calibration reference notional. It is not, by itself, the amount of
capital the current account requires a cohort to absorb. Promotion v3 therefore
keeps the raw ratio immutable and derives two auditable values:

```text
executable_capacity_usd = raw_capacity_ratio * calibration_reference_notional_usd
allocation_adjusted_capacity_ratio = executable_capacity_usd / sector_max_notional_usd
```

Layers 1, 2, and 4 use the allocation-adjusted ratio. The bridge obtains account
AUM and the sector ceiling from a Portfolio-owned `portfolio_capital_context_v1`
artifact, validates its exact file SHA and internal payload SHA independently,
and reconciles the calibration reference notional to the sealed preregistration.
Consumer Defensive does not import Portfolio Layer code. A capital-context
change is deployment evidence only: it cannot add observations, claim fresh
chronological evidence, or satisfy the new-input-panel transition requirement.

The capacity test is deliberately conservative. Every cohort is tested as if it
had to absorb the complete Consumer Defensive sector budget, not one quarter of
that budget. For the report-only context dated `2026-08-28`, account AUM is
`$500,000`, the eight-sector ceiling is `12.5%`, and the tested Consumer budget
is `$62,500`. The corrected sealed decision passes capacity for all four cohorts;
the minimum headroom is 188.94x for Beverages, 3.78x for Distribution/Retail,
12.48x for Household/Personal/Tobacco, and 10.83x for Packaged Foods/Agriculture.
Because all four cohorts qualify, each receives one fourth of the 12.5% sector
budget: 3.125% of account AUM, or `$15,625` at the planning AUM. Total Consumer
Defensive authority is therefore 12.5%, or `$62,500`. Capacity modifiers are
exactly 1.0 whenever the adjusted ratio is at least 1.0; a ratio below 1.0 fails
Layer 1 and receives zero authority. Portfolio Layer now pins this registry
through the production change-control workflow. A missing, changed, stale, or
expired registry removes authority fail-closed.

## Evidence workflow

All artifacts are immutable and hash-bound. Use new output paths for every run; the commands refuse to overwrite an existing artifact.

1. Build and independently validate the prior promotion input and decision. Preserve their external SHA pins.
2. Before observing the future review sample, create a review plan with `30a_manage_consumer_defensive_promotion_evidence_v3.py plan`. The plan freezes the review window, eligible return and outer-OOS dates, candidate/model contracts, prior evidence hashes, and methodology-file hashes.
3. Create the separate registration anchor with the `anchor` subcommand. Record its SHA outside the generated artifact and outside the evidence directory used by the engine.
4. After the scheduled window closes, build the current promotion input from matched point-in-time cohort, XLP, and SPY daily paths plus exact outer-OOS observation identities. Do not replace missing peer returns or assert freshness manually.
5. Run the `manifest` subcommand. It proves the current paths and outer-OOS identities are exact extensions of the preregistered prior evidence.
6. Run `30_run_consumer_defensive_promotion_engine_v3.py` with the prior decision/input, their trusted decision SHA, the plan, anchor, externally trusted anchor SHA, fresh manifest, current promotion input, and an optional rank table.
7. Independently review the decision, activation registry, cohort lock arithmetic, dates, selected candidate/model contracts, and hashes. Do not pin a registry whose review or activation validity window has expired.
8. Production consumption requires an explicit Portfolio Layer change-control action that pins both `production_activation_registry_file_path` and `production_activation_registry_sha256`. The file must remain inside the configured Consumer Defensive output root. Update the exact per-cohort alpha and cap maps to the registry values under the same reviewed change. A path without its SHA, a SHA without its path, a mismatched row/lock, an expired lock, or any unpinned registry fails closed.

The reviewed Portfolio change must update all capital-control surfaces together: enable the Consumer score contract; copy each lock's `expected_alpha_at_full` into `calibration_by_scope`; copy each lock's `optimizer_cap` into both `optimizer_cap_by_scope` and `optimizer.scope_weight_caps.consumer_defensive`; and set `optimizer.sector_weight_caps.consumer_defensive` to the reviewed aggregate ceiling. Leaving any surface at zero is a safe non-activation, not a partial promotion. The score adapter independently reproduces the registry and row bindings, while the optimizer and its output validator independently enforce overlapping sector and cohort caps through final decimal rounding.

The aggregate sector ceiling must equal the Portfolio-owned Consumer Defensive
budget represented by all four reserved full slots. Per-cohort investable caps
must match their locks exactly. When a cohort fails, the difference between the
12.5% sector ceiling and the sum of active cohort caps becomes an explicit cash
reservation; it cannot be reassigned to another cohort or sector. Positive
Consumer authority also requires Portfolio gross exposure in `(0, 1]`;
leverage cannot multiply an absolute reviewed cap.

After the pin change, run the Portfolio score collection and optimizer validation in the normal orchestrated release rehearsal. Authority is checked against both the source snapshot date and the actual Portfolio run date. The registry cannot be shifted or extended beyond 63 days from its decision date. Stage 1 revalidates every cohort cap and alpha against the pinned registry and seals the complete Portfolio configuration hash; Stage 3 and its independent output validator reject any later config change. Production is authorized only if the registry is included in archived source files and all authority, sector-cap, and scope-cap gates pass.

Qualifying design evidence may authorize the standard allocation directly. Fresh, preregistered chronological evidence is still required for subsequent active reviews and reauthorization. A material model change resets authority to benchmark-only monitoring rather than bypassing review.

## Current production wiring

- Consumer configuration is active, enabled, and required, with a 12.5% sector ceiling.
- Portfolio Layer treats Consumer Defensive as required sector eight. It independently pins registry payload SHA 1722d00239df6625045197f3e95752fa6911f0d80910c3cd40ecabf07073e0e1, all four expected-alpha values, four 3.125% cohort caps, and the 12.5% overlapping sector cap.
- All four current cohort locks are active_full. Each receives the full standard slot when it has eligible current rows. A future failed cohort keeps its 3.125% slot in cash; the slot is not reassigned.
- Script 31_publish_consumer_defensive_production_scores_v3.py reads one coherent query-only SQLite transaction, requires the exact prior XNYS signal session, binds every row to the selected candidate and activation lock, and records the source database main-file, WAL, and SQLite data-version identities.
- Script 32_run_consumer_defensive_production_refresh_v3.py owns the Consumer-only daily production path. It publishes the dated rank file under output/consumer_defensive/dashboard/{date} and a terminal PASS/FAIL manifest under output/consumer_defensive/orchestration/{date}. Ordinary retries preserve valid caches; cache bypass requires explicit --force-refresh.
- The global orchestrator runs Consumer Defensive at dependency tier 0 and blocks the tier-1 Portfolio run if Consumer's fresh rank or terminal PASS manifest is absent. The sector therefore cannot be silently skipped from a future allocation.

### Governed scoring scope

The scope-leak correction applies reviewed calibration exclusions before any
cohort or full-universe cross-sectional normalization. Stage 6A now uses the
immutable definition `consumer_defensive_scoring_features_v3`; its Stage 7
successor is `consumer_defensive_stage7_baseline_v4` with contract schema
`consumer_defensive_stage7_contract_v3`. Historical Stage 6A v2 and Stage 7 v3
evidence remains sealed under its original identity and is not relabeled.

The governed current universe contains exactly 79 names: 12 Beverages, 23
Consumer Staples Distribution/Retail, 20 Household/Personal/Tobacco, and 24
Packaged Foods/Agricultural Products. The other 31 current source names are
explicit reviewed exclusions. The immutable `calibration_scope_sha256` is
`b9993085e910504b386484f3642db7c11e4ccc0ad82f170da19ab06981c03c68`.
It is embedded in the Stage 6 contract and every published rank row. Changing
membership or a cohort assignment requires a new scope contract and model
identity.

### Fail-closed handoff and accepted replay

The publisher verifies the exact scope, source/remaining/excluded counts,
cohort counts, ticker-set hash, input and component manifests, rank-file hash,
and canonical SHA-256 of every rank row. The terminal refresh independently
reloads the pinned candidate and activation registries, reconstructs each
cohort's authoritative lock, and rejects any row or publisher identity that
does not match. Portfolio Layer then requires the same-date terminal PASS and
independently revalidates the rank snapshot, publisher manifest, governed
scope, ticker set, cohort counts, activation locks, and row hashes. Missing,
extra, altered, stale, or out-of-scope data fails closed at each boundary.

The accepted production replay is
`output/consumer_defensive/orchestration/2026-08-28/consumer_defensive_production_refresh_manifest_v3.json`.
It records PASS for the complete 17-step Consumer-only refresh. Its paired
publisher manifest under `output/consumer_defensive/dashboard/2026-08-28/`
publishes exactly 79 governed rows. Stage 7 v4 remains the registered successor
identity; this production path publishes calibrated scores from validated Stage
6A and does not claim that a separate canonical Stage 7 v4 snapshot produced
those rows.

The allocation-critical candidate contracts currently use core factors and zero
specialized-metric weights. Specialized disclosures remain connected to the
Consumer-owned Dedicated Parser and shared factor-validation service as a
measurement/research lane; they are intentionally not allowed to make the
daily Portfolio allocation unavailable until separately validated nonzero
weights are approved.

## Report-only run

```powershell
C:\Users\josel\Miniconda3\python.exe consumer_defensive\scripts\30_run_consumer_defensive_promotion_engine_v3.py `
  --promotion-input <current-promotion-input.json> `
  --previous-decision <previous-decision.json> `
  --trusted-previous-decision-sha256 <externally-pinned-sha256> `
  --previous-promotion-input <previous-promotion-input.json> `
  --preregistration <review-plan.json> `
  --registration-anchor <registration-anchor.json> `
  --trusted-registration-anchor-sha256 <externally-pinned-anchor-sha256> `
  --fresh-evidence-manifest <fresh-evidence-manifest.json> `
  --rank-table <validated-rank-table.csv> `
  --output-dir <new-immutable-output-directory>
```

Expected outputs are `consumer_defensive_promotion_decision_v3.json`, `consumer_defensive_activation_registry_v3.json`, and, when requested, `consumer_defensive_activated_rank_table_v3.csv`. Their creation does not activate production.

## Production invariant

No research result, score, generated registry, or rank-table candidate activates capital by itself. Production authority exists only while a reviewed registry is pinned by exact path and SHA, its cohort locks are internally valid and current, the corresponding rank rows match those locks, and Portfolio Layer's independent safety checks accept the handoff.
