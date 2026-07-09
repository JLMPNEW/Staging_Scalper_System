# Portfolio Layer — Staged Implementation Plan & Acceptance Gates

This package is the **shared portfolio-construction layer** that sits above all sector/sub-sector
AQR pipelines (biotech, med_devices, semiconductors, software-infra, and future sleeves). The sector
pipelines decide **WHO** to own (security selection / alpha). This layer decides **HOW MUCH** and
**WHEN** (sizing, timing, regime, allocation, exits), plus a probabilistic forecasting brain on top.

## Guiding principles

1. **Two layers, kept separate.** Sector AQR = security selection (WHO). `portfolio_layer/` = timing,
   sizing, allocation, hedging (HOW MUCH / WHEN). They join only through stable data contracts.
2. **Full independence from PROD.** Vendor a self-contained copy of the needed PROD components and
   re-root every path into Staging. No import or path reaches back to `PROD_Scalper_System`. The fork
   will drift from PROD — that is intended.
3. **One shared layer above all sectors.** Nothing sector-specific lives in it.
4. **Two comparability problems, never conflated.** *Intra-sector* (which names) → calibrated
   expected-alpha score. *Inter-sector* (how much per sector) → macro/regime + rotation budgets,
   NOT raw cross-sector score comparison.
5. **The optimizer's target weights are the primary exit mechanism.** Discretionary stops exist only
   for catalyst time-stops and speculative tail-stops.
6. **Forecast risk, not price.** The ML layer predicts calibrated probabilities/distributions
   (volatility, drawdown probability, regime transition) — never point price levels.
7. **Data is routed by layer and horizon.** Timing/regime data → macro/forecast layer.
   Demand/fundamental data → sector selection layer. Fast horizons use market-internal data; slow
   horizons use macro.
8. **Promotion gate = beat the simpler baseline out-of-sample, net of cost**, under walk-forward +
   lockbox. The ML regime forecaster must beat the existing rule-based MacroLayer regime, not beat
   nothing. Non-improving layers stay shadow-only.

## Target directory layout

```
Staging_Scalper_System/portfolio_layer/
  config.yaml                      # all paths rooted in Staging
  db/portfolio_layer.sqlite        # layer DB (scores, risk, weights, ledger); generated, gitignored
  core/        contracts.py, paths.py, logging_utils.py
  scores/      01_collect, 02_calibrate, 03_validate
  risk/        04_check_risk_readiness, 05_build_return_panel, 05c/05d liquidity,
               06_build_risk_coverage, 07_build_covariance_model, 08_validate_risk_panel
  costs/       12_build_trade_list, 13_build_cost_model, 14_apply_no_trade_bands, 15_validate_cost_model
  optimizer/   tier1_portfolio_optimizer.py, tier1_common.py, 09_run_portfolio_optimizer,
               10_validate_optimizer_outputs   (vendored)
  rotation/    rotation_timeseries.py, sector_rotation_selector.py, foreign_market_evaluator.py,
               17_build_rotation_signals, 18_validate_rotation_signals, 19_run_rotation_ablation_replay (clean)
  MacroLayer/  vendored macro engine + macro_raw.sqlite/macro_serving.sqlite (independent)
  macro/       Stage 6 adapter: 20_run_macro_serving, 21_build_macro_contract,
               22_validate_macro_contract
  research/    65_pit_snapshot_store, 66_define_calibration_targets           (Stage 11 infra)
  forecast/    67_train_models, 67_calibrate                                  (deferred; only if Stage 11 justifies ML)
  blacklitterman/  23_build_bl_inputs, 24_run_bl_optimizer, 25_apply_bl_cost_overlay,
                   26_validate_bl_fusion
  hedging/     75_run_hedging_overlay                                        (deferred; tested inside Stage 11)
  sleeves/     27_build_sleeve_framework, 28_apply_risk_budgets, 29_validate_sleeves
  ledger/      30_import_ib_activity_statement, 31_build_holdings_ledger,
               32_validate_holdings_ledger                                  (Stage 8.5)
  exits/       33_build_exit_signals, 34_apply_exits, 35_validate_exits
  payout/      14_build_payout_liability
  backtest/    15_backtest, 16_ablation_walkforward, 17_lockbox_ledger
  orchestration/ 18_run_pipeline, 19_risk_governor
  STAGE_GATES.md  README.md  output/
```

## Canonical contract: `stocks_scores` (the cross-sector currency)

Every sector pipeline emits this, identical schema, one as-of date per run:

| column | meaning |
|---|---|
| `as_of_date` | point-in-time date of the score |
| `ticker`, `sector`, `industry`, `industry_aggregate` | identity / taxonomy. `industry_aggregate` is the normalized grouping key; `industry` is descriptive and may carry cohort/subsector semantics by source pipeline. |
| `final_score` | **calibrated expected forward alpha** (excess vs sector benchmark), common units |
| `within_sector_percentile` | legacy field name; computed within `source_pipeline`/sleeve, not broad GICS sector |
| `rating` | `within_sector_percentile` bucket; useful for display/confidence context, not an absolute cross-sector exit trigger |
| `score_confidence` | calibration confidence / coverage haircut |
| `investable_eligible` | 0/1 **hard gate** carried from the sector's native portfolio candidate gate (med: `portfolio_candidate_gate`; tech: `rank_ready_flag`/`calibration_eligible_flag`; biotech: its own gate). The optimizer ranks/sizes **only eligible names** — never top raw score. |
| `eligibility_reason` | provenance for the gate decision (why eligible/excluded) |
| `source_pipeline`, `score_version` | provenance |

This is exactly what `tier1_portfolio_optimizer` already consumes — the join is a contract, not a code merge.

**Eligibility semantics (decided):** a sector hands the portfolio layer its *portfolio-candidate-eligible*
set, not its own final pick list. The sector vouches that a name is safe/eligible; cross-sector
*selection and sizing* is the portfolio layer's job. The eligible set is therefore a **superset** of
the sector's internal final list (e.g. med Tier 1 is expected to be contained in med
`portfolio_candidate_gate`). Stage 1 validates that the published gate is present and carried through; a
true containment check requires each sector to publish its internal final pick list as a separate artifact.
Headline score per sector: med = `portfolio_candidate_score` when present
(otherwise `composite_score`; IC tilt already baked in via `replace_raw`); tech family = `final_score`; `ic_tilted_composite_score`
and `safe_core_score` are audit-only and must never drive reranking.

---

## Stage 0 — Architecture & governance scaffold

**Goal:** an importable, isolated `portfolio_layer/` package with its own config, DB, and logging.

**Build:** package skeleton; `config.yaml` (all paths Staging-rooted); `core/paths.py`,
`core/logging_utils.py`; init layer SQLite with the foundation tables `runs` and
`data_quality_issues` only. Contract/score/risk tables are deferred to Stage 1, where their schema
is actually designed.

**Acceptance tests:**
- `portfolio_layer` imports without importing any sector package or anything under `PROD_Scalper_System`.
- Static scan finds **zero** references to `PROD_Scalper_System` paths anywhere in the package.
- `config.yaml` resolves DB, output, cache, and macro-serving paths, all under `Staging_Scalper_System/`.
- Layer DB initializes clean with `runs` and `data_quality_issues` tables present.

## Stage 1 — Canonical cross-sector score contract + calibration

**Goal:** every sector emits comparable, calibrated expected-alpha scores.

**Build:** `core/contracts.py` (schema + validator); `scores/01_collect_sector_scores.py` (unions each
sector's latest scored output); `scores/02_calibrate_cross_sector_scores.py` (maps each sector's
calibrated score onto a common expected-alpha scale via that sector's historical IC / realized-return
calibration); `scores/03_validate_score_contract.py`.

**Acceptance tests:**
- Each sector produces a `stocks_scores` file passing schema validation (all columns, types, no nulls in keys).
- All rows in a run share one `as_of_date`; a staleness gate flags any sector lagging beyond tolerance.
- `final_score` units are provisional expected-alpha units in Stage 1 hard-gate validation; per-sector
  realized forward returns increasing monotonically across `final_score` deciles (rank-IC > 0,
  sign-correct) is revalidated after the Stage 2 return panel exists.
- Cross-sector parity: equal `final_score` in two sectors implies statistically indistinguishable
  realized forward alpha (calibration-parity test passes within tolerance). This empirical gate is
  reported as deferred until Stage 2 return data exists; Stage 1 may only pass hard schema/PIT gates.
- `investable_eligible` is populated from each sector's native portfolio candidate gate; ineligible names
  are excluded from optimizer input. Stage 1 validates the published gate is present and populated; if a
  sector also publishes an internal final pick-list artifact, a later containment gate can assert that it is
  a subset of the published eligible set.
- Duplicate tickers across sectors are detected and resolved deterministically; any duplicate not covered by
  a canonical override is WARNed for curation rather than silently trusted.

## Stage 2 — Unified cross-sector risk panel (live/current book)

**Goal:** the covariance model used to *size today's book* — one model spanning all eligible tickers
across all sectors (real cross-sector correlations), not per-sector blocks.

**Design decision (decided):** self-sourced by the portfolio layer from ONE price source over the
**current** union universe. It does **not** reconcile price tables across the five sector DBs (an
avoidable N-way calendar/adjustment/FX/vintage problem), and it does **not** require delisted/survivorship
history. *"Prices are universal, scores are not"* — the layer consumes each sector's *scores* via the
Stage 1 contract, but sources *prices* itself. **Stage 2 is the live/current-book panel only; it is NOT
the survivorship-correct backtest panel** — that is a different product, built in **Stage 11**.

**Stage 2 must not rewrite Stage 1 eligibility.** `investable_eligible` (the sector's selection gate) is
read-only here. Stage 2 emits **separate** risk fields — `risk_eligible` (0/1), `risk_status`
(`direct` | `shrunk` | `excluded`), `risk_reason`. Selection eligibility and risk-data eligibility are
distinct: Stage 1 says "investable by sector rules"; Stage 2 says "has enough usable risk data, or needs
shrinkage/exclusion for optimizer risk sizing."

**Universe:** the **final `stocks_scores.csv`** (post-duplicate-resolution → one row per ticker, e.g.
BSX gets exactly one price series), restricted to `investable_eligible=1`, **plus** benchmarks / hedges /
rotation ETFs (SPY, QQQ, SMH, SOXX, XBI, sector SPDRs, foreign ETFs). Held-but-now-ineligible names are
added once a holdings ledger exists (Stage 8.5+).

**Config (`score_contract` peer block `risk_panel`):** `lookback_trading_days` (e.g. 504),
`min_direct_history_days`, `hard_floor_history_days`, `benchmark_tickers`, `sector_etf_map` (sector →
shrinkage-target ETF), `shrinkage_method` (`pairwise_linear`|`ledoit_wolf`|`oas`),
`covariance_frequency` (`daily`|`weekly`), `max_condition_number`, `max_missing_day_tolerance`,
`max_stale_price_trading_days`, `max_abs_return_for_covariance`, `price_provider`, `adjustment_policy`.

**Master calendar:** the **benchmark (SPY) trading calendar** is the US master calendar. Align all
tickers to it — do **not** require every ticker to have every row. Detect ticker-level gaps and
distinguish normal non-trading gaps from missing/suspended data. **Never forward-fill prices into fake
zero returns** except under an explicit, audited policy.

**Price reproducibility:** cache the pulled price snapshot with `provider`, `fetch_timestamp`,
`run_as_of`, `adjustment_policy`, `start/end dates`, and `sha256` per series/file — so the same as-of run
reproduces exactly even if the provider later revises adjusted prices.
The snapshot also seals provider split events in `split_events.csv`; validation must not call live
market-data APIs.

**Provider fallback:** Yahoo query1/query2 are the primary price sources. A secondary Stooq fallback may
fill symbol-specific Yahoo failures, but provider and source symbol must be recorded per ticker; fallback
usage never hides provenance.

**Corporate-action ticker aliases:** Stage 1 contract tickers stay stable for downstream joins, but
Stage 2 may use a configured active market-data symbol after an effective date (for example a rebrand or
ticker migration). `fetch_results.csv`, `price_snapshot.json`, and `risk_manifest.json` must record the
contract ticker, query symbol, provider source symbol, effective date, issuer id, and reason.

**Terminology:** with Yahoo adjusted close this is an **adjusted-close return panel** (name it that, not
"total return," unless dividend/split total-return behavior is explicitly verified).

**Build order:**
1. `risk_panel` config block.
2. `risk/04_check_risk_readiness.py` — readiness gate over **sealed Stage 1 artifacts**.
3. `risk/05_build_return_panel.py` — self-sourced adjusted-close panel (SPY master calendar, FX→USD) +
   price-snapshot cache with the provenance/hashes above.
4. `risk/06_build_risk_coverage.py` — thin-history classification → `risk_coverage.csv`.
5. `risk/07_build_covariance_model.py` — shrinkage + PSD fix + hierarchical clustering (reusing optimizer
   risk utilities).
6. `risk/08_validate_risk_panel.py` — acceptance gates + sealed manifest with hashes.

**Optional Stage 2.5 liquidity build:** `risk/05c_collect_ib_historical_spread_samples.py` and
`risk/05d_audit_liquidity_panel.py` are broker-dependent spread/liquidity steps, not prerequisites for
the core Stage 2 covariance panel. If their artifacts exist, `08_validate_risk_panel.py` validates and
hashes them; if they are absent, Stage 2 records a WARN and still accepts the risk panel. Stage 4/7 cost
overlays are the first stages that require those artifacts when `transaction_costs.spread_source` resolves
to `liquidity_panel`.

**Risk coverage artifact (`risk_coverage.csv`, one row per ticker):** `ticker`, `source_pipeline`,
`score_eligible` (= Stage 1 `investable_eligible`), `risk_status` (`direct`|`shrunk`|`excluded`),
`risk_eligible`, `observation_count`, `missing_day_count`, `missing_day_fraction`, `start_date`,
`end_date`, `right_edge_missing_day_count`, `shrinkage_target`, `risk_reason`.

**Acceptance tests:**
- All tickers aligned to the SPY master calendar; ticker-level gaps detected and classified
  (non-trading vs missing/suspended); no fake zero-return forward-fill outside the audited policy.
- Each date uses only data available by that date (**no future dates beyond run as-of**).
- Adjusted-price sanity: split adjustment applied; unexplained price-jump (un-adjusted split) detector
  passes. Dividend treatment is named, not assumed.
- **Thin-history hierarchy (no silent drops), written to separate risk fields:** (a) `risk_status=direct`
  when history ≥ `min_direct_history_days`; (b) `risk_status=shrunk` for partial history (recent IPO) →
  shrink toward the `sector_etf_map` target or cluster factor; (c) below `hard_floor_history_days`,
  `risk_status=excluded` + `risk_eligible=0` with a reason — **never** mutate `investable_eligible`.
- Right-edge freshness: a name whose last price is stale by more than `max_stale_price_trading_days`
  master-calendar rows is `risk_status=excluded` with `risk_reason=stale_right_edge:*`; current-book
  risk sizing must not use a stale terminal price.
- Benchmark/hedge/rotation ETFs present with full history.
- FX normalization to USD when non-USD names appear.
- Covariance matrix is symmetric **PSD** after the fix; **condition number** ≤ `max_condition_number`.
- Hierarchical clustering groups obviously-correlated names (e.g., multiple semis) into shared clusters
  on a known-correlated control set.
- Price-snapshot cache + manifest carry provenance and sha256 hashes; a re-run reproduces the panel.
- Split-event review is file-only from sealed `split_events.csv`; `data_quality_review.csv` is hashed in
  `risk_manifest.json`, and split-ratio-consistent suspected adjustment artifacts fail until explicitly
  quarantined or overridden.
- `returns_panel.csv` recomputes exactly from `prices_adjclose.csv` without forward-filling missing
  prices.
- Main `covariance.csv` is annualized so its units match annualized expected-alpha scores; period
  covariance may be emitted separately for audit.

**Readiness precondition — gate on the sealed Stage 1 run, not sector internals:**
`04_check_risk_readiness.py` verifies, for the target as-of: (1) `stocks_scores.csv` exists; (2) the
Stage 1 `manifest.json` `hard_gate_acceptance == PASS`; (3) the on-disk `stocks_scores.csv` sha256
matches the manifest hash (no stale/tampered downstream artifact); (4) source staleness within each
sector's configured tolerance (from manifest `per_sector` source dates); (5) every enabled sector is present
in the sealed manifest.
It **fails or warns naming the offending sector** according to `readiness_stale_status` and does
**not** re-run or deeply inspect sector pipelines. A separate convenience launcher MAY call the sectors'
own refresh scripts, but it lives **outside** the core portfolio layer.

## Stage 3 — Vendor optimizer + AQR-only baseline book

**Goal:** a working, risk-aware portfolio from AQR scores alone — the baseline and first shippable value.

**Optimizer universe policy (LOCKED):** the optimizer sizes only names with **`investable_eligible=1`
AND `risk_eligible=1`** (Stage 1 selection gate ∩ Stage 2 risk-data gate). Any name with
`risk_eligible=0` is **excluded from sizing** and surfaced in a `risk_excluded_candidates.csv`
(a.k.a. `held_no_risk_data`) report — selectable by score, but not sized until it has risk history.
**Do NOT** zero-weight risk-ineligible names inside the optimizer, and **do NOT** invent prior-hold
behavior — that waits for the holdings ledger (Stage 8.5+). This is the correct default pre-ledger.

**Universe (refined):** sized names are `investable_eligible=1 AND risk_eligible=1 AND role=scored AND
ticker ∈ covariance.csv`. Benchmarks/ETFs are diagnostics/replay context only, never optimization
candidates. `risk_eligible=0` eligible names → `risk_excluded_candidates.csv` (may be empty).

**Covariance (unambiguous):** Stage 3 **injects the Stage 2 annualized `risk/covariance.csv` directly**;
it does NOT let vendored tier1 rebuild covariance from prices. `prices_adjclose.csv`/`returns_panel.csv`
are used only for the report-only replay.

**Expected returns:** `mu_used = final_score * score_confidence` (both annualized; configurable).

**Build (implemented):** `tier1_portfolio_optimizer.py` + `tier1_common.py` vendored into `optimizer/`
(re-rooted, independence-gated) for later BL use; the Stage 3 baseline uses a thin long-only
mean-variance solver `optimizer/optimizer_core.py` (cvxpy, injected Σ). Scripts:
`optimizer/09_run_portfolio_optimizer.py` (book + `risk_excluded_candidates.csv` + `optimizer_meta.json`),
`optimizer/10_validate_optimizer_outputs.py` (gates + provenance-sealed `optimizer_manifest.json`),
`backtest/11_run_static_baseline_replay.py` (**static trailing replay diagnostic — NOT an OOS backtest**;
true walk-forward baseline is Stage 11).

**Acceptance tests:**
- Optimizer universe == `{investable_eligible=1} ∩ {risk_eligible=1} ∩ {role=scored} ∩ {in covariance}`;
  every excluded eligible name in `risk_excluded_candidates.csv` and **nowhere** in the weight vector.
- Weights valid: long-only, `≤ max_weight_per_name`, sum to `gross_exposure` (within tolerance).
- **Positive `mu_used`↔active-weight relationship** (Spearman > 0); negative-α holdings allowed as
  diversifiers but reported (NOT a monotonic-weight gate — covariance/caps break monotonicity).
- Covariance injected from Stage 2 (meta `covariance_sha256` matches the Stage 2 artifact; never rebuilt).
- Solver returns optimal; solver name/status/objective/attempts recorded.
- Manifest hashes scores, coverage, covariance, Stage 1 & Stage 2 manifests, optimizer config, and the
  vendored optimizer source — fully reproducible.
- Static replay is labeled lookahead/diagnostic; metrics are context, **not** a promotion baseline.

## Stage 4 — Transaction-cost & turnover overlay (flat commission, AUM-aware)

**Goal:** a trade-list + cost + cash-residual overlay over the Stage 3 book, so the baseline is reported
**net of realistic cost**. **Stage 3 is unchanged** — Stage 4 is an execution/cost layer on top.

**Decisive fact — flat per-order commission ⇒ cost is dollar-denominated and AUM-dependent.** Commission
is **$1.00–$1.25 per order**, where **one name · one side = one order** (buy GMED = 1, sell GMED = 1,
round-trip = 2). At a given AUM the commission as a fraction of a trade depends on the trade's dollar
size, so AUM is a **required** input.

**Required inputs (no silent defaults):** `--aum` / config `transaction_costs.aum_usd` (= 300,000);
commission `{low 1.00, base 1.125, worst_case 1.25}` (base for the reported/sealed number, **worst_case
for conservative trade/no-trade decisions**); `rebalance_horizon_days` (default 21; sensitivity at 10/63);
optional `--prior-weights` (default = **cash**, so a first build is one-way buys only).

**Cost model (per traded name, per side):**
```
trade_cost_$ = commission_per_order                          # flat, exact
             + half_spread_bps × |trade_notional|             # provisional config default until bid/ask exists
             + impact_bps(|trade_notional|, ADV) × |trade_notional|   # "none" until volume is fetched (deferred)
cost_weight_drag = Σ trade_cost_$ / AUM
```
Commission = **exact**. Spread has two modes:
- Default: `transaction_costs.half_spread_bps_default` (currently 5 bps), no broker dependency.
- Enhanced: `liquidity_panel.enhanced_intraday_enabled=true` collects IBKR historical BID_ASK 5-minute
  bars in the overnight run, samples configured intraday times, writes `risk/ib_spread_samples.csv`,
  `risk/spread_snapshot.csv`, and `risk/spread_snapshot_meta.json`, and upserts them into the
  portfolio-owned SQLite DB. Stage 4 then consumes the sealed per-ticker half-spread snapshot. If the
  exact as-of day is missing, the panel uses the latest available sample within
  `max_stale_liquidity_days`; only tickers with no valid recent sample fall back to the configured 5 bps.

ADV/impact remains a deferred refinement (`impact_model: none` until volume/impact data exists).

**One-way vs round-trip (explicit):** first build from cash → **one-way buy cost only**; rebalance →
one-way cost of each executed buy/sell delta; **round-trip cost is a diagnostic, never the default deducted
from the current run.**

**Refinement A — utility-aware (covariance-aware) no-trade bands (rebalance only, deferred by default).**
Because Stage 1 score-to-alpha magnitudes are provisional, Stage 4 must **not** suppress trades using
`mu_used` unless `transaction_costs.enable_provisional_mu_no_trade=true` is explicitly set for research.
The production default is cost-only execution/review until Stage 11 calibrates score snapshots to realized
forward returns. Once calibrated, compare the **period** utility gain to the one-time cost with
`k = rebalance_horizon_days / 252`:
```
utility_period(w) = k · [ mu'w − 0.5·gamma·w'Σw ]
execute a rebalance trade only if  utility_period(after) − utility_period(before) > cost_weight_drag + buffer
```
The calibrated no-trade band therefore consumes `target_weights.csv`, prior/current weights,
realized-return-calibrated expected returns, Stage 2/11 covariance, and the AUM/cost config — **not alpha alone**.

**Refinement B — AUM-aware minimum economic position filter (applies even on the first build).** Stage 3's
`min_weight_to_hold` (5 bps) is deliberately AUM-blind; Stage 4 asks "is the position big enough to justify
a flat order fee?" `position_notional = target_weight × AUM`; drop a position to **CASH** when the
commission is too large a fraction of it. Distinct from rebalance no-trade bands; both route residual to CASH.

**CASH handling (required):** suppressed/dropped weight goes to an explicit **CASH** line in
`cost_adjusted_target_weights.csv`. Gate: `sum(asset_weights) + cash_weight == gross_exposure`.

**Build (renumbered after Stage 3's 09/10/11):**
- `costs/12_build_trade_list.py` — prior (cash default) vs target → trades (ticker, prior_w, target_w,
  delta_w, trade_notional, side, n_orders).
- Optional when enhanced spread is enabled: `risk/05c_collect_ib_historical_spread_samples.py` after
  the risk panel is built. Its universe is explicit via `liquidity_panel.universe_source`; the project
  default is `risk_eligible_scores`, so it samples the full scored/risk-eligible portfolio universe.
- `risk/05d_audit_liquidity_panel.py` enriches the spread snapshot with score/risk/optimizer/trade context
  and writes `liquidity_audit.csv`, `liquidity_audit_by_sector.csv`, and `liquidity_audit_summary.json`.
  Extreme spreads are surfaced as WARN/review items unless they exceed the hard data-quality fail threshold.
  Do **not** compare spreads to `final_score`/`mu_used` here; score-to-alpha magnitudes are provisional
  until Stage 11 maps score snapshots to realized forward returns.
- `costs/13_build_cost_model.py` — trades + AUM + commission/spread/impact → `cost_report.csv` (per-name
  commission/spread/impact/total_$ + cost_bps) + totals.
- `costs/14_apply_no_trade_bands.py` — min-economic-position filter (first build) + utility-aware no-trade
  suppression (rebalance) + CASH residual → `cost_adjusted_target_weights.csv`.
- `costs/15_validate_cost_model.py` — gates + provenance-sealed `cost_manifest.json`.
- `backtest/16_run_net_static_replay.py` — net-of-(one-way)-cost replay; **still diagnostic/in-sample**.

**Acceptance tests:**
- AUM is required (run fails without `--aum`/config); commission applied as **flat $/order**, not bps.
- `sum(asset_weights) + cash_weight == gross_exposure`.
- **Both gross and net reported**, and **commission-in-bps scales inversely with AUM** (the flat-fee
  signature) — NOT "the gap is non-trivial" (at high AUM the gap is correctly trivial).
- One-way cost used for current execution; round-trip reported only as a diagnostic.
- No-trade bands (rebalance) measurably cut turnover with the utility>cost rule; sensitivity at 10/21/63 days.
- Cost matches a hand-calc on a sample (e.g., 34 buys × $1.125 = $38.25).
- `cost_manifest.json` hashes AUM, commission/horizon assumptions, `target_weights.csv`, `covariance.csv`,
  `stocks_scores.csv`, and the Stage 3 `optimizer_manifest.json` (which must be acceptance=PASS, hash-matched).
- Net replay labeled lookahead/diagnostic — the official OOS net baseline is **Stage 11**.

## Stage 5 — Tactical rotation sleeve (SHADOW-ONLY) — IMPLEMENTED

**Goal:** the fast (1–2 wk) money-flow tilt across sector ETFs (and foreign ETFs, budget held at 0). The
first time-series/regime overlay over the AQR book. **Build order ≠ authority order:** rotation is built
first (self-contained on the sealed Stage 2 panel, zero external deps), but in the final fusion (Stage 7)
it is a **bounded tilt *under* the macro governor**, never above it.

**Hard rule — shadow-only until Stage 7/Stage 11.** Stage 5 generates + validates signals and runs a
diagnostic ablation. It **does not touch the live book**: `rotation.enabled_in_production: false`, Stage 3
`target_weights.csv` and Stage 4 cost-adjusted book remain byte-identical, and the stage writes only under
`rotation/`. Promotion (enabling the tilt in production) waits for the Stage 7 BL fusion + the Stage 11 OOS
walk-forward — never on the in-sample ablation here.

**Build (clean re-implementation, NOT vendored from PROD).** PROD's rotation source is unavailable and
PROD is off-limits (independence #2), so the logic is re-implemented from the optimizer's known contract +
documented design. Modules (pure, deterministic, PIT): `rotation/rotation_timeseries.py` (vol-normalized
multi-horizon momentum + absolute-trend state), `rotation/sector_rotation_selector.py`,
`rotation/foreign_market_evaluator.py`. Scripts: `rotation/17_build_rotation_signals.py`,
`rotation/18_validate_rotation_signals.py`, `rotation/19_run_rotation_ablation_replay.py`.

**Inputs:** only the sealed Stage 2 panel (`risk/prices_adjclose.csv`, `returns_panel.csv`) — no new
fetch, fully PIT — plus `risk_panel.sector_etf_map` and the `rotation:` config block.

**Optimizer contract (verified against `optimizer/tier1_portfolio_optimizer.py`):**
- Sector file requires `SectorName, ScorePct, State`. **`SectorName` is a join key** — the optimizer maps
  each stock's sector → `SectorName` to attach `ScorePct`/`State`. Our 5 sleeves collapse to only 2 GICS
  sectors, so **`SectorName` = `source_pipeline`** (the sleeve), and **Stage 7 must join stocks on
  `source_pipeline`** (`cfg.sector.stock_to_sectorname`). Sector `State ∈ {Positive, Neutral, Negative}`
  (the optimizer's default multiplier keys — other tokens are silently ignored).
- Foreign file requires `Ticker, MarketName, Score, ScorePct, State`; `Score` = raw composite,
  `ScorePct` = percentile; foreign `State ∈ {Eligible, Avoid}`.
- Stage 5 emits **both** a canonical audit CSV (snake_case) and an exact-contract `*_optimizer.csv`.

**Tilt (ablation only):** bounded multiplicative scaling of a sleeve's aggregate weight,
`mult = clip(f(ScorePct), [0.7, 1.3])`, capped at 1.0 when the absolute-trend gate fails (never tilt
*into* a downtrend); renormalize to gross and re-impose the Stage 3 `max_weight_per_name`. Long-only and
gross preserved; a tilt can never create a short. The `max_sector_budget_shift` cap is validated on the
realized post-projection book; it is not assumed to follow analytically from the multiplier bounds.

**Acceptance gates (hard — all PASS, WARN non-blocking):**
1. **independence_no_prod_ref** — no PROD path in rotation logic/scripts (Stage 0 AST gate is authoritative).
2. **pit_no_lookahead** — panel right edge ≤ as_of and matches the sealed meta.
3. **optimizer_contract_schema** — `*_optimizer.csv` exact columns; `State` ∈ enum; `ScorePct ∈ [0,100]`,
   `Score` numeric.
4. **sectorname_mapping_bijective** — `SectorName` set == `source_pipeline` set in `stocks_scores.csv`,
   1:1 with `sector_etf_map`, no dup/missing.
5. **etf_panel_coverage** — every rotation ETF present in the Stage 2 panel with ≥ `ma_days+slope` history
   and a non-stale right edge.
6. **bounded_tilt** — multiplier ∈ `[mult_min, mult_max]`; downtrend capped at 1.0; realized
   post-projection sector-budget shift ≤ `max_sector_budget_shift`.
7. **canonical_optimizer_consistent** — optimizer rows mirror canonical; `State` derivation correct
   (downtrend ⇒ Negative).
8. **foreign_budget_zero** — `foreign.applied_budget == 0` (locked until Stage 6).
9. **deterministic_rebuild** — rebuilding sector and foreign signals from the same sealed panel + config
   reproduces the sealed artifacts.
10. **shadow_only_non_destructive** — production disabled; Stage 3 `target_weights.csv` + Stage 4 adjusted
    book still match their sealed hashes (rotation changed nothing).
11. **rotation_artifacts_reproducible** — meta file hashes match disk; manifest sealed.

**Reported diagnostic (WARN-only, never gates/promotes):** `19_run_rotation_ablation_replay.py` — AQR-only
vs AQR+rotation, net of per-name cost over a trailing window. It recomputes the one-way establishment cost
for the exact Stage 3 raw `target_weights.csv` book being replayed, then charges the *incremental* turnover
(AQR→tilted). Records net Sharpe of both, the delta, turnover, establishment-cost bps, and incremental-cost
bps. WARN if net Sharpe degrades beyond tolerance. Single-snapshot/lookahead by construction — the real OOS
promotion test is Stage 11.

## Stage 6 - MacroLayer contract adapter

**Goal:** expose the vendored MacroLayer regime/fit engine to the portfolio layer through a sealed,
portfolio-native contract. MacroLayer remains an independent Staging-owned data engine under
`portfolio_layer/MacroLayer`; `portfolio_layer/macro/` is the adapter boundary.

**Design decision:** Stage 6 does **not** let MacroLayer overwrite `stocks_scores.csv`, Stage 3 optimizer
inputs, or any live book artifact. Macro data flows forward only as sealed artifacts under
`runs/<as_of>/macro/`, consumed later by Stage 7.

**Taxonomy rule:** MacroLayer's native sector tables use the broad Yahoo/GICS-like taxonomy. The portfolio
join key is the five-sleeve `source_pipeline` domain from Stage 1:
`biotech`, `med_devices`, `semiconductors`, `software_infrastructure`, `technology_hardware`.
Stage 6 maps MacroLayer industry/aggregate/sector fits to those sleeves through
`macro.sleeve_taxonomy`, with an explicit fallback ladder:
`industry -> industry_aggregate -> macro_sector_fallback`. Every fallback is recorded.

**PIT rule:** every MacroLayer table is queried as `MAX(as_of_date) <= run_as_of`. The adapter may use a
newer MacroLayer DB seed, but it must never consume rows after the portfolio run's as-of date.

**Build:**
1. `MacroLayer/00_validate_macro_layer_foundation.py` - foundation check for the vendored engine.
2. `macro/20_run_macro_serving.py` - optional convenience runner for the vendored serving DAG; always
   passes `--skip-final-optimizer` so legacy MacroLayer optimizer integration cannot write portfolio
   artifacts.
3. `macro/21_build_macro_contract.py` - read-only serving-DB adapter that emits:
   `macro_regime.csv`, `macro_sector_fit.csv`, `macro_stock_overlay.csv`, `macro_country_fit.csv`,
   `macro_foreign_budget.csv`, `macro_foreign_candidates.csv`, and `macro_contract_meta.json`.
4. `macro/22_validate_macro_contract.py` - acceptance gates + sealed `macro_manifest.json`.

**Acceptance tests:**
- **independence_shadow_only** - macro wrapper has no PROD path token; `macro.enabled_in_production=false`.
- **macro_contract_schema** - all Stage 6 CSVs expose exact schemas expected by Stage 7.
- **stage1_contract_unchanged** - `stocks_scores.csv` still matches the sealed Stage 1 manifest and the
  macro build metadata pins the same hash.
- **pit_no_future_macro_dates** - all row-level `macro_as_of_date` values and meta source dates are
  `<= run_as_of`.
- **macro_freshness_within_tolerance** - regime, country, sector/industry/aggregate, stock overlay, and
  foreign budget/candidate data are within configured staleness tolerances.
- **sleeve_taxonomy_matches_scores** - `macro_sector_fit.source_pipeline` exactly equals the Stage 1
  source_pipeline set; no broad-sector joins.
- **stock_overlay_coverage_and_fallback** - every Stage 1 ticker appears exactly once; fallback fraction
  is bounded separately for all names and investable-eligible names.
- **stage7_contract_surface** - sector target weights + macro fits + foreign budget fields are numeric and
  the sleeve target weights sum to 1.
- **macro_meta_reproducible** - metadata hashes current inputs (including `macro_serving.sqlite`),
  sources, and CSV artifacts.
- **no_legacy_macro_optimizer_outputs** - Stage 6 writes only `runs/<as_of>/macro/` artifacts; legacy
  MacroLayer optimizer outputs are absent from the portfolio run.

Known-stress regime checks (2008 GFC, 2020 COVID, 2022 rate-hike) remain MacroLayer engine diagnostics;
they are not a substitute for the Stage 6 contract gates.

## Deferred Research Modules 6.5-6.7 - Folded Into Stage 11

These are **not prerequisites** for Stages 7-8 and should not be built as standalone forecast layers now.
The immediate bottleneck is empirical calibration of the existing `final_score` contract, not ML. The useful
parts of 6.5/6.6 become **Stage 11 research infrastructure**:

- **6.5 narrowed:** build a PIT score/snapshot store, not a broad feature store. Required history:
  `as_of_date`, `ticker`, `source_pipeline`, `native_score`, `final_score`, `rating`, `score_confidence`,
  eligibility/risk status, sector/sleeve membership, regime label, rotation state, and sealed source hashes.
- **6.6 narrowed:** define only the calibration targets Stage 11 needs first:
  `forward_return_21d`, `forward_return_63d`, `forward_return_126d`,
  `forward_excess_return_vs_sector`, `drawdown_next_63d`, and `regime_at_snapshot`.
- **6.7 deferred:** ML forecasting / calibrated probability models are built only after Stage 11 proves the
  score snapshots have stable realized payoff and a rule-based OOS baseline exists. The model must beat the
  existing MacroLayer / BL / sleeve stack out-of-sample, net of cost, or it remains shadow-only.

The broader feature-routing ideas remain valid later: macro/timing data belongs in the macro/forecast layer;
sector-demand/fundamental data belongs in sector pipelines. But new connectors and ML forecasts should wait
until Stage 11 has enough PIT history to test them without overfitting.

## Stage 7 — Black-Litterman fusion: macro views + sector budgets (SHADOW-ONLY adapter)

**Goal:** fuse everything built so far into one book — AQR calibrated alpha (Stage 1) as BL **views**,
macro regime/sector fit (Stage 6) as the **governor** (gross scaling + sector budgets), rotation State
(Stage 5) as a **bounded tilt within** those budgets, on the injected Stage 2 covariance, then net of the
Stage 4 cost model. **Stage 7 is an explicit sealed adapter/fusion layer, not a loose call into the
vendored optimizer.** It stays **shadow-only**; promotion to the live book waits for Stage 11 OOS.

**Current implementation correction:** Stage 7 injects the sealed Stage 2 covariance into tier1, de-annualized
to tier1's return period, and uses the sealed Stage 2 price panel only as a run-local diagnostic/input
artifact. `23_build_bl_inputs.py` generates a
tier1-native `stocks_scores_csv`, a ticker-level `bl_benchmark_weights.csv`, and a run-local sealed tier1
config. Annual alpha views are consumed through an explicit absolute-alpha mode; they are not z-scored and
rescaled. Sleeve-level sector budgets start from `strategic_sector_weights` by default, not Stage 3 realized
weights; ticker-level benchmark weights are neutral/equal inside each sleeve by default, with Stage 3
distribution available only as an explicit config option. Macro sector shifts use absolute and relative caps
plus a small sleeve floor, so tiny sleeves are not
silently eliminated by a single adverse macro score.

**Authority hierarchy realized in tier1's own machinery** (verified against `tier1_portfolio_optimizer`):
macro = the **prior/budget** → `benchmark_sector_weights` drives `π = δ·Σ·w_bench`; rotation = the
**bounded tilt** → `SectorState` × `sector_state_alpha_multipliers`; AQR alpha = per-name **absolute view**
(`P=I`, `q = π + alpha`, `μ_BL = posterior(π,Σ,P,q,Ω,τ)`).

**UNITS — locked option B (annualized).** BL views are **annualized return units**, matching the Stage 2
**annualized** covariance. `final_score` (already annual-return decimals, observed range ≈ [−0.20, +0.10])
is the only thing that becomes an expected-return view. `rating`, `score_confidence`, `ScorePct`, rotation
`State`, and macro regime are **NOT returns** — they only adjust **confidence (Ω), gross exposure, sector
budgets, or alpha multipliers**. The generated tier1 config derives `returns.frequency` from the Stage 2
covariance metadata and de-annualizes both annual covariance and annual alpha into that same optimizer
period. A hard gate asserts `covariance_meta.annualization_factor == periods_per_year(returns.frequency)`.
The view path must preserve the calibrated alpha (no z-score-and-rescale that discards magnitude).

**Adapter mapping (sealed inputs → tier1 hooks):**
| Sealed source | tier1 hook | Role |
|---|---|---|
| Stage 1 `final_score` | `SignalScore` (annual alpha view) | per-name absolute view |
| Stage 1 `rating` | `confidence_by_rating` → Ω | view confidence |
| Stage 6 sector budget = **Stage 3 sleeve wt + bounded macro_fit shift** | sector-targets CSV → `benchmark_sector_weights` → π | sector budget (governor) |
| Stage 5 rotation `State` | `SectorState` → `sector_state_alpha_multipliers` | bounded sector tilt |
| Stage 6 `regime` | `gross_exposure × regime_scalar` | gross scaling |
| Stage 6 foreign budget (`active_flag`-gated) | `region_budgets.FOREIGN.{min,max}` | foreign cap |
| Stage 6 `stock_overlay` | **diagnostic only** (≤ small conf haircut) | cautious (33–36% fallback) |
| Stage 2 `covariance` | injected Σ | risk |

Sector budget = `renorm(clip(stage3_sleeve_wt + bounded_shift(macro_fit_z), ±max_sector_shift))` —
**Stage 6 `target_weight` is the neutral Stage-3 baseline, not a macro-optimized allocation**; macro shifts
it within a cap. Foreign honors `active_flag` (currently 0 → stays 0).

**Build — 4 sealed scripts (`blacklitterman/`):**
- `23_build_bl_inputs.py` — adapter: from sealed Stages 1/2/3/5/6 emit `bl_views.csv`,
  `bl_sector_targets_optimizer.csv`, `bl_foreign_budget_optimizer.csv`, and a **generated, sealed**
  `bl_optimizer_config.yaml` (tier1 config whose `macro_optimizer_integration.inputs` point **only at
  run-local sealed files** — never `MacroLayer/macro_serving.sqlite`, never PROD) + `bl_inputs_meta.json`.
  Includes **contract-probe + pre-solve feasibility checks** (below). **First build stops here for review.**
- `24_run_bl_optimizer.py` — run vendored tier1 on the sealed adapter config + injected Σ →
  `bl_target_weights.csv`, `bl_optimizer_summary.csv`, `bl_optimizer_meta.json`, and a hard validation CSV
  that verifies realized `cash_weight` / `risky_gross_exposure` match the macro-regime budget.
- `25_apply_bl_cost_overlay.py` — rerun the Stage 4 cost model + latest liquidity snapshot against the fused
  book → namespaced `bl_cost_adjusted_target_weights.csv` (baseline Stage 4 untouched).
- `26_validate_bl_fusion.py` — gates + sealed `bl_manifest.json`.

**Contract-probe + feasibility checks (in `23`, before any solve):**
- Upstream manifests (1,2,3,5,6 + liquidity if used) `acceptance==PASS` and hash-match.
- `sector_name` join key == `source_pipeline` set (bijective); every view ticker ∈ covariance index;
  covariance universe aligns with the optimization universe exactly.
- Optimizer input columns match tier1 loaders (sector targets `{sector_name,target_weight}`; foreign
  `{Ticker,MarketName,Score,ScorePct,State}`; views carry SignalScore/Rating/SectorState).
- Sector budgets sum to gross; FOREIGN ⊆ gross; per-name caps can satisfy each budget
  (`budget_s ≤ n_s·max_weight`); no risk-ineligible name required by a budget.
- Generated config references only run-local sealed files (scan: no MacroLayer DB, no PROD, no abs paths).
- Units: `final_score` finite annual decimals; `covariance_units=="annualized"`; Stage 2 annualization factor
  matches the generated tier1 return frequency.

**Acceptance gates (hard; WARN non-blocking):** upstream-sealed-&-current · no-direct-MacroLayer-DB-read ·
independence/PIT · join-completeness + covariance alignment · BL sanity (views finite, Ω positive &
bounded, no-views ⇒ posterior recovers π, **units annualized**) · hierarchy (sector exposures within macro
budgets, rotation ⊆ budget, FOREIGN ≤ country-fit budget, `gross==base×regime_scalar`) · conservation
(long-only, Σw=gross, caps) · cost-adjusted book reproducible · **baseline Stage 3/4 byte-unchanged** ·
`enabled_in_production:false` · determinism/provenance.

**Reported diagnostic (WARN-only, never promotes):** fused book vs Stage 3 AQR-only, net of Stage 4 cost.
Promotion to the live/default book is **deferred to Stage 11** (OOS walk-forward + lockbox). First test
case = the fully sealed **2026-06-18** run.

**Config:** `black_litterman_fusion:` block pins `tau`, `delta`, `return_space`, alpha units policy
(B), `confidence_by_rating`/min/max/boost, `sector_state_alpha_multipliers`, `regime_to_gross_scalar` map,
`macro_sector_max_shift`, `foreign_activation_policy`, `cost_overlay_namespace`, `enabled_in_production:false`.

## Deferred Module 7.5 - Hedging / Actuation Overlay

Hedging is **not needed before Stage 11**. The base fused/sleeved book should first prove its OOS behavior
without adding another moving part. Stage 7.5 remains a candidate module tested inside Stage 11 after the
walk-forward harness exists.

Candidate actions: gross-exposure reduction, index put protection, inverse/defensive ETFs, or defensive
rotation. Any hedge must be costed explicitly and compared against the unhedged Stage 7/8 book net of hedge
cost. If it does not improve OOS tail metrics without unacceptable return drag, it remains shadow-only.

## Stage 8 — Multi-horizon sleeves + risk-allocation engine (SHADOW-ONLY)

**Goal:** take the *sealed* Stage 7 cost-adjusted fused book and re-allocate its **risk** (not capital) —
factor-neutralized, diversified into many low-correlation bets, with risk placed where the information
ratio is highest — partitioned into horizon sleeves with regime-conditional risk budgets and a drawdown
throttle. It is a **risk-allocation engine, not risk accounting**: the Rentech part is *where* the risk
goes, not just measuring it. **Shadow-only**: it emits a *proposal*, never mutates the Stage 7 book.

**Where it sits:** consumes the sealed Stage 7 manifest + `blacklitterman/costs/bl_cost_adjusted_target_weights.csv`
(hash-verified, not just read), joined to sealed metadata (`bl_target_weights.csv` SectorName/Rating;
`stocks_scores.csv` `final_score`/`score_confidence`/`source_pipeline`) and the Stage 2 **annualized**
`covariance.csv`. It does **not** select new names and introduces no new tickers.

**Risk model (all from the sealed Σ — no new regression):** `covariance.csv` is 419×419 and already
contains the **SPY + 5 sleeve ETFs**, so a multi-factor model falls straight out of Σ:
`Ω_f = Σ[F,F]`, betas `B = Σ[A,F]·Ω_f⁻¹`, systematic `BΩ_fB'`, idiosyncratic `D = diag(Σ[A,A] − BΩ_fB')`.
- **Per-name risk contribution** `RCᵢ = wᵢ(Σw)ᵢ / w'Σw` (non-CASH only; CASH excluded from Σ/RC).
- **Factor risk decomposition** → systematic vs idiosyncratic share + per-factor (market, each sleeve) share.
- **Effective number of bets (Meucci/PCA):** `Σ=EΛE'`, `v=E'w`, `p_k=v_k²λ_k/(w'Σw)`, **ENB=exp(−Σ p_k ln p_k)**.

**Rentech-style allocation principles (what makes it more than vol budgeting):**
1. **Factor-neutralized:** keep risk **idiosyncratic-dominated** — cap market-beta and each sector-factor
   risk share; the book is paid for *stock selection*, not unintended factor beta.
2. **Risk where the edge is (Euler/IR):** at a risk-efficient book marginal risk ∝ marginal alpha; within a
   sleeve the neutral is **equal risk contribution (risk parity), tilted by IR = `final_scoreᵢ/σᵢ`**;
   positions paying risk without alpha justification are flagged/trimmed.
3. **Diversification as an objective:** gate on **ENB ≥ floor** — many small, low-correlation bets, not a
   few big ones that weight caps alone would miss.
4. **Cross-sleeve joint risk:** budget against the **full joint Σ** (sleeves are correlated; total risk ≠ Σ
   of sleeve risks).
5. **Regime-conditional budgets:** sleeve risk budgets are a function of the Stage 6 regime (risk-off cuts
   the speculative/`medium_rotation` budget, raises `long_core`).
6. **Continuous drawdown/Kelly throttle:** `scale = clip(1 − dd/dd_limit, 0, 1)·(σ_target/σ_realized)` —
   **Phase 1 = simulated/diagnostic only** (no persisted state); Phase 2 persists `sleeve_state`.
7. **Cost/capacity aware:** higher-turnover sleeves penalized by Stage-4 cost; **ADV/capacity caps are the
   one genuinely deferred piece** (no volume data) — flagged, never faked.

**Sleeves (PIT, exactly one per held name, auditable reason):**
- `short_catalyst` (1–3 mo) — **DISABLED in Phase 1**; requires the formal event contract
  `events/catalyst_events.csv` (`ticker,event_type,event_date,event_asof_date,source_pipeline,confidence,
  source_artifact,source_sha256`; gate `event_asof_date ≤ run_as_of`). Absent ⇒ **WARN + disable**, never
  `final_score`-faked.
- `medium_rotation` — held names whose sleeve has Stage 5 rotation `State==Positive`.
- `long_core` — remaining names, driven by `final_score`.

**Phase-1 gross policy:** Stage 8 **re-allocates risk composition without increasing Stage-7 gross**. If a
realized risk-contribution cap binds, the excess weight is trimmed to CASH (de-risking); Stage 8 does **not**
scale gross upward or set a live `σ_target` (Stage 6/7 own gross via regime). Explicit vol-targeting is Phase 2.

**Build (`sleeves/`, clean):**
- `sleeves/risk_model.py` — RC, Σ-based factor decomposition, ENB, betas, IR, realized RC-cap trimming,
  sleeve feasibility bounds (pure functions).
- `27_build_sleeve_framework.py` — sleeve assignments + risk diagnostics (RC/factor/ENB) on the Stage 7
  book → `sleeve_assignments.csv` + `risk_model_meta.json`.
- `28_apply_risk_budgets.py` — IR-tilted risk-parity within sleeve + regime-conditional sleeve budgets,
  iterative **scale→project**, realized RC-cap enforcement by deterministic trim-to-CASH, and feasible
  sleeve-risk diagnostics (capped-simplex, long-only, per-name + sleeve caps, residual→CASH; fail closed
  on unsafe/infeasible books) → `sleeve_adjusted_target_weights.csv`, `sleeve_risk_budget.csv`,
  `factor_risk_decomposition.csv`, `effective_bets.json`.
- `29_validate_sleeves.py` — gates + sealed `sleeve_manifest.json`.
- `30_run_sleeve_ablation_replay.py` (optional, WARN-only diagnostic).

**Acceptance gates (hard; WARN non-blocking):**
- **Stage 7 sealed & current** — `bl_manifest.json` acceptance PASS + cost-adjusted book hash verified;
  **no new tickers**; every held non-CASH ticker ∈ covariance; **cov hash == sealed Stage 2**.
- **Partition** — each held name → exactly one sleeve, complete + disjoint, auditable reason.
- **RC guards** — `w'Σw > eps`, no NaN/inf RC, realized per-name risk-contribution cap enforced; any cap
  trim moves to CASH, never to another risky name; concentration probe (inject one huge alpha) is capped.
- **Sleeve budgets** — realized sleeve RC shares must be within band of the **feasible clipped** regime
  budget (`clip(raw_budget, feasible_min, feasible_max)`); distance from the raw aspirational budget is
  reported as WARN-only when caps/universe concentration make it unreachable.
- **Factor-neutral** — market-beta and each sector-factor share ≤ caps as hard gates; idiosyncratic share floor
  is WARN-only in Phase 1 because the upstream two-GICS book limits what reweighting can achieve.
- **Diversification** — `ENB` must not worsen as a hard gate; absolute `ENB ≥ floor` is WARN-only in Phase 1.
- **IR consistency** — risk concentrated where IR is highest (no large un-alpha'd RC outliers; WARN if borderline).
- **Conservation / no-add-risk** — weights (incl. CASH) sum to 1; **risky gross ≤ Stage-7 cost-adjusted
  risky gross**; **cash ≥ Stage-7 cash** unless explicitly overridden; exact risky-gross equality is not
  required when RC-cap trimming de-risks the book; long-only; per-name + sleeve caps.
- **Shadow-only / non-destructive** — Stage 7 (and Stage 3/4) artifacts byte-unchanged;
  `enabled_in_production:false`; writes only under `runs/<as_of>/sleeves/`.
- **Determinism / provenance** — sealed manifest hashing Σ, Stage 7 inputs, config, sources.

**Reported diagnostic (WARN-only, never promotes):** sleeve-adjusted vs Stage 7 fused, net of cost.
Promotion (re-cost the proposal / go live) waits for Stage 11 OOS.

**Phase 1 (now):** `long_core` + `medium_rotation`, factor/ENB/IR/regime budgets, simulated throttle,
shadow proposal — no Stage 7 mutation, no cost-overlay invalidation. **Phase 2:** the catalyst event
surface + horizon-matched short-window Σ for it; persistent drawdown state; ADV/capacity caps; cost overlay
on a promoted proposal.

## Stage 8.5 — Holdings ledger + broker statement ingestion

**Goal:** make the portfolio layer stateful before exits/payouts. The raw source is a sealed IB Activity
Statement CSV saved under `IB_reports/`; the core pipeline does **not** connect to live IB. One-day and
date-range reports are both valid inputs, so missed daily runs can be caught up with a wider statement.

**Build:** `ledger/30_import_ib_activity_statement.py` parses the IB multi-section CSV into normalized
run-local CSVs; `ledger/31_build_holdings_ledger.py` loads the artifacts into the portfolio-owned SQLite
DB and builds `holding_lots.csv` + `holding_state.csv`; `ledger/32_validate_holdings_ledger.py` seals
`ledger_manifest.json`.

**Tables:** broker statement sources, open positions, net stock positions, trades/fills, instruments,
cash report, dividends, cash movements, fees, securities lending, holdings lots, current holding state,
and reconciliation checks.

**Bootstrap policy:** reconstruct current stock lots from report trades and IB aggregate cost basis. If a
position predates the report window, create an explicit inferred pre-report lot only when reconciled to
IB aggregate quantity/basis; any manual entry date is stored in provenance. Current known override:
`FISV` 100 shares entered `2025-10-29`, basis inferred from IB aggregate cost basis. Lent shares do not
reduce exposure; lending is stored separately (e.g. BDX uses shares-at-IB for exposure, not net shares).

**Acceptance tests:**
- Raw IB CSV hash matches the sealed import; source period/end date and account metadata are recorded.
- Normalized CSV hashes match the build metadata; SQLite row counts match sealed CSV artifacts.
- Current holdings reconcile to IB open positions; stock lots reconcile to current quantity and cost basis.
- Trade keys are unique/idempotent and include the raw statement `source_row`, so value-identical fills
  inside one statement cannot collide; reprocessing the same CSV replaces rows instead of duplicating them.
- Instruments and cash/NAV data are present; securities lending is separated from portfolio exposure.
- `enabled_in_production:false`; no live IB/TWS connection in the core stage.

**Known ledger limits before Stage 9:**
- Overlapping date-range statements may repeat the same trade under different raw-source hashes unless a
  future IB report includes a stable execution ID; Stage 9 should consume one sealed ledger run, not merge
  overlapping broker statements naively.
- Lot-level cost basis is reconciled to IB aggregate basis. If a corporate action or wash-sale adjustment
  creates a trade-vs-aggregate residual, the aggregate is authoritative and the residual is attached to the
  last lot; aggregate P&L is correct, while per-lot P&L remains an audited approximation.

## Stage 9 — Exit engine

**Goal:** systematic, sleeve-specific exits over the **actual ledger holdings**. Stage 9 is not a
fresh-from-cash rebalance and does not force the broker book into the Stage 8 target. It decides which
currently held equity positions should be kept, reviewed, soft-exited, or hard-exited; target-book gaps are
reported only as diagnostics.

**Prerequisite:** consumes the sealed Stage 8.5 holdings ledger. The exit run must explicitly stamp the
portfolio target as-of date and the ledger as-of date; an equal-date run is preferred, but a later broker
ledger may be accepted for current-holdings exits only when the PIT rule is documented in the manifest.
Default PIT rule: `signal_as_of <= ledger_as_of`, using the latest sealed score/risk stack on or before the
ledger date.

**Build:** `exits/33_build_exit_signals.py`, `exits/34_apply_exits.py`,
`exits/35_validate_exits.py`.

**Phase 1 scope:**
- Equities only: every actual stock holding gets exactly one decision.
- Options are emitted to `unsupported_positions.csv` as `unsupported_phase1`, never traded.
- Securities lending does not reduce exposure; exits use owned shares from the ledger state.
- Held-but-not-scored names default to `review`/keep (no score-decay sale is possible).
- Scored but no-longer-investable names become `soft_exit` candidates, not hard forced sales.
- Scored + investable names use absolute `final_score` signal-decay thresholds for hard/soft exits; low
  confidence, weak score, large loss, or concentration become review flags. Within-sector `rating` is context
  only unless a legacy fallback is explicitly enabled.
- `events/catalyst_events.csv` is absent in Phase 1, so catalyst time-stops are WARN+disabled.
- Stage 8 target overlap/gap is reported in `target_gap_report.csv`; no buy orders are generated.
- Output is an **exit recommendation list** (`exit_actions.csv`) plus diagnostics, not a conserved
  reweighted target book. Exited weight flows to CASH only in the later Stage 12 transition/orchestration
  layer once actual trading, no-trade bands, and target reconciliation are applied together.
- `estimated_realized_pl` is proportional to current unrealized P&L in Phase 1. Lot-level FIFO/tax-aware
  realized P&L is deferred to Phase 2 / Stage 12 execution planning.

**Acceptance tests:**
- Stage 8.5 ledger manifest PASS and hashes current; Stage 1 signal manifest hard gates PASS and
  `signal_as_of <= ledger_as_of`.
- Every actual equity holding appears exactly once in `exit_signals.csv` and `exit_actions.csv`.
- Held-but-not-scored names are not force-sold by score logic.
- Price-only moves with intact score do not force an exit; a synthetic scored/investable large-loss probe
  must produce keep/review, not soft/hard exit.
- No options or target-only names appear in tradable exit actions.
- No generated action is a buy; Stage 9 only proposes exits/reviews on actual holdings.
- Deterministic rebuild and sealed `exit_manifest.json` with config/source/input/output hashes.
- WARN-only diagnostics: target gap vs Stage 8, disabled catalyst events, and any not-scored/not-eligible
  coverage gaps.

**Carry-forward bookkeeping after Phase 1:**
- Lot-level FIFO/tax-aware realized P&L is still deferred; Phase 1 `estimated_realized_pl` is only a
  proportional shadow estimate.
- The "no harmful AQR price-stop" rule is now a formal synthetic gate: intact scored/investable positions
  with large price-only losses may become `review`, never a forced exit.
- A conserved `exit_adjusted_target_weights.csv` is intentionally absent in Phase 1. Materializing exits
  into CASH and proving weight conservation belongs in Stage 12 transition/orchestration, where exits,
  no-trade bands, costs, and target reconciliation are applied together.
- Current blue-chip health-care soft-exits are policy-dependent on the upstream `med_devices`
  `portfolio_candidate_gate` quality. That dependency is acceptable while shadow-only, but Stage 11/12
  promotion should audit whether the eligibility gate is too aggressive before any automatic sale.

## Stage 10 — Payout / liability layer

**Goal:** generate periodic payouts from natural rotation without forced liquidation.

**Build:** `payout/14_build_payout_liability.py` (cash buffer, payout *range* as a soft optimizer
constraint, harvest-staggering so realized gains spread over time).

**Acceptance tests:**
- Payout is met from cash buffer + natural rotation across a backtest; a forced-sale detector confirms no
  position with an intact thesis is liquidated solely to fund a distribution.
- Payout cadence is hit within tolerance; shortfalls draw the buffer, not core winners.
- Distribution constraint (soft penalty) does not materially degrade net Sharpe vs the no-payout book.

## Stage 11 — Walk-forward + lockbox validation harness (promotion gate)

**Goal:** the rigorous gate wrapping the combined system; lockbox period untouched until final.

**Binding protocol:** `docs/LOCKBOX_PROTOCOL.md` (declared 2026-06-27, sealed BEFORE any historical
replay/calibration result was inspected). It fixes the windows — development 2024-01-02..2025-12-31,
lockbox 2026-01-01..Open Event — plus purge/embargo rules, the registered comparison arms (payout
excluded by default), promotion criteria, the one-open policy, and enforcement (`65`/`66`/`16` refuse
sealed dates without `--lockbox-open`; manifests record the protocol file's sha256).

**Readiness tracker - what exists vs. what Stage 11 still needs:**

| area | current implementation / data status | still required for full Stage 11 |
|---|---|---|
| Sector score snapshots | Stage 1 consumes dated sector files and emits sealed `stocks_scores.csv` snapshots. Med-devices has generated daily `score_review_pack/<yyyy-mm-dd>/med_device_daily_composite_scores.csv` history from 2019-01-04 through current, with candidate gate, candidate score, model/version fields, calibration eligibility, component scores, and liquidity/capacity fields. Tech/biotech historical file generation is in progress. | Generate the same PIT dated Stage-1-consumed CSV history for biotech, semiconductors, technology hardware, and software infrastructure. Each file must include `asof_date`, ticker/company/taxonomy, native score, investability gate, `calibration_eligible_flag`, score/model/scoring versions, confidence/data-completeness, component scores, review/veto reasons, and `avg_dollar_volume_60d` when available. |
| Stage 1 replay | Current Stage 1 can build the live canonical contract and already points med-devices to the dated path template. It preserves source hashes, duplicate-resolution provenance, finite/range gates, canonical overrides, and model/version fields where published. | Add/run the historical replay driver that iterates dates, requires all enabled sectors for each date, writes one sealed `stocks_scores.csv` snapshot per date, and stores a PIT snapshot index/table. Historical replay must fail loudly on missing sector files, score-scale drift, stale/mixed as-of dates, or missing calibration-critical fields. |
| Forward returns | Stage 2 owns the live/current price panel and covariance model. | Build Stage 11 target-generation over historical snapshots: 21d, 63d, 126d, and 252d forward returns; sector-excess returns; benchmark returns; drawdown targets; and factor-residual returns. Targets must align `snapshot_as_of_date` to future returns without look-ahead. |
| Survivorship | Stage 2 is intentionally live/current-book oriented and does not solve long-horizon survivorship. Some sectors have delisted/Norgate support outside the portfolio layer. | Build `backtest/15b_build_survivorship_panel.py`: a survivorship-complete return panel for historical backtests. Use layer-owned ingestion or published sector delisted-price exports, never live reads of sector DBs. Yahoo-only history is not acceptable for the full Stage 11 backtest because delisted names disappear. |
| Benchmarks / factors | Stage 2 already carries broad and sleeve ETFs used for risk, rotation, and factor decomposition. | Ensure historical benchmark/ETF coverage exists for every snapshot date and forward-return horizon: SPY/QQQ plus sleeve ETFs such as SMH/SOXX, XBI, IHI, and configured software/hardware/foreign proxies. |
| Macro regime | Stage 6 produces a sealed macro contract from independent MacroLayer outputs with PIT `MAX(as_of_date) <= run_as_of` semantics. | Build historical macro-regime joins for each score snapshot date, including regime label, gross scalar, sector macro fits, and foreign-budget state. Historical macro features must use vintages/releases available by the snapshot date. |
| Rotation state | Stage 5 produces sealed rotation signals and optimizer-compatible sector states. | Recompute or load historical rotation state for each snapshot date using only prices available at that date. Join `Positive`/`Neutral`/`Negative` sleeve state and foreign eligibility to the PIT snapshot store. |
| Risk / liquidity state | Stages 2, 2.5, and 4 build current risk eligibility, covariance, liquidity snapshots, and transaction-cost overlays. | Store historical risk eligibility, coverage reason, spread/cost assumptions, ADV/capacity, and cost estimates by snapshot date. Liquidity can use historical IB spread samples when available, then latest-prior fallback, then documented default spread. |
| BL / sleeves / exits | Stages 7, 8, 8.5, and 9 are implemented as sealed shadow layers: BL fusion, sleeve risk proposal, broker ledger ingestion, and actual-holdings exits. | For Stage 11, replay these layers through time as comparison arms: AQR-only, +rotation, +macro/BL, +sleeves, +exits. Exit tests require historical or replayed holdings/ledger state; otherwise exits remain a current-book diagnostic until Stage 12 orchestration/backtest support exists. |
| Calibration model | Stage 1 slopes are provisional; Stage 7 consumes them as annual alpha views but does not prove the payoff. | Estimate payoff slopes by sector, horizon, and regime after enough PIT snapshots exist. Start with within-sector/date standardization and ridge shrinkage; add elastic-net and Bayesian hierarchical shrinkage only after basic diagnostics are stable. Emit calibrated slopes plus confidence intervals back to Stage 1/Stage 7 only after OOS gates pass. |
| Optional modules | Forecasting (6.7) and hedging (7.5) are intentionally deferred. | Test ML forecasting and hedging only inside Stage 11 after the rule-based stack has a valid OOS baseline. They remain shadow-only unless they beat the simpler stack OOS net of cost. |

**Minimum Stage 11 dataset row:** one row per `(snapshot_as_of_date, ticker)` containing the sealed Stage 1
score fields (`source_pipeline`, native score, `final_score`, `rating`, `score_confidence`,
`investable_eligible`, `calibration_eligible_flag`, model/scoring versions, component scores), joined to
macro regime, rotation state, sleeve assignment, risk eligibility, liquidity/capacity, forward returns,
sector/benchmark excess returns, factor-residual returns, and survivorship/delisting flags.

**Data-quality rule:** historical sector CSVs are valid for Stage 11 calibration only if they are
point-in-time. A file dated `2019-01-04` must have been generated using data available on or before
2019-01-04. If a historical file uses today's revised data, future-known classifications, or future-known
eligibility/veto decisions, it may be useful for diagnostics but not for true OOS calibration.
Technology dashboard rank snapshots also replay the current universe and are not a survivorship-correct
calibration panel unless explicitly stamped with `survivorship_corrected_panel_flag=1`. See
`docs/score_eligibility_flags.md` for the canonical flag meanings and consumers.

**History horizon rule:** a 252-trading-day forward target requires at least 252 trading days after each
snapshot. One year of daily snapshots is the minimum for first 252d labels; 2-3 years of snapshots is the
practical minimum for stable sector/horizon/regime calibration. Weekly sampling can be used for OOS folds
or lower-overlap diagnostics, but the raw snapshot store should remain daily so Stage 11 can choose the
proper sampling cadence without losing information.

**Build - optimal Stage 11 sequence:**
- `backtest/15b_build_survivorship_panel.py` - the **survivorship-complete** return panel. Delisted/Norgate
  history is mandatory here, unlike Stage 2's live-only panel. Source decision: self-ingest delisted history
  into the layer vs. consume each sector's *published* delisted-price export - never a live sector-DB read.
- `research/65_build_pit_score_snapshot_store.py` - PIT snapshot history for score calibration: score rows,
  eligibility/risk status, sector/sleeve membership, regime label, rotation state, and sealed source hashes.
- `research/66_define_calibration_targets.py` - forward-return targets needed first:
  `forward_return_21d`, `forward_return_63d`, `forward_return_126d`,
  `forward_excess_return_vs_sector`, `drawdown_next_63d`, and `regime_at_snapshot`.
- `backtest/16_run_ablation_walkforward.py` - walk-forward across the existing rule-based stack:
  AQR-only, +rotation, +macro/BL, +sleeves, +exits, net of cost.
- `backtest/16b_run_regime_parameter_sweep.py` - research-only regime-gated parameter sweep over the
  Stage 16 `regime_lever` arm: supportive-regime score multiplier, rebalance cadence, and unsupported-regime
  fallback mode. It emits evidence only; promotion still requires the Stage 11 lockbox gates.
- `backtest/17_publish_lockbox_ledger.py` - sealed out-of-sample ledger.

Only after those pass should optional modules be tested: `forecast/67_train_models.py` /
`forecast/67_calibrate.py` for ML forecasting, and `hedging/75_run_hedging_overlay.py` for hedging. They are
compared inside Stage 11 and remain shadow-only unless they beat the simpler stack OOS net of cost.

**Empirical alpha calibration note (core Stage 11 item, not a Stage 1 replacement today):** the current
Stage 1 score-to-expected-alpha slopes are provisional and remain shadow-only until enough PIT score
history exists. Stage 11 must add the institutional calibration module:
1. Build score snapshot history.
2. Join each snapshot to forward returns.
3. Standardize scores within sector and date.
4. Estimate payoff slopes by sector, horizon, and regime.
5. Use ridge/elastic-net shrinkage.
6. Add Bayesian shrinkage toward zero.
7. Validate with walk-forward / purged OOS tests.
8. Emit calibrated alpha slopes + confidence intervals.
9. Feed those back into Stage 1 / Stage 7.

Decision framing to preserve: same direction as the current architecture - yes; better than provisional
slopes once enough history exists - yes; better than replacing the current staged implementation today - no,
because it requires historical PIT score snapshots we do not yet have. It should become a core Stage 11
calibration module, not a premature Stage 1 rewrite.

**Acceptance tests:**
- A survivorship-bias probe confirms delisted/halted tickers are present in the backtest panel (no
  survivorship gap); the live Stage 2 panel is explicitly *not* used for historical backtests.
- Walk-forward first runs the **existing rule-based stack**:
  `stocks-only / +rotation / +macro+BL / +sleeves / +exits`, with no look-ahead.
  `+forecast` and `+hedge` are optional comparison arms only after the PIT calibration harness exists.
- Score snapshots are mapped to realized forward returns before any alpha-vs-cost metric is used:
  implement a real spread/commission-vs-realized-forward-alpha diagnostic here, replacing the intentionally
  deferred Stage 2.5 `final_score`/`mu_used` comparison.
- **Full stack beats net-of-cost AQR-only baseline on out-of-sample information ratio** (Calmar and max
  drawdown also reported). If it does not, the simpler configuration is promoted instead.
- Lockbox ledger is sealed and append-only; a tamper/look-ahead probe fails the build if violated.
- Results are benchmarked against SPY/sector-ETF beta and a risk-parity baseline (context, not gate).
- ML forecasting and hedging are promoted only if their Stage 11 comparison beats the simpler rule-based
  stack out-of-sample, net of cost; otherwise they stay shadow-only or unbuilt.

## Stage 12 — Orchestration, multi-timescale rebalance, risk governor

**Goal:** one production pipeline with strategic (monthly) + tactical (weekly) cadences and a
portfolio-level kill-switch.

**Build:** `orchestration/18_run_portfolio_pipeline.py` core DAG:
`scores -> risk -> liquidity/costs -> rotation -> macro -> BL -> sleeves -> exits -> payout`.
Optional branches (`forecast`, `hedging`) are disabled unless Stage 11 OOS validation promotes them.
`orchestration/19_run_risk_governor.py` handles drawdown circuit-breaker + regime kill-switch using the
rule-based stack first; ML/hedging governors are later optional plugins, not baseline dependencies.

**Acceptance tests:**
- One command rebuilds the full book consistently; strategic vs tactical cadences run on schedule without
  churning the core book on tactical noise (turnover attribution by cadence).
- Risk governor cuts gross exposure on a simulated drawdown breach and on a rule-based risk-off regime flip;
  recovery re-risks correctly. Forecast-driven cuts are optional only after Stage 11 promotion.
- Full pipeline is idempotent and PIT-consistent across all sectors at a single as-of date.
- End-to-end dry-run on current data produces a deployable target book with full provenance.

---

## Cross-cutting validation philosophy

- Every stage from 5 onward carries the same ablation discipline: does this layer beat the simpler book
  net of cost, out-of-sample? Non-improving layers stay shadow-only.
- The lockbox window is defined once (Stage 11) and never inspected during development.
- Shadow-mode first for each new signal, consistent with how FDA features were staged in biotech.
- Forecasting carries the highest overfitting risk. Do not build it before Stage 11 has PIT score snapshots,
  realized forward-return targets, and a rule-based OOS baseline. If built later: few features,
  regularization first, purged/embargoed CV, deflated Sharpe, and PIT vintages are mandatory.

## Decisions to confirm before Stage 3 (sensible defaults assumed)

1. **Long-only vs long/short** for the book. *Default: long-biased with small tactical short capability.*
2. **Strategic / tactical rebalance cadence.** *Default: monthly strategic (macro/AQR), weekly tactical (rotation).*
3. **Payout cadence & target range.** *Default: quarterly, range-based, buffer-funded.*
4. **Foreign ETF sleeve on at launch?** *Default: build it (Stage 5), budget held at zero until macro
   country-fit is live (Stage 6).*
5. **Hedging instrument.** *Deferred until Stage 11; default comparison candidates are gross-exposure reduction + index puts, with inverse ETFs optional.*

## Sequencing note

Build value first, plumbing later, forecasting last. Stages 0-4 give a working, cost-aware, risk-managed
AQR book. Stages 5-8 add timing, macro/BL allocation, cost-adjusted fusion, and sleeve risk budgeting.
Stage 9/10 add exits and payout mechanics if still needed. Stage 11 then builds the PIT snapshot/target
infrastructure, empirical alpha calibration, and OOS promotion harness. ML forecasting and hedging are tested
only after that harness exists and are promoted only if they beat the simpler rule-based stack net of cost.
