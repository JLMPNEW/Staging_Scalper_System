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
  costs/       07_cost_turnover_model
  optimizer/   tier1_portfolio_optimizer.py, tier1_common.py, 08_run_optimizer   (vendored)
  rotation/    sector_rotation_selector.py, foreign_market_evaluator.py,
               rotation_timeseries.py, 09_build_rotation_signals             (vendored)
  macro/       <migrated MacroLayer subtree + macro_serving.sqlite>          (vendored)
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

**Build:** copy `tier1_portfolio_optimizer.py` + `tier1_common.py` into `optimizer/`, re-root all paths;
`optimizer/08_run_portfolio_optimizer.py` wrapper feeding `stocks_scores` (calibrated `final_score` → BL
view) + Stage-2 covariance; minimal `backtest/15_run_portfolio_backtest.py`.

**Acceptance tests:**
- Optimizer runs end-to-end on real `stocks_scores` + covariance; weights sum to target gross/net with
  all constraints satisfied (position caps, long/short policy).
- Weights are monotonic-ish in `final_score` within a sector (higher alpha → higher weight, risk-adjusted).
- Backtest of the AQR-only book produces a sensible equity curve over the holdout; Sharpe/Calmar/turnover
  recorded as the **baseline** all later stages must beat.
- Output schema is stable and versioned (weights + low/high bands per name).

## Stage 4 — Transaction-cost & turnover model

**Goal:** make cost a first-class input before adding any faster signal.

**Build:** `costs/07_build_cost_turnover_model.py` (per-name cost from spread/ADV/vol + market-impact
term); **no-trade bands** integrated into the optimizer wrapper.

**Acceptance tests:**
- Backtests report **gross and net-of-cost** returns; the gap is non-trivial and scales with turnover.
- No-trade bands measurably cut turnover vs Stage 3 with small net-return loss (band sensitivity curve produced).
- Cost estimates are sane against a manual benchmark on liquid vs illiquid names.
- Net-of-cost AQR-only baseline is locked as the official benchmark for all subsequent ablations.

## Stage 5 — Tactical rotation sleeve

**Goal:** add the fast (1–2wk) money-flow tilt across sector and foreign ETFs.

**Build:** vendor `sector_rotation_selector.py`, `foreign_market_evaluator.py`, `rotation_timeseries.py`
into `rotation/`; `rotation/09_build_rotation_signals.py` emits `sector_rotation_csv` + `foreign_etfs_csv`.

**Acceptance tests:**
- Rotation features compute on the Staging price panel; sector/foreign tables match the optimizer's
  expected input schema exactly.
- `State`/`ScorePct` behave correctly on known historical rotations (e.g., energy 2022 ranks top; a
  known downtrend is gated out by the absolute-trend filter).
- Ablation: **AQR + rotation** vs AQR-only, net of cost, on the holdout — rotation must not degrade net
  Sharpe (improvement is the goal; non-degradation is the gate).
- Rotation tilts at the margin only (bounded magnitude); it cannot flip a core long to a short.

## Stage 6 — MacroLayer migration

**Goal:** migrate the regime/fit engine with its own serving DB and connectors, fully Staging-rooted.

**Build:** copy MacroLayer subtree → `macro/`; re-root `config_macro_raw.yaml`, DB paths, `out/` dirs;
wire API keys via env; run the serving DAG up through regime decision, industry/sector fit, country fit,
stock overlay.

**Acceptance tests:**
- Macro raw + serving pipelines run end-to-end writing to `macro/macro_serving.sqlite` (no PROD paths,
  verified by scan).
- `macro_regime_decision_daily`, `sector_macro_fit_daily`, `country_macro_fit_daily`,
  `stock_macro_fit_daily` materialize for current dates.
- Known stress windows (2008, 2020, 2022 rate-hike) classify into risk-off / contraction regimes.
- Macro `check_*` diagnostics pass at parity with PROD on overlapping history (allowing for data vintage).

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

## Stage 7 — Black-Litterman integration: macro views + sector budgets

**Goal:** fuse calibrated alpha (views) with regime-driven sector budgets and forecast-driven exposure.

**Build:** `blacklitterman/10_build_bl_views.py` mapping calibrated `final_score` → BL views, macro sector
fit → sector tilt/budget constraints, and forecast regime/vol → gross-exposure scaling + view confidence.

**Acceptance tests:**
- BL posterior blends calibrated views with the equilibrium prior; views are expected-return units (no
  ordinal-rank contamination — verified).
- Inter-sector weight differences are driven by **macro sector budgets**, not raw cross-sector score
  comparison (shuffling cross-sector score *levels* while holding within-sector ranks leaves sector
  budgets unchanged).
- Risk-off regime / high forecast drawdown probability measurably reduces gross exposure (exposure-vs-
  regime curve produced).
- Full ablation (`baseline / macro-full / stocks-only`) runs and is reported net of cost vs baseline.

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
