# Macro Regime V2.4 Candidate — Frozen Pre-Registration Spec

**Status: FROZEN as of 2026-08-04, prior to any implementation, feature build, evidence run, or
gate test for this candidate. NOT IMPLEMENTED.** The estimation code and configs are hash-sealed;
every code/config change listed below is deferred to a single governance event (see
Preconditions). No component, transform, sign, join rule, threshold, or history-start below may
change after this date without creating a new candidate version.

Successor context. V2.3 closed the recalibration campaign as its own spec required: sealed
2026-07-17 evidence was NOT_PROMOTABLE (2/4 cells; P_PI_NOW and P_PI_LEAD VALIDATED, both growth
cells REJECTED; `out/regime_v2_3/2026-07-17/macro_regime_v2_promotion_summary.json`). V2.4 opens
a NEW campaign with a NEW information family rather than re-tuning the old one: PIT first-print
surprise indices from the deferred-research program (`SURPRISE_FACTOR_CANDIDATE_SPEC.md`),
addressing the 2026-08-02 audit finding that "the layer has no surprise/first-print/momentum
features at all" (`MACRO_AUDIT_FINDINGS_2026-08-02.md`). The candidate targets the still-rejected
growth cells, where the diagnostic signal lives.

## Multiplicity and selection disclosure

1. This is the fourth v2-family candidate whose walk-forward gates score an overlapping
   historical OOS window (after V2.1/V2.2/V2.3, disclosed per the V2.3 convention). The
   new-campaign justification is that the delta is a new information source (actual-vs-expected
   first-print surprises), not a re-tune of existing knobs: the ridge penalty is inherited, no
   threshold moves, no recalibration-policy change, no composite edits.
2. The inclusion/sign/exclusion decisions below were SELECTED on full-window diagnostic
   correlations from the sealed research build (max_availability_date 2026-07-31,
   `out/surprise_research/2026-07-31/manifest.json`, builder sha256
   `19d3fa3ab1bcd6c795590dba19fd1b880c9237d2eca7849385d373c493709e31`). That diagnostic window
   overlaps the gate OOS window; this is pre-test feature selection and is disclosed as such.
   Mitigation, not absolution: the diagnostics target forward changes of the SERVED composites,
   not the first-release outcome labels the gates score; no label-target evidence has been run
   for this candidate; all decisions are frozen here before any such run.
3. Required ablation evidence: V2.4 vs V2.2 as a row-for-row paired comparison (identical
   training and OOS rows by construction — see Availability/PIT), paired Brier difference with
   the seeded circular block bootstrap per `SURPRISE_FACTOR_CANDIDATE_SPEC.md`.

## Identity

| Field | Value |
|---|---|
| model_version | `macro_regime_v2_4_surprise_v1` |
| config block | `probability_v2_4:` (DRAFT in this spec; **NOT APPLIED** — governance event) |
| shadow_only | `true` (hard-required by builder; never writes production surfaces) |
| output_dir | `MacroLayer/out/regime_v2_4` (own dir; artifact paths are date-keyed and would collide otherwise) |
| history_start_date | `2001-01-01` |
| probability keys | unchanged: `P_G_NOW_V2`, `P_G_LEAD_V2`, `P_PI_NOW_V2`, `P_PI_LEAD_V2` (model_version separates candidates in every table PK; keeping keys preserves comparison joins) |
| base | **V2.2** — V2.1 composites (G_NOW, G_LEAD_V21, PI_NOW_V21, PI_LEAD, SHOCK), V2.1 feature metrics and financial_conditions recipe, plus V2.2's unconditional trailing PIT recalibration (min 20 resolved pairs, ≥5 positive, ≥5 negative; unready fit or non-positive slope passes raw through) |
| targets / labels / thresholds / gates | identical to V2 through V2.3 — no gate is relaxed, none added |
| ridge_penalty | **5.0, inherited. Re-tuning ridge_penalty for this candidate is FORBIDDEN** (any change is a new candidate version). This keeps V2.4 vs V2.2 attributable to the added predictors alone. |

Base is V2.2, deliberately NOT V2.3: V2.3's conditional-pooled latching is a separate mechanism
whose interaction with new predictors would confound attribution. Against V2.2, V2.4 is a pure
predictor ablation. (Whether surprise information belongs in the recalibration layer instead is
Open Question 3 — for a later candidate, not this one, and not retroactively.)

## Deltas vs V2.2 (exactly one delta class)

Three predictors are added to the GROWTH predictor tuple (both growth cells, `P_G_NOW_V2` and
`P_G_LEAD_V2`), provisioned by one new as-of join in predictor-panel assembly. Everything else is
byte-identical to V2.2: inflation predictor tuples, ALL mandatory-predictor sets, composites,
feature metrics, the financial_conditions recipe, recalibration policy and constants, training
minimums, thresholds, gates, and the walk-forward protocol.

### Added predictors (frozen)

| # | Predictor name | Source (`macro_surprise_research.sqlite`) | Entry | Expected coefficient sign | Basis |
|---|---|---|---|---|---|
| 1 | `surprise_index_growth_lead` | `surprise_index_daily` where `regime_block='growth_lead'` | as-is | positive (continuation) | r=+0.1420 vs G_LEAD forward-21d change, n=5322, window 2011-11-16 → 2026-07-13 |
| 2 | `surprise_index_growth_now` | `surprise_index_daily` where `regime_block='growth_now'` | **as-is (NOT sign-flipped)** | **negative (mean reversion)** | r=−0.1070 vs G_NOW forward-21d change, n=5929, window 2009-11-19 → 2026-07-13 |
| 3 | `surprise_dispersion` | derived: `(abs(surprise_index_growth_lead) + abs(surprise_index_growth_now)) / 2`, computed AFTER the join and zero-fill | derived | none pre-registered | uncertainty / net-surprise-magnitude feature |

**Why growth_now enters as-is rather than sign-flipped.** Two frozen reasons:

1. *Mechanically inert.* `fit_ridge_logistic` z-scores its design matrix in-window and the ridge
   penalty is symmetric, so negating a predictor column produces the identical probability path
   with a mirrored coefficient. A sign flip cannot change any model output; it would only create
   a cosmetic divergence between the stored research series and the model feature, complicating
   provenance for zero statistical benefit.
2. *The negative correlation IS the information, pre-registered as mean reversion.* The G_NOW
   composite's membership largely coincides with the underlying series of the growth-now surprise
   metrics (claims, payrolls, CFNAI, ADS, production/sales blocks). By the time a positive
   surprise run has accumulated in the index, the served composite has already absorbed the level
   news; the forward 21-day composite change then tends to fade the run. The EXPECTED fitted
   coefficient is therefore negative, and evidence diagnostics must report it so a sign
   inconsistent with this frozen hypothesis is visible at review, not explained after the fact.

**`surprise_dispersion` definition note.** The absolute value of a decay-weighted surprise sum
measures the magnitude of net directional surprise flow, not cross-metric dispersion in the
strict sense; the name follows the research program's usage and the definition above is frozen to
remove ambiguity. A true cross-sectional dispersion (std of per-metric `surprise_z` per day)
would require a new upstream table and is deliberately out of scope. No coefficient sign is
pre-registered: it is admitted as a variance-regime proxy and the ridge penalty shrinks it if
uninformative. It is computed from the two ADMITTED indices only (see exclusion below).

### Exclusion: inflation_now surprise index (frozen)

`surprise_index_inflation_now` is EXCLUDED from V2.4 entirely — as a directional predictor and
from the dispersion feature. Basis: r=+0.0081 vs PI_NOW forward-21d change (n=4791, window
2013-02-01 → 2026-07-13) — indistinguishable from zero. Admitting a predictor with no diagnostic
signal adds estimation variance with no pre-registered hypothesis, and keeping it out of the
dispersion term keeps the r≈0 series fully outside V2.4's feature surface (any effect it produced
would be untraceable to a frozen direction). The inflation predictor tuples are byte-identical to
V2.1/V2.2/V2.3 for the same reason: both inflation cells already VALIDATED in V2.2 and V2.3, the
surprise diagnostics offer them nothing, and un-hypothesized features risk degrading working
cells.

### Diagnostic provenance (recorded, not a gate)

| regime_block | composite | forward window | n_obs (daily, overlapping) | pearson_r | window |
|---|---|---|---|---|---|
| growth_lead | G_LEAD | 21 calendar days | 5322 | +0.1420 | 2011-11-16 → 2026-07-13 |
| growth_now | G_NOW | 21 calendar days | 5929 | −0.1070 | 2009-11-19 → 2026-07-13 |
| inflation_now | PI_NOW | 21 calendar days | 4791 | +0.0081 | 2013-02-01 → 2026-07-13 |

Caution recorded pre-test: these are daily observations of overlapping 21-day forward windows
(~21× overlap), so the effective sample is on the order of 250–280 per block, not ~5×10³. See
Open Question 2. These correlations carry no gate; the gates below are unchanged.

## Availability / PIT contract

1. **Upstream (already enforced, no change).** Every surprise value's availability equals the
   vintage date of its underlying FIRST PRINT; `build_macro_surprise_factors.py` enforces this
   fail-closed (expectation windows restricted to strictly-earlier availability, PIT assertions,
   true-vintage-registry restriction). `surprise_index_daily` rows at `as_of_date` d are
   decay-weighted (half-life 60 calendar days) sums of standardized surprises available on or
   before d. No re-derivation happens in the v2.4 builder; it consumes the table as-is.
2. **Join (frozen).** In the v2.4 builder's predictor-panel assembly (the `_load_predictors`
   stage), for each prediction date d in the daily panel, each block's feature takes the value of
   the LATEST `surprise_index_daily` row with `as_of_date <= d` (as-of BACKWARD join — never a
   forward row). Staleness is `np.busday_count(as_of_date, d)` with the default Mon–Fri weekmask
   and no holiday calendar (deterministic, environment-free). If staleness exceeds
   **10 business days**, or no row exists at or before d: the feature is set to **0.0** and the
   panel row's `surprise_stale_flag` is set to 1. `surprise_dispersion` is computed after this
   fill (stale/absent → 0 = "no measured surprise pressure").
3. **Zero is the neutral value, not an imputation hack.** The index is a decayed sum whose
   no-information limit is exactly 0; zero-fill states "no net surprise information", which is
   also the truthful description of pre-index history (the indices only exist from ~2009–2011).
4. **The flag is metadata, never a predictor.** `surprise_stale_flag` (1 if ANY of the two block
   joins zero-filled at that date) is carried on the panel, exported with diagnostics, and
   summarized in the run manifest (`surprise_zero_filled_rows`, per-block counts, and
   `surprise_max_staleness_business_days_observed`). It MUST NOT enter any predictor tuple:
   pre-index history would make it a pure calendar-era dummy — an overfitting channel with no
   macro content.
5. **Panel-depth invariance (the paired-comparison guarantee).** Mandatory-predictor sets are
   unchanged (growth: `G_NOW`, `G_LEAD_V21`; inflation: `PI_NOW_V21`, `PI_LEAD`,
   `inflation_level_yoy`) and `predictor_complete_flag` tests mandatory predictors only. Since
   zero-fill makes the three new columns always non-null, training-row selection, model-readiness
   dates, and the OOS row set are IDENTICAL to V2.2's. Every V2.4-vs-V2.2 comparison is paired
   row-for-row by construction. Early training windows where a surprise column is entirely zero
   are safe: the fitter's zero-variance guard (`_column_stats`, std ≤ 1e-8 → divisor stays 1.0)
   yields a scaled zero column and the ridge penalty pins its coefficient at ~0.

## Frozen estimation parameters and evidence gates

Estimation: ridge logistic per cell, `ridge_penalty = 5.0` (inherited — re-tuning forbidden, see
Identity), `output_probability_floor = 0.02`, training minimums growth 40 / inflation 60 / lead
40 with ≥8 positive and ≥8 negative samples, monthly calibration cadence, V2.2's unconditional
trailing recalibration with its frozen readiness constants (≥20 resolved pairs, ≥5 positive, ≥5
negative; unready fit or non-positive slope passes raw probabilities through; the stored
`probability_value` IS the recalibrated probability).

Evidence gates — byte-identical to the `probability_v2` / `v2_1` / `v2_2` / `v2_3` evidence
blocks; no gate is relaxed, none added:

| Gate | Threshold |
|---|---|
| growth_min_oos_samples | 24 |
| inflation_min_oos_samples | 60 |
| inflation_lead_min_oos_samples | 16 |
| minimum_oos_class_samples | 6 positive AND 6 negative, per cell |
| minimum_auc | 0.52 |
| minimum_brier_skill | 0.0 |
| minimum_brier_improvement_vs_v1 | 0.0 |
| calibration slope band | [0.50, 1.50] — corrected (de-standardized) Platt metric per the V2.2 disclosure; all candidates scored identically |
| decision_min_top_probability | 0.50 |
| decision_min_confidence | 0.10 |

## Walk-forward protocol (unchanged)

Identical machinery to V2.2: monthly calibration dates (last panel row per calendar month);
training pairs restricted to target rows with `label_available_date <= C` and mandatory
predictors complete; each model applied to daily rows in [C, next C); mandatory-predictor
completeness gates `coverage_flag`; trailing recalibration fitted per V2.2's frozen description
on pairs resolved at or before C. Evidence is produced by the unchanged
`validate_macro_regime_v2_promotion.py` machinery, model_version-scoped, with V2.4 evaluated
side-by-side against deployed V1 and shadow V2/V2.1/V2.2/V2.3 under identical thresholds.

## Preconditions (governance events; ALL required before any evidence run)

1. **Surprise builder promoted from research one-off to a scheduled step.**
   `build_macro_surprise_factors.py` becomes a scheduled pipeline step ordered strictly BEFORE
   the v2.4 probability build on every run day. The source of record remains
   `MacroLayer/macro_surprise_research.sqlite::surprise_index_daily`; promotion of surprise rows
   into `macro_feature_daily`/composite policy remains OUT of scope for this candidate (per the
   research spec's adoption path, that step follows a passed gate and its own amendment). In
   normal operation the scheduled ordering keeps `surprise_zero_filled_rows` at 0 for current
   dates; the manifest counters (contract item 4) make any scheduling regression visible.
2. **Regression tests for the as-of join** (added under `tests/`, run in CI before the
   governance commit merges). Required cases: exact-date match (staleness 0); backward match
   within window; staleness boundary (exactly 10 business days passes; 11 fills with flag);
   empty/missing table → all rows 0 + flag; NO forward leakage (a surprise row dated d+1 must
   never affect date d); weekend/holiday `busday_count` arithmetic; dispersion computed after
   fill; and a panel-depth invariance test asserting V2.4's training/OOS row sets equal V2.2's.
3. **Hash-seal additions to the v2.4 manifest.** The v2.4 build manifest must include
   `surprise_builder_sha256` (`build_macro_surprise_factors.py`) and `probability_engine_sha256`
   (`macro_probability_v2.py`), plus the input research DB's file sha256 and the referenced
   research manifest's path+sha256; `validate_macro_probabilities_v2.py` and the promotion
   validator verify all of them fail-closed. This closes the 2026-08-02 audit's hash-coverage gap
   ("the v2 seal does not hash `macro_probability_v2.py`") for this candidate and extends seal
   coverage to the new upstream surprise builder, so no estimation or surprise-construction logic
   can change without tripping validation.
4. **Registry + config governance event.** Add the `MODEL_VERSION_V24` variant entry to
   `PROBABILITY_V2_VARIANTS` and the `probability_v2_4` config block exactly as drafted below, in
   a single reviewed commit, with all seals refreshed. Any deviation from the drafts amends this
   spec FIRST (new candidate version if the deviation is substantive).

## Draft variant-registry entry (illustrative sketch — NOT APPLIED)

```python
MODEL_VERSION_V24 = "macro_regime_v2_4_surprise_v1"

GROWTH_PREDICTORS_V24 = GROWTH_PREDICTORS_V21 + (
    "surprise_index_growth_lead",
    "surprise_index_growth_now",
    "surprise_dispersion",
)
# Growth cells use GROWTH_PREDICTORS_V24; inflation cells reuse INFLATION_PREDICTORS_V21.
# Mandatory predictor sets are UNCHANGED. Variant mirrors MODEL_VERSION_V22
# (recalibrate=True, policy "always") plus new frozen fields, all defaulted so every
# existing variant remains byte-identical in behavior:
#   surprise_feature_blocks = (("surprise_index_growth_lead", "growth_lead"),
#                              ("surprise_index_growth_now", "growth_now"))
#   surprise_dispersion_feature = "surprise_dispersion"
#   surprise_max_staleness_business_days = 10
```

## Draft config block (NOT APPLIED — mirrors `probability_v2_2`; do not commit outside the governance event)

```yaml
  # V2.4 candidate (frozen spec: MacroLayer/V2_4_CANDIDATE_SPEC.md): V2.2 base + surprise-factor
  # predictors on the growth cells, provisioned by a staleness-bounded as-of join. Identical
  # thresholds; own output_dir. DRAFT — NOT APPLIED.
  probability_v2_4:
    enabled: true
    shadow_only: true
    model_version: "macro_regime_v2_4_surprise_v1"
    history_start_date: "2001-01-01"
    output_dir: "MacroLayer/out/regime_v2_4"
    growth_resilient_qoq_ann_threshold: 0.02
    inflation_pressure_yoy_threshold: 0.025
    minimum_inflation_components: 4
    growth_min_training_samples: 40
    inflation_min_training_samples: 60
    lead_min_training_samples: 40
    minimum_positive_samples: 8
    minimum_negative_samples: 8
    ridge_penalty: 5.0            # inherited from V2.1/V2.2/V2.3 — re-tuning forbidden (spec)
    output_probability_floor: 0.02
    energy_shock_z_threshold: 1.5
    energy_shock_yoy_threshold: 0.25
    decision_min_top_probability: 0.50
    decision_min_confidence: 0.10
    surprise:
      db_path: "MacroLayer/macro_surprise_research.sqlite"
      table: "surprise_index_daily"
      join: "asof_backward"
      max_staleness_business_days: 10
      stale_fill_value: 0.0
      stale_flag_column: "surprise_stale_flag"
      blocks:
        growth_lead: "surprise_index_growth_lead"
        growth_now: "surprise_index_growth_now"
      dispersion_feature: "surprise_dispersion"   # mean(|growth_lead|, |growth_now|), post-fill
    vintage_audit:
      preferred_history_start_date: "2001-01-01"
      probe_fred_by_default: false
    evidence:
      growth_min_oos_samples: 24
      inflation_min_oos_samples: 60
      inflation_lead_min_oos_samples: 16
      minimum_oos_class_samples: 6
      minimum_auc: 0.52
      minimum_brier_skill: 0.0
      minimum_brier_improvement_vs_v1: 0.0
      minimum_calibration_slope: 0.50
      maximum_calibration_slope: 1.50
```

## Open questions (recorded pre-test; resolvable only by evidence or a successor spec, never by editing this one)

1. **2025 ablation concentration dependency.** The diagnostic window ends 2026-07-13 and includes
   the surprise-rich 2025 segment; the r=+0.142 / r=−0.107 signals may be episode-driven. The
   ablation evidence (V2.4 vs V2.2 paired Brier) MUST include a per-calendar-year decomposition.
   Concentration in 2025 alone does not fail a gate (gates are unchanged), but it is a recorded
   promotion-review consideration: a gain that exists only in one macro episode is weaker
   evidence than the same aggregate gain spread across regimes, and the review must say which one
   was observed.
2. **Effective-sample overlap.** The daily diagnostics overlap ~21× (effective n ≈ 250–280 per
   block, not ~5×10³), and at gate level the audit already flagged that v2 gates ignore label
   overlap/autocorrelation (quarterly-yoy labels at monthly cadence) while H1 received seeded
   circular block-bootstrap gates. Open: should the V2.4 ablation CIs (and eventually the
   v2-family gates) be computed under that same block bootstrap, and should it become binding for
   this campaign? This spec keeps the gates byte-identical — adding a binding bootstrap gate is
   itself a governance change and must be decided BEFORE the evidence run if it is to bind.
3. **Recalibration-layer alternative for the growth_now term.** The mean-reversion content of
   `surprise_index_growth_now` might belong in the recalibration layer (e.g., a surprise-run-aware
   adjustment of the trailing intercept) rather than in the base model, where it competes with
   correlated level composites for ridge mass and could destabilize the G_NOW coefficient across
   windows. This candidate freezes base-model membership; if evidence shows an unstable or
   sign-flipping G_NOW surprise coefficient alongside a failed gate, a successor candidate may
   move the term — not this one, and not retroactively.

## Promotion rule (unchanged)

Promotion requires EVERY one of the four cells to pass sample-size, class-balance, AUC,
Brier-skill, and calibration gates, plus the decision-confidence gates, in sealed evidence
produced by the unchanged machinery under the corrected Platt metric — fail-closed, no partial
promotion, no gate edits. Promotion to `macro.regime_source` additionally requires a protocol
amendment; this spec does not itself authorize promotion, and per campaign discipline V2.4 gets
one shot at the gate as frozen here.
