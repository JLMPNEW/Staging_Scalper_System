# Macro Regime V2.1 Candidate — Frozen Pre-Registration Spec

**Status: FROZEN as of 2026-07-19, prior to any feature build, evidence run, or gate test.**
No component, transform, sign, weight, threshold, or history-start below may change after this
date without creating a new candidate version. Written per the V2.1 guardrails:

1. V2.1 is a NEW model version; shadow V2 (`macro_regime_v2_independent_outcomes_v1`) and
   production V1 remain byte-identical in behavior and are used as benchmarks.
2. All-employee AHE is replaced by production/nonsupervisory AHE across the ENTIRE V2.1
   history — one series definition, no splicing.
3. NFCI is replaced by a pre-registered market-based stress block using only series proven
   available on each historical date; HY OAS is NOT duplicated (it remains a separate,
   pre-existing G_LEAD component).
4. Components/transforms/signs/weights/history-start are frozen here, before testing.
5. PIT feature history is rebuilt, then all four outcome-cell promotion gates rerun.
6. V2.1 is compared against deployed V1 AND shadow V2 under the same thresholds.
7. Promotion requires EVERY required cell to pass sample-size, class-balance, AUC,
   Brier-skill, and calibration gates. No gate is relaxed.

## Identity

| Field | Value |
|---|---|
| model_version | `macro_regime_v2_1_independent_outcomes_v1` |
| config block | `probability_v2_1:` in `config_macro_raw.yaml` |
| shadow_only | `true` (hard-required by builder; never writes production surfaces) |
| output_dir | `MacroLayer/out/regime_v2_1` (separate from V2's `out/regime_v2` — artifact paths are date-keyed and would collide otherwise) |
| history_start_date | `2001-01-01` |
| probability keys | unchanged: `P_G_NOW_V2`, `P_G_LEAD_V2`, `P_PI_NOW_V2`, `P_PI_LEAD_V2` (candidates are separated by `model_version` in every table PK; keeping keys preserves the V2→V1 comparison joins) |
| targets / labels / thresholds | identical to V2 |
| predictor recipes | identical to V2 EXCEPT the three NFCI/AHE touchpoints listed below (frozen here, pre-test) |

### V2.1 predictor-recipe deltas (frozen)

The V2 model consumes NFCI in one place beyond the G_LEAD composite: the derived
`financial_conditions` predictor = mean standardized(`us_hy_oas`, `us_nfci`). Keeping it would
reintroduce the unpublishable pre-2011 series AND change the predictor's composition mid-history
(splicing). Frozen V2.1 deltas:

1. Predictor slots: `G_LEAD` → `G_LEAD_V21`; `PI_NOW` → `PI_NOW_V21` (in predictor tuples and
   mandatory-predictor sets). `G_NOW`, `PI_LEAD`, `SHOCK` slots unchanged.
2. `financial_conditions` (V2.1) = mean standardized(`us_hy_oas`, `us_ig_spread_baa10y`,
   `us_equity_vol`) — same aggregation, NFCI leg replaced by the pre-registered stress pair.
3. `FEATURE_METRICS` (V2.1) = V2 list − `us_nfci` + `us_ig_spread_baa10y` + `us_equity_vol`.
   All other derived predictors (`growth_activity`, `policy_tightness`, inflation recipes,
   energy recipes) are byte-identical to V2.

## Composite inputs

| Predictor slot | V2 uses | V2.1 uses | Change |
|---|---|---|---|
| growth now | `G_NOW` | `G_NOW` (reused) | membership unchanged; history completed by CFNAI + ICSA true-vintage backfill (below) |
| growth lead | `G_LEAD` | **`G_LEAD_V21`** | `us_nfci` removed (series unpublished before 2011-05 — true vintages cannot exist); `us_anfci` removed (same inception defect, optional, redundant); market-stress block added |
| inflation now | `PI_NOW` | **`PI_NOW_V21`** | `us_avg_hourly_earnings` (CES0500000003, unpublished before 2010) replaced by `us_avg_hourly_earnings_prod` (AHETPI) for the entire history |
| inflation lead | `PI_LEAD` | `PI_LEAD` (reused) | membership unchanged (TIPS-market components, `vintage_policy: none`, unrevised) |

### G_LEAD_V21 membership (18 components, equal weight 1/18)

The 16 carried-over G_LEAD components (all except `us_nfci`, `us_anfci`), plus:

| New metric | Series | Freq | vintage_policy | sign | required | Rationale |
|---|---|---|---|---|---|---|
| `us_ig_spread_baa10y` | BAA10YM (Moody's Baa − 10Y Treasury) | monthly | `none` (unrevised computation from unrevised inputs — same policy class as `us_hy_oas`) | **−1.0** | 1 | IG credit-stress dimension of financial conditions; distinct market segment from HY OAS |
| `us_equity_vol` | VIXCLS | daily | `none` (CBOE index values are never revised) | **−1.0** | 1 | Equity-volatility stress dimension; observations from 1990 |

Required set = current G_LEAD required set minus `us_nfci`, plus both new components
(`us_bci`, `us_cci`, `us_cli`, `us_hy_oas`, `us_yield_curve_10y2y` remain required as today).
A funding-stress leg (CP−T-bill, TED) is deliberately **excluded**: no single unrevised series
remains continuously published post-LIBOR, which would violate the
available-on-every-historical-AND-current-date requirement.

### PI_NOW_V21 membership (8 components, equal weight 1/8)

The 7 carried-over PI_NOW components (all except `us_avg_hourly_earnings`), plus:

| New metric | Series | Freq | vintage_policy | transform | sign | required |
|---|---|---|---|---|---|---|
| `us_avg_hourly_earnings_prod` | AHETPI (prod & nonsupervisory AHE, total private) | monthly | `true_vintage` (ALFRED vintages from 1999-08-06; observations from 1964-01) | `pct_change` yoy, lookback 12 | +1.0 | 1 |

CES0500000030 (the modern prod-worker ID) was probed and REJECTED: its ALFRED archive also
starts 2011-03. AHETPI is the old-basis ID with the deep real-time archive.

### Feature-policy parameters for new metrics (mirroring frequency peers)

| metric | feature | transform | lookback | zscore_window | min_history | sign | clips |
|---|---|---|---|---|---|---|---|
| us_ig_spread_baa10y | level | level | 0 | 60 | 24 | −1.0 | ±5.0 |
| us_equity_vol | level | level | 0 | 252 | 63 | −1.0 | ±5.0 |
| us_avg_hourly_earnings_prod | yoy_pct | pct_change | 12 | 60 | 24 | +1.0 | ±5.0 |

Registry rows: `source_name=fred_alfred`, `ref_area=USA`, `regime_block` per composite,
`history_start_date` 2000-01-01 (stress block) / 1990-01-01 (AHETPI), `enabled=1`.

## True-vintage backfills (data additions, no model change)

| Series | Metric | Gap to fill | Source of true first prints |
|---|---|---|---|
| CFNAI / CFNAIMA3 | `us_cfnai`, `us_cfnai_ma3` | vintages 2007-09 → 2011-05 (~45 monthly releases) | Chicago Fed CFNAI historical release archive |
| ICSA | `us_initial_claims` | vintages 2008-04 → 2009-05 (~60 weekly releases) | DOL ETA weekly claims press releases |

Backfilled rows are written to `macro_observation_raw` with `source_name=fred_alfred`,
`source_series_id` matching the registry, `release_date = vintage_date =` the actual
historical publication date, `revision_flag=0`, and the standard dedupe key so they merge
with (never duplicate) existing ALFRED editions.

## Gates (unchanged from V2 — no relaxation)

Promotion requires, for EVERY one of the four cells: minimum OOS samples (growth 24 /
inflation-lead 16 / inflation-now 60), minimum 6 positive and 6 negative OOS outcomes,
AUC ≥ threshold, Brier skill ≥ threshold, calibration-slope gate, plus the decision-level
confidence gates (top probability ≥ 0.50, confidence ≥ 0.10). Evidence is produced by the
same `validate_macro_regime_v2_promotion.py` machinery, model_version-scoped, and V2.1 is
evaluated side-by-side against deployed V1 and shadow V2 under identical thresholds.
Promotion to `macro.regime_source` remains fail-closed on sealed PROMOTABLE evidence and a
protocol amendment; this spec does not itself authorize promotion.
