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
  risk/        04_return_panel, 05_covariance, 06_validate
  costs/       12_build_trade_list, 13_build_cost_model, 14_apply_no_trade_bands, 15_validate_cost_model
  optimizer/   tier1_portfolio_optimizer.py, tier1_common.py, 08_run_optimizer   (vendored)
  rotation/    rotation_timeseries.py, sector_rotation_selector.py, foreign_market_evaluator.py,
               17_build_rotation_signals, 18_validate_rotation_signals, 19_run_rotation_ablation_replay (clean)
  MacroLayer/  vendored macro engine + macro_raw.sqlite/macro_serving.sqlite (independent)
  macro/       Stage 6 adapter: 20_run_macro_serving, 21_build_macro_contract,
               22_validate_macro_contract
  features/    65_pit_feature_store, 65_data_routing                         (Stage 6.5)
  forecast/    66_define_targets, 67_train_models, 67_calibrate              (Stage 6.6-6.7)
  blacklitterman/  10_build_bl_views
  hedging/     75_run_hedging_overlay                                        (Stage 7.5)
  sleeves/     11_build_sleeves, 12_apply_risk_budgets
  exits/       13_run_exit_engine
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
| `ticker`, `sector`, `industry`, `industry_aggregate` | identity / taxonomy |
| `final_score` | **calibrated expected forward alpha** (excess vs sector benchmark), common units |
| `rating` | bucketed rating, identical definition across sectors |
| `score_confidence` | calibration confidence / coverage haircut |
| `investable_eligible` | 0/1 **hard gate** carried from the sector's native safety/quality gate (med: `passed_tier1_safety_gate`; tech: `rank_ready_flag`/`calibration_eligible_flag`; biotech: its own gate). The optimizer ranks/sizes **only eligible names** — never top raw score. |
| `eligibility_reason` | provenance for the gate decision (why eligible/excluded) |
| `source_pipeline`, `score_version` | provenance |

This is exactly what `tier1_portfolio_optimizer` already consumes — the join is a contract, not a code merge.

**Eligibility semantics (decided):** a sector hands the portfolio layer its *safety/quality-gate-eligible*
set, not its own final pick list. The sector vouches that a name is safe/eligible; cross-sector
*selection and sizing* is the portfolio layer's job. The eligible set is therefore a **superset** of
the sector's internal final list (e.g. med Tier 1 ⊆ med safety-gate-eligible), and the validate gate
asserts that containment. Headline score per sector: med = `composite_score` (= `raw_composite_score`,
IC tilt already baked in via `replace_raw`); tech family = `final_score`; `ic_tilted_composite_score`
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
- `investable_eligible` is populated from each sector's native safety/quality gate; ineligible names
  are excluded from optimizer input. The eligible set is a superset of the sector's own final pick
  list (med Tier 1 ⊆ med safety-gate-eligible), asserted by the validate gate.
- Duplicate tickers across sectors are detected and resolved deterministically.

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
added once a holdings ledger exists (Stage 8+).

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
matches the manifest hash (no stale/tampered downstream artifact); (4) source staleness within tolerance
(from manifest `per_sector` source dates); (5) every enabled sector is present in the sealed manifest.
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
behavior — that waits for the holdings ledger (Stage 8+). This is the correct default pre-ledger.

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

## Stage 6.5 — Unified PIT feature store & data routing

**Goal:** a vintage-correct feature store combining macro + market-internal + routed alternative data.

**Build:** `features/65_pit_feature_store.py` (point-in-time panel using ALFRED/PIT vintages);
`features/65_data_routing.py` (explicit routing table). New alt-data connectors added selectively:
- **Timing/regime → macro layer:** BEA, BLS (CPI/PPI/JOLTS), Census aggregates, Treasury FiscalData,
  World Bank/IMF/OECD.
- **Sector demand/fundamental → sector pipelines (alpha, not timing):** USAspending (federal IT/cloud/
  cyber obligations), SAM.gov (forward solicitations), NSF awards, EIA electricity (datacenter/AI power).

**Acceptance tests:**
- Every feature carries a vintage/as-of; a leakage probe confirms **no revised values** appear in any
  training window (point-in-time integrity).
- Routing is enforced: sector-demand feeds land in sector pipelines, not the macro forecaster; a test
  asserts no quarterly-macro feature is exposed to the 1–2wk model.
- Each new connector has freshness/coverage QA and is gated on orthogonality (incremental correlation
  below threshold vs existing features) before inclusion.
- Highest-priority orthogonal sources (EIA datacenter power, SAM.gov/USAspending forward IT demand)
  ingest and pass QA.

## Stage 6.6 — Forecast targets & labels

**Goal:** define what the forecaster predicts — risk, not price.

**Build:** `forecast/66_define_targets.py` producing horizon-matched labels: realized volatility,
`P(drawdown > X%)`, regime-transition probability, return-distribution quantiles — at 1w/2w/1m/3m.

**Acceptance tests:**
- All labels are computable point-in-time with no look-ahead; horizon→data mapping enforced (1–2wk uses
  market-internal data only; 1–3m may use macro).
- Targets are probabilistic/distributional — **no point price-level target exists** in the schema.
- Label leakage probe (overlapping-horizon contamination) passes under purged construction.

## Stage 6.7 — ML forecasting models

**Goal:** calibrated probabilistic forecasts that beat the existing rule-based regime.

**Build:** `forecast/67_train_models.py` (regularized models — penalized logistic / gradient boosting
before anything deep; few economically-motivated features); purged & embargoed walk-forward CV;
`forecast/67_calibrate.py` (probability calibration + confidence intervals).

**Acceptance tests:**
- Forecasts are calibrated: reliability curve within tolerance; intervals have correct empirical coverage.
- Validation uses **purged & embargoed walk-forward CV**; deflated Sharpe / multiple-testing correction
  applied to any signal search.
- **Gate:** the model beats the rule-based MacroLayer regime decision out-of-sample, net of cost. If it
  does not, it stays shadow-only and the rules are retained.
- Volatility and drawdown-probability forecasts show genuine skill (Brier/CRPS beats climatology baseline).

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

## Stage 7.5 — Hedging / actuation overlay

**Goal:** translate a forecast downturn into action — scale down, hedge, or rotate defensive.

**Build:** `hedging/75_run_hedging_overlay.py` mapping forecast drawdown probability / risk-off signal to
gross-exposure reduction, index put protection, or inverse/defensive rotation, with hedge cost modeled.

**Acceptance tests:**
- A forecast risk-off event triggers the configured hedge in backtest.
- The hedged book shows lower max drawdown than the unhedged book with an acceptable net-return cost.
- Hedge cost (premium/slippage) is explicitly modeled and netted in all reported results.
- Ablation: hedging improves Calmar / tail metrics vs the un-hedged Stage 7 book.

## Stage 8 — Multi-horizon sleeves + risk budgeting

**Goal:** partition the book into horizon sleeves with risk (not capital) budgets and per-sleeve
drawdown limits.

**Build:** `sleeves/11_build_sleeve_framework.py` (assign names to short-catalyst / medium / long-AQR
sleeves by driving signal); `sleeves/12_apply_risk_budgets.py` (vol-contribution budget per sleeve,
risk-contribution caps, sleeve caps, pod-style per-sleeve drawdown throttle).

**Acceptance tests:**
- Each position maps to exactly one sleeve with an auditable reason; the AQR factor score drives the long
  (6–18mo) sleeve, catalysts (e.g., FDA AdCom dates) drive the short (1–3mo) sleeve.
- Sleeve allocations are by **vol contribution**, not capital — the speculative sleeve's capital share is
  below its equal-capital share.
- No single name exceeds its risk-contribution cap (% of portfolio variance); a concentration probe (one
  huge alpha) is correctly capped.
- A simulated sleeve drawdown beyond its limit automatically throttles that sleeve's risk budget at the
  next rebalance.

## Stage 9 — Exit engine

**Goal:** systematic, sleeve-specific exits beyond optimizer rebalancing.

**Build:** `exits/13_run_exit_engine.py` implementing signal-decay exit (AQR), time-stop (catalyst sleeve,
event-date driven), vol/ATR stop (speculative), trim-to-target profit-taking, cost-aware no-trade bands.

**Acceptance tests:**
- Signal-decay exits trigger when calibrated score leaves the top quantile; price-only moves do **not**
  force-exit a value position (no harmful fixed stop on the AQR sleeve).
- Catalyst positions time-stop at/after event resolution regardless of P&L.
- Speculative vol-stops cap per-trade loss at the configured fraction of sleeve risk budget on a stress replay.
- Ablation: exit engine improves Calmar / reduces max drawdown vs Stage 8 without materially cutting net return.

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

**Build:** `backtest/16_run_ablation_walkforward.py` (walk-forward across all ablations);
`backtest/17_publish_lockbox_ledger.py` (sealed out-of-sample ledger);
`backtest/15b_build_survivorship_panel.py` — the **survivorship-complete** return panel (this is where
delisted/Norgate history becomes **mandatory**, unlike Stage 2's live-only panel). Source decision to
make here: self-ingest delisted history into the layer vs. consume each sector's *published* delisted
price export — never a live sector-DB read. Include a cross-check audit reconciling a sample of overlap
tickers + benchmarks against the sectors' own series.

**Acceptance tests:**
- A survivorship-bias probe confirms delisted/halted tickers are present in the backtest panel (no
  survivorship gap); the live Stage 2 panel is explicitly *not* used for historical backtests.
- Walk-forward runs `stocks-only / +rotation / +macro / +forecast/hedge / +sleeves+exits` end-to-end with
  no look-ahead (PIT enforced at every stage).
- Score snapshots are mapped to realized forward returns before any alpha-vs-cost metric is used:
  implement a real spread/commission-vs-realized-forward-alpha diagnostic here, replacing the intentionally
  deferred Stage 2.5 `final_score`/`mu_used` comparison.
- **Full stack beats net-of-cost AQR-only baseline on out-of-sample information ratio** (Calmar and max
  drawdown also reported). If it does not, the simpler configuration is promoted instead.
- Lockbox ledger is sealed and append-only; a tamper/look-ahead probe fails the build if violated.
- Results are benchmarked against SPY/sector-ETF beta and a risk-parity baseline (context, not gate).

## Stage 12 — Orchestration, multi-timescale rebalance, risk governor

**Goal:** one production pipeline with strategic (monthly) + tactical (weekly) cadences and a
portfolio-level kill-switch.

**Build:** `orchestration/18_run_portfolio_pipeline.py` (DAG: scores → risk → costs → features → forecast
→ rotation → macro → BL → hedge → optimize → sleeves → exits → payout);
`orchestration/19_run_risk_governor.py` (drawdown circuit-breaker + regime/forecast kill-switch).

**Acceptance tests:**
- One command rebuilds the full book consistently; strategic vs tactical cadences run on schedule without
  churning the core book on tactical noise (turnover attribution by cadence).
- Risk governor cuts gross exposure on a simulated drawdown breach and on a risk-off regime/forecast flip;
  recovery re-risks correctly.
- Full pipeline is idempotent and PIT-consistent across all sectors at a single as-of date.
- End-to-end dry-run on current data produces a deployable target book with full provenance.

---

## Cross-cutting validation philosophy

- Every stage from 5 onward carries the same ablation discipline: does this layer beat the simpler book
  net of cost, out-of-sample? Non-improving layers stay shadow-only.
- The lockbox window is defined once (Stage 11) and never inspected during development.
- Shadow-mode first for each new signal, consistent with how FDA features were staged in biotech.
- Forecasting (6.5–6.7) carries the highest overfitting risk: few features, regularization first,
  purged/embargoed CV, deflated Sharpe, and PIT vintages are mandatory, not optional.

## Decisions to confirm before Stage 3 (sensible defaults assumed)

1. **Long-only vs long/short** for the book. *Default: long-biased with small tactical short capability.*
2. **Strategic / tactical rebalance cadence.** *Default: monthly strategic (macro/AQR), weekly tactical (rotation).*
3. **Payout cadence & target range.** *Default: quarterly, range-based, buffer-funded.*
4. **Foreign ETF sleeve on at launch?** *Default: build it (Stage 5), budget held at zero until macro
   country-fit is live (Stage 6).*
5. **Hedging instrument.** *Default: gross-exposure reduction + index puts; inverse ETFs optional.*

## Sequencing note

Build value first, plumbing later, forecasting last. Stages 0–4 give a working, cost-aware, risk-managed
AQR book. Stages 5–7 add timing and allocation. Stages 6.5–7.5 add the statistical forecasting brain and
hedging — deliberately late, after the ablation harness exists, because they carry the highest risk of
self-deception. Stages 8–12 add sleeves, exits, payout, validation, and orchestration.
