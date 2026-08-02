# MacroLayer Systematic Audit — 2026-08-02

Scope: all ~70 scripts under `portfolio_layer/MacroLayer` (ingestion, connectors, PIT serving,
features, composites, v1 probabilities/regimes, v2 shadow + promotion, H1 hybrid, Stage 9–11 fit
layers, Stage 12A–D portfolio integration, shadow backtest, orchestration), plus the `macro.*`
contract in `portfolio_layer/config.yaml` and the live raw/serving SQLite databases.

Method: 7 parallel line-by-line review passes (one per functional cluster), followed by
independent verification of top-severity claims against the code and against `macro_raw.sqlite`.
Findings marked **[VERIFIED]** were confirmed directly (code read + DB query); others carry the
reviewing pass's file:line evidence. Line numbers are as-of this audit date.

Current system state (context for severity):
- Macro is shadow-only: `macro.enabled_in_production: false`, `regime_source: v1`.
- v2 promotion is sealed **NOT_PROMOTABLE** (`validated_probability_cells=0/4`); H1 is
  **NOT_PROMOTABLE**; v1↔v2 historical regime disagreement is 84.8%.
- As of 2026-07-31 the three models disagree outright: v1=SLOW_GROWTH, v2=HEATING_UP,
  H1=STAGFLATION, all low confidence. (Itself a symptom of the estimation issues below.)
- 2026-07-29 dated outputs are missing promotion + vintage-gap artifacts in all four v2
  namespaces — a partial pipeline run that no gate caught.

---

## 1. CRITICAL — PIT integrity of the data foundation

These four findings undermine the core premise ("train on what was known when") and should be
fixed before any further calibration/promotion work, because every layer above inherits them.

### C1. ALFRED incremental fetch stores clamped `realtime_start` as vintage → fabricated vintages [VERIFIED]
`connectors/fred_alfred.py:103,117,163-167` + `run_macro_raw_pipeline.py:195-216`.
Incremental true-vintage tasks request `realtime_start = last_vintage - revision_window_days`
while keeping `observation_start` at full history. FRED clamps each returned row's
`realtime_start` to the requested window start, and the connector stores that clamped date as
`vintage_date`/`release_date`. Because the dedupe key includes `vintage_date`
(`macro_storage.py:562-574`), every incremental run mints a full-history batch of rows under a
fabricated vintage date.
**DB evidence:** ICSA has ~3,600 rows per single vintage date, repeated weekly
(2024-05-14/21/28, 06-18, 07-02/16/23/30 each carry the entire ~69-year history). A genuine
weekly claims vintage revises 1–2 weeks. CFNAI shows the same signature (710 rows on
2020-05-26). Revision counts, `distinct_vintage_count`, first-print selection robustness, and
any vintage-window training query are corrupted. QA cannot see it: the vintage duplicate check
groups by `vintage_date` (`qa_macro_raw.py:598-624`), so clamped duplicates look distinct.
**Fix:** always fetch vintages with `realtime_start=1776-07-04` (or ALFRED `output_type=4`
first-release + `output_type=3` new-and-revised); discard rows whose `realtime_start` equals the
requested window start unless it is a genuine release; add a QA rule flagging any true-vintage
series where >N periods share one vintage date equal to a task window start.

### C2. Availability falls back to observation-period date → look-ahead for non-vintage sources [VERIFIED]
`macro_serving_common.py:203-212` (`effective_available_date` = max of observation/release/
vintage) + `build_macro_observation_daily_pit.py:32,71-78` (observation_date itself falls back
to *period start*). When release and vintage are NULL, a monthly value becomes "known" on the
1st of its reference month — before the period elapsed, let alone published. No publication-lag
model exists anywhere in the serving layer.
**DB evidence:** 98 of 127 metric_keys carry rows with NULL release+vintage; EIA (19,330/19,330)
and OECD (15,270/15,270) rows are 100% affected; plus 117,786 FRED rows and 24,253 ADS rows.
**Fix:** per-source publication-lag policy — availability = `max(period_end + configured_lag,
retrieved_at_date)` for non-vintage sources — and a post-build invariant check asserting
`effective_available_date_selected <= as_of_date` semantics can't be satisfied by period-start
fallbacks.

### C3. PIT history is rewritten on every rebuild for non-vintage sources
`build_macro_observation_daily_pit.py:341-346` (full-range clear + reinsert). For sources
storing only latest revised values, every rebuild recomputes all historical `as_of_date` rows
from current data — past PIT rows absorb future revisions, so downstream training data is
revised, not first-print. Companion: ADS current-file rows are stored with NULL vintage on a
true-vintage series and mutate in place each run (`connectors/phillyfed_ads.py:75-92,141-144`).
**Fix:** append-only first-seen persistence (store `retrieved_at`-stamped first prints) and
immutable PIT history for already-served dates.

### C4. Live API keys committed in tracked config [VERIFIED]
`config_macro_raw.yaml:525` (FRED) and `:540` (EIA) contain literal keys; the file is
git-tracked, and YAML keys take precedence over env vars (`run_macro_raw_pipeline.py:304-309`).
Worse, `00_validate_macro_layer_foundation.py:232-237` claims to check secret hygiene but PASSES
literal keys. **Fix:** rotate both keys, strip from YAML (use `api_key_env` only), make the
validator fail on literal keys, and consider git-history scrubbing.

---

## 2. HIGH — selection, orchestration, and gating defects

### Raw ingestion / QA
- **Soft connector failures count as success.** `run_macro_raw_pipeline.py:146-161`: OECD/SDMX
  return `FetchResult(error_text=...)` without raising; `error_count` stays 0 and the run is
  `completed`. A run where all 56 OECD series fail looks green. QA freshness uses all-history
  `max_release_date` (`qa_macro_raw.py:270`), so prior data masks a dead fetch.
- **QA gate is fail-open at process level.** `qa_macro_raw.py:924-947` writes `failed` to the DB
  but always exits 0; orchestrators keying off exit codes never see failure.
- **EIA pagination truncates** when `response.total` missing (`connectors/eia_seriesid.py:101-127`)
  and **literal `0` values are dropped** via `str(x or "")` (`eia_seriesid.py:133`).
- **Empty successful fetch wipes sync watermarks** (`macro_storage.py:636-691`) → next run
  refetches from history start and re-mints clamped vintages (interacts with C1).
- **Metric policy collapses multi-source metrics last-wins** (`macro_policy.py:53-77` vs one row
  per registry row from `build_macro_metric_policy.py:189-221`).

### PIT serving
- **Candidate ranking is vintage-first, not period-first.** `macro_serving_common.py:215-223`:
  any row with a vintage beats every row without one (date.min fallback), and a re-vintaged old
  period beats the newest period. Mixed-source metrics can pin to stale old periods.
- **Merge-stream stall:** SQL sort key vs Python availability mismatch
  (`build_macro_observation_daily_pit.py:103-136` vs `216-220`) — one forecast-style row whose
  Python availability is later than its SQL key blocks consumption of already-available
  candidates for months.
- **Partial-range run destroys the calendar.** `build_macro_calendar_daily.py:87` wipes the
  whole `macro_calendar_daily` then inserts only the requested window; PIT days deleted outside
  the window are never reinserted (calendar-driven).
- **Frequency/ref_area picked by `MIN()` across registry rows** (`macro_serving_common.py:120-144`)
  — 'daily' sorts first, wrongly triggering business-day staleness for non-daily fallbacks.
- **Stale tail rows survive** when the resolved end date moves backward
  (`macro_serving_storage.py:1380-1405` + `macro_serving_common.py:102-103`), and
  `macro_metric_latest` then serves exactly those rows.
- **Shadow steps hard-fail the mandatory DAG by default** (`run_macro_serving_pipeline.py:248-256`)
  — a v2 research failure aborts after PIT rebuild but before industry/country/stock layers,
  leaving the serving DB internally inconsistent (likely cause of the 2026-07-29 artifact gap).

### v1 probability / regime engine (live tilt driver)
- **Tautological NOW-horizon calibration [VERIFIED].** `build_macro_probabilities.py:46-51,158-164`:
  `P_G_NOW`/`P_PI_NOW` label = sign of the same composite, same month — perfect separation;
  slope set by the ridge penalty, train_auc trivially 1.0, diagnostics meaningless for those keys.
- **INITIALIZE bypasses every decision gate [VERIFIED].** `build_macro_regime_decision.py:243-252`:
  the first covered day's argmax becomes the incumbent regardless of probability, confidence,
  cadence, or confirmation — the entire hysteresis path depends on where table history starts.
- **Hysteresis can deadlock on a dead incumbent.** `build_macro_regime_decision.py:270-297`:
  nothing re-examines the incumbent's own probability; with 4 states and
  `min_top_probability=0.50`, mass can split so no challenger ever clears the gates while the
  incumbent decays toward the floor — a stale regime can drive tilts indefinitely.
- **Next-3m prediction uses a one-day transition step.** `build_macro_regime_smoothed.py:374-377`
  applies the daily transition matrix once (needs ~T^63 for a 3-month horizon), dragging
  `p_smoothed_next_3m_*` toward the current regime; transition counts from autocorrelated daily
  rows also swamp the prior (`transition_prior_strength=24` vs ~250 counts/yr).
- **Windowed rebuilds mutate mixed history** (smoothing warm-up always starts at table min but
  only the window is rewritten) — stored history becomes path-inconsistent.
- **Partial current month enters training as a complete month**
  (`build_macro_probabilities.py:135-141,163`) — labels flip when the month completes, so
  rebuilt history ≠ served history (non-reproducible past decisions).

### Features / composites
- **Z-score windows count events, not time.** `build_macro_features.py:511-535` + `471-508`:
  revision events re-emit the same period into the window (revision-heavy series bias their own
  mean/std), and level-series identity-compression (`326-327`) makes a "252-obs" window span a
  decade for step-like series. Cross-metric z-scores are not comparable.
- **The composite stress-window check suite is non-binding.** `check_macro_composite_regimes.py:211-219`:
  failures are WARN, missing data INFO, always exit 0 — and its analysis fallback (96-97)
  re-admits exactly the low-coverage raw values the coverage gate rejected.
- **SHOCK composite has no coherent stress sign convention** (commodities +, dollar +, neer/reer −):
  in a GFC/COVID event components cancel, which is why stress-window "spike" expectations are
  fragile. `us_10y_real_yield` at sign −1 as a *required* PI_LEAD member makes 2022-style
  tightening read as disinflationary inside the inflation-expectations composite.
- **The layer has no surprise/first-print/momentum features at all** despite the design intent —
  only level/diff/pct-change vs the latest-known vintage of the lag period.

### v2 / H1 promotion integrity
- **Deleting the H1 baseline re-baselines the drift/chain guard fail-open.**
  `validate_macro_h1_promotion.py:731-733,848-854`: missing baseline → chain-head continuity
  skipped, drift=[], fresh baseline created with no failure reason. Ops checkpoints under
  `output/h1_chain_checkpoints/` are never auto-reconciled. (Current live evidence already shows
  `baseline_created: false` with component drift listed — review whether the listed drift is
  expected.)
- **The v2 seal does not hash `macro_probability_v2.py`** — all estimation logic (ridge solver,
  variant registry, recalibration constants) can change without tripping validation
  (`validate_macro_probabilities_v2.py:388-390`, `build_macro_regime_v2_decision.py:544-547`).
- **Ops guard passes `regime_source: v2` unconditionally** (`validate_macro_h1_operations.py:362-364`)
  despite the documented v1-until-promotable policy.
- **No config/builder drift check between validation and promotion runs**; `validation.json`
  acceptance is an unsigned JSON (`validate_macro_regime_v2_promotion.py:301-326`).
- **Narrow `--start-date` reruns leave stale v2 daily rows** under the same model_version
  (`build_macro_probabilities_v2.py:764-771`), which H1 and promotion joins consume as current.
- **v2 gates ignore label overlap/autocorrelation** (yoy labels at monthly cadence; point
  thresholds on ~1/3-effective samples) — H1 got block-bootstrap gates (A2.7), the v2 family
  that feeds it did not.
- **H1 quadrant pairing is exact-date** (`validate_macro_h1_promotion.py:322-341`), so
  `QUADRANT_MIN_PAIRED_OUTCOMES=12` binds ~mid-2029, contradicting spec A1.3 arithmetic and the
  ops "late-2028" message. Coverage-gate denominator is eligible-rows-only, so capture outages
  vanish from the ≥95% gate.

### Stage 9–11 fit layers
- **Stage 9 coverage/carry-forward is vacuous.** `build_macro_industry_fit.py:633-638,702-722`:
  `merge_asof` with no tolerance carries arbitrarily stale regime/shock context onto weekly
  dates, and coverage_flag is 1 for every date after the first covered row. Every downstream
  `coverage_flag=1` filter inherits this.
- **ref_area convention hazard in Stage 10** (`build_macro_country_fit.py:250,644-658`): a
  coding mismatch silently yields `local_macro_fit=0.0` with **no confidence penalty** because
  the coverage ratio is computed from a different join than the actual local scores.
- **Confidence shrinks signed fit toward 0** (`build_macro_country_fit.py:690-692,749-753`):
  below zero this *raises* uncertain bad-fit countries in the descending rank.
- **Class haircut double-applied** (weighted-sum term + unconditional fallback penalty keyed on
  the same `country_class`).
- **Stage 11 tactical merge can silently neutral-fill a whole pipeline** when
  `source_pipeline` doesn't match a rotation `SectorName`
  (`build_macro_stock_overlay.py:653-662`).
- **Stage 11 favored/adverse lift acceptance is near-tautological** — the flag is derived from a
  score that is itself an addend of the selection score (`check_macro_stock_overlay.py:159-218`).
- **Vacuous Stage 10 check:** `external_range >= 0.0` passes by construction
  (`check_macro_country_fit.py:188-196`); Stage 10/11 gates are latest-date-only (no history
  checks, unlike Stage 9).
- **Component double counting:** industry final_score already embeds sector (20%) and shock
  (via prior); Stage 11 re-adds `sector_macro_fit` and `shock_fit` with independent weights.

### Stage 12 portfolio integration
- **Cap waterfall renormalizes above the cap when infeasible.**
  `build_macro_stock_sleeve_targets.py:292-315` (duplicated in
  `build_macro_foreign_sleeve_budget.py:275-298`): if `n_active × cap < 1` every weight is
  scaled to `cap/total > cap` with no error; historical dates with few covered industries store
  cap-violating weights and only the latest date is gated.
- **Sector cap declared but never enforced in construction**; `_add_bands` un-clips the band
  upper whenever the target exceeds the cap (`build_macro_stock_sleeve_targets.py:350,467-497`).
- **Stage 12D cannot run in this tree and its gates no-op.** `run/check_macro_optimizer_integration.py`
  import `tier1_common`/`tier1_portfolio_optimizer` which don't exist under the repo root, and
  `portfolio_layer/config.yaml` lacks every key the checks read (`stock_targets.enabled` → no
  breach checks; budget csv "" → budget 0.0). The priority-review artifacts under
  `out/final_optimizer` were produced by some other environment/config.
- **Fail-open candidate selection.** `implement_stage12d_priority_order.py:224-229`: if the
  acceptance summary CSV is absent, every case is eligible and a "production candidate" is
  chosen with no gate and no warning. No freshness/run-id manifest anywhere in the 12D chain;
  `--case` subset runs overwrite the whole case summary; stale `weights_long_only.csv` from
  failed cases is silently reused.
- **IBKR session is read-only by usage but not by construction** — `ib.connect(...)` without
  `readonly=True` (`implement_stage12d_priority_order.py:270`); account NAV/positions written
  plaintext into `out/`.
- **Shadow backtest statistics are invalid:** daily-overlapping 5-day holding windows compounded
  as sequential periods (total/annual return, vol, Sharpe, drawdown all wrong;
  `run_macro_shadow_backtest.py:406-423`); final ~5 signals' exit prices NaN'd by the freshness
  blanking then counted as cash (323 + adapter 247-249); zero transaction costs; missing/delisted
  tickers earn the cash return; benchmark is 100% SPY vs a book with a fixed 20% cash sleeve.
- **Shadow backtest reads config keys that don't exist** (`universe.max_us_stocks_long_only`,
  `allocation.region_budgets.CASH.max`, `cash.annual_yield`) — quotas silently degenerate to
  top-N and cash yield is 0.

---

## 3. MEDIUM/LOW — notable others (abbreviated)

- `macro_raw_config.py:26-31` returns `{}` on missing/malformed config — everything then runs on
  hard-coded defaults silently.
- Country coverage: `coverage_flag` vacuously 1 when a country has zero required metrics;
  `stale_metric_count` conflates never-had-data with stale; ref_area join fail-open to zero.
- `00_validate_macro_layer_foundation.py`: Stage-7 contract check gates only column presence and
  row_count>0 — a year-stale serving DB passes; `audit_macro_v2_vintage_gaps.py:794` hardcodes
  `audit_status: "PASS"`.
- Weekly/daily lag transforms count observations, not elapsed time (yoy on gappy weekly series
  drifts horizon); monthly lag lookup assumes first-of-period observation dates.
- `carry_forward_flag` in `macro_feature_daily` is silently redefined (event-recency, not PIT
  carry-forward); composites consume it believing PIT semantics.
- Feature/composite policy drift fails silently (left-join → permanent
  `missing_standardized_value`, composites keep publishing at 0.40–0.50 coverage thresholds).
- All non-US features are built daily and consumed by nothing (compute with zero downstream use).
- ADS vintage-header parse: mmddyy/yymmdd ambiguity unverified; zero matched columns returns an
  empty frame with no error.
- CFNAI backfill silently skips unrecoverable months (ICSA backfill correctly fails closed).
- Retry-After uncapped in `macro_http.py`; OECD bundle cache defeated by per-spec
  `observation_start` (up to ~70 min/run at min_interval 75s); `_BundleFailure` cached for the
  whole run suppresses sibling retries.
- v1 config/code default drift (`ridge_penalty` 1.0 vs 2.5; floor 0.005 vs 0.02) — config wins,
  defaults mislead.
- Dead config keys `pi_now/pi_lead_min_prospective_outcomes` (validator uses frozen constants).
- `check_macro_stock_overlay` demands 100% coverage at latest date (brittle in the opposite
  direction); `sector_tactical_enabled=false` + `missing_policy=strict` always crashes.
- Duplicated `SHOCK_FEATURE_SPECS` across Stages 9/10 whose correctness depends on upstream
  double-negation of sign_multiplier — change one side and the other silently inverts.
- 12A canonical-universe inner merge silently shrinks the exported optimizer universe; fallback
  branch emits a different schema than the merge branch.
- `staging_portfolio_adapter.py:167-174` accepts the latest survivorship panel without checking
  `acceptance == "PASS"`.

Verified-sound items worth keeping (do not "fix"): v1 expanding-window calibration and
label-availability gating are PIT-clean; regime state vocabulary/order is consistent across all
layers; ridge penalty excludes intercept and per-window scaling is PIT-safe in v2; walk-forward
label cutoffs are enforced in code and SQL; H1 ledger is genuinely append-only hash-chained;
composite weight renormalization and missing-component exclusion are correct; 12A/12B/12C
builders are idempotent with atomic CSV writes; v2_1/v2_2/v2_3 are single-sourced (no
copy-paste divergence); `implement_stage12d_priority_order.py` places no orders.

---

## 4. Enhancements — better estimation of current & future conditions

Ordered by expected impact on estimation quality:

1. **Fix the vintage foundation first (C1–C3).** Every model improvement is confounded until
   first-print/vintage data is trustworthy. Add the PIT invariant validator (post-build check:
   availability ≤ as_of, no fabricated vintage concentrations, immutable served history).
2. **Time-grid standardization.** Compute z/percentiles on a resampled per-period series (one
   value per observation period, latest vintage as-of date), not the event stream — makes
   windows comparable across metrics and removes revision-frequency bias.
3. **Real surprise & revision features.** First prints are already partially captured
   (CFNAI/ICSA backfills): build `surprise = first_print − PIT expectation` (AR(1)/random-walk,
   or consensus if a source is added), a Citi-style rolling surprise index, and
   mean-absolute-revision reliability weights per metric (replacing the constant
   source_quality multiplier).
4. **Diffusion indices per composite** (fraction of components > 0 / > ±1σ) — robust to the
   SHOCK sign-cancellation problem and nearly free given
   `macro_composite_component_daily` exists.
5. **Replace product-of-marginals with a joint model.** The 4-state regime probability is
   currently `P(G)×P(π)` under independence (the known v1 weakness). Options in increasing
   ambition: (a) bivariate logistic / Gaussian-copula on the two logits; (b) multinomial ridge
   logistic on realized quadrants; (c) a proper Hamilton-filter Markov-switching model or HMM
   estimated expanding-window on monthly composite observations — this also gives coherent
   filtered probabilities, principled smoothing (replacing the ad-hoc blend), and fixes the
   next-3m horizon problem via `T^h` or semi-Markov durations.
6. **Nowcast fusion.** A small mixed-frequency dynamic factor model (Kalman filter) over
   first-print vintages (CFNAI, claims, ADS are ideal inputs) feeding the regime filter —
   principled outage handling (posterior variance grows instead of silent matrix propagation)
   and a true "current conditions" estimate between releases.
7. **PIT-safe hyperparameter selection + ensembling.** Choose ridge λ per calibration date by
   expanding-window CV (seal the chosen λ in the model row); ensemble V1/V2.1/V2.2/V2.3 by
   trailing log-loss-weighted logit averaging as a pre-registered candidate instead of serially
   auditioning single variants against the same OOS window.
8. **Autocorrelation-honest gates.** Port H1's seeded block bootstrap into the v2 promotion
   gates (paired Brier-difference CIs, block length matched to label overlap); count effective
   (non-overlapping) samples in `calibration_min_months` and OOS minimums.
9. **Decision-layer hardening.** Gate-checked initialization; incumbent staleness failsafe
   (force re-evaluation if the active regime's probability < floor for N weeks); decayed rather
   than hard-reset pending counters; nearest-business-day roll for uncovered decision Fridays;
   bounded-staleness context joins (merge_asof tolerances) in Stage 9.
10. **Uncertainty quantification surfaced downstream.** Publish credible intervals / bootstrap
    bands on regime probabilities and gate regime *switches* on interval separation; expose a
    single scalar "macro confidence" that downstream sizing can consume (see §5).

## 5. Using the estimates to improve investment selection

Currently consumed as: sector/industry fit tilts (bounded budget shifts), regime→gross-exposure
scalar, foreign sleeve activation/budget, and BL view confidence. Additional uses, roughly in
order of implementation cost:

1. **Transition-probability tilts (cheapest, data already exists).** Stage 9/10 condition only
   on the current smoothed state (Stage 10 uses a fixed 70/30 now/next split). Weight the
   industry×regime prior map by the full one-step-ahead regime distribution (after fixing the
   T^h horizon bug) — anticipates rotations, reduces turnover at regime boundaries, and uses
   `p_next_3m` which is currently computed then barely used.
2. **Regime-conditional expected returns as BL views.** Replace the affine
   `final_score = 50 + 10×selection_score` rescale with expected-return priors from historical
   regime-conditional sector/industry return distributions (estimated walk-forward, shrunk
   hierarchically industry→aggregate→sector→global). This turns the macro layer from a ranking
   perturbation into calibrated return views the optimizer can trade off against risk.
3. **Empirical macro betas from returns.** Rolling PIT ridge regressions of industry/country/
   stock excess returns on the shock-factor series (dollar, energy, real yield, credit, NFCI)
   to replace keyword-rule exposures; blend with hand priors by estimation precision. Gives
   stock-level macro sensitivity for selection (avoid high-beta-to-dollar names when the dollar
   composite is spiking) rather than industry-bucket approximations.
4. **Macro-uncertainty position sizing.** Use regime confidence/entropy to scale gross exposure
   continuously (replacing the coarse per-regime scalar table) and to modulate single-name
   concentration: high regime entropy → smaller active bets, tighter caps; high-confidence
   regimes → allow fuller expression of the fit tilts.
5. **Drawdown/shock gate.** Wire `shock_fit` + contraction/stagflation transition probability
   into a de-risking rule that raises cash and shrinks the foreign budget before the regime
   label flips (labels lag by construction due to hysteresis; the transition probabilities move
   first).
6. **Vol targeting with regime-conditional covariance.** Estimate per-regime covariance scaling
   (vol is regime-dependent) and target portfolio vol instead of a fixed 20% cash sleeve — the
   cash weight becomes an output, not an input.
7. **Regime-conditional strategy allocation.** Use the disagreement between v1/v2/H1 (currently
   84.8% and treated purely as promotion evidence) as a *signal*: high model disagreement =
   ambiguous macro state = tilt toward stock-specific alpha (your sector sleeves' bottom-up
   scores) and away from macro tilts; agreement = lean into the macro overlay. This makes the
   shadow models useful before promotion.
8. **Sleeve-specific macro conditioning.** Map composites to sleeve-relevant channels: real
   yields/funding conditions → biotech and unprofitable-growth cohorts (your calibration gates
   already show 2025 selection alpha broke down — regime-conditioning the *gate* may explain
   when the sleeve model works); dollar + global PMI → semis/hardware; energy + freight →
   transportation; claims/ISM → machinery. Use macro fit to gate sleeve activation rather than
   only tilting weights.
9. **Cost-aware no-trade bands.** Derive Stage 12B band widths from the existing
   `transaction_costs` config so macro-driven rebalancing only trades when the expected tilt
   benefit clears round-trip costs; add the same cost model to the shadow backtest so
   macro-vs-baseline deltas are net-of-cost (currently gross, which biases toward macro).
10. **Event-window risk control.** The release calendar metadata already exists: shrink new
    position entries or widen stops around top-tier release timestamps (CPI, FOMC, payrolls)
    for the short-horizon scalper sleeves, and schedule rebalances after, not before,
    high-impact releases.

## 6. Suggested remediation order

1. C4 (rotate/remove keys — 30 minutes, do first), then C1→C2→C3 with a fresh full-vintage
   backfill into a clean raw DB (backups already exist under `backups/`).
2. Fail-closed plumbing: connector error counting, QA exit codes, serving-DAG step ordering,
   PIT invariant validator, 12D freshness manifest + acceptance-gate requirement.
3. v1 engine fixes (INITIALIZE gates, incumbent failsafe, T^h horizon, event-vs-time windows)
   — these change live shadow behavior, so re-run the stress-window checks after.
4. Promotion-integrity fixes (hash coverage, baseline reconciliation, block-bootstrap v2 gates).
5. Then the estimation enhancements (§4) and selection integrations (§5), which are only
   meaningful once 1–4 hold.
