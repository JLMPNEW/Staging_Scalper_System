# Macro Regime V2.3 Candidate — Frozen Pre-Registration Spec

**Status: FROZEN as of 2026-07-19, prior to any evidence run for this candidate.**
Successor to V2.2. Motivation from V2.2's sealed 2026-07-08 evidence (corrected Platt metric):
unconditional trailing recalibration VALIDATED both inflation cells (PI_NOW 1.79→1.30, PI_LEAD
0.69) but DEGRADED the sparse quarterly growth cells (G_NOW raw slope 0.53 in-band was pushed to
0.28 by noisy ~quarterly fits; G_LEAD 0.25 → −0.11). G_LEAD now passes AUC (0.535≥0.52) and
Brier skill (+0.032≥0) raw and fails ONLY on over-confidence (slope 0.25).

**Multiplicity disclosure:** this is the third candidate evaluated against the same OOS window
(after V2.1, V2.2). It is the final iteration of this campaign; if it fails its gates, the
campaign stops with no promotion until new OOS accrues.

## Identity

| Field | Value |
|---|---|
| model_version | `macro_regime_v2_3_conditional_recal_v1` |
| config block | `probability_v2_3:` |
| shadow_only | `true` |
| output_dir | `MacroLayer/out/regime_v2_3` |
| history_start_date | `2001-01-01` |
| inputs (composites, predictors, feature metrics) | identical to V2.1/V2.2 |
| targets / labels / thresholds / gates | identical — no gate is relaxed |

## Delta vs V2.2 (exactly one: the recalibration POLICY)

At each calibration date C, per probability cell:

1. **Pair pool.** Growth cells (P_G_NOW_V2, P_G_LEAD_V2) POOL their resolved
   (raw walk-forward prediction, label) pairs into one growth pool; each inflation cell uses
   its own pairs. Pooling exists to give the sparse quarterly cells sample mass; the two
   growth cells share the same predictor family and outcome definition, so a shared
   miscalibration structure is the frozen hypothesis.
2. **Conditional LATCHED trigger (cell-level) with pooled correction.** The TRIGGER is the
   cell's OWN trailing raw Platt slope (fitted on the cell's own resolved pairs, labels ≤ C;
   an unready fit leaves the latch unchanged). Windows are processed in chronological order;
   the FIRST window whose own trailing slope lies OUTSIDE [0.5, 1.5] (fit ready) latches
   recalibration ON, and it remains ON for all subsequent windows — per-window toggling is
   forbidden because a mixture of corrected (narrow-logit) and raw (wide-logit) windows lets
   the raw windows dominate the single OOS calibration fit and defeats the correction
   (mechanism established on synthetic data before any evidence run; amended pre-test).
   The CORRECTION LINE applied is fitted on the cell's POOL (growth pool for growth cells),
   subject to readiness (≥20 pairs, ≥5 positive, ≥5 negative, slope > 0); an unready pool fit
   passes that window through raw. Never-latched cells keep raw probabilities everywhere.
   The trigger band equals the gate band and is frozen here.
3. Application form unchanged: `p = clip(sigmoid(a + b·logit(p_raw)), floor, 1−floor)`.

Expected mechanics (stated pre-test): G_NOW trailing raw slope ~in-band → mostly untouched →
retains its V2.1-raw behavior; G_LEAD out-of-band → corrected with pooled (~2× deeper) pairs;
PI_NOW out-of-band → corrected as in V2.2; PI_LEAD ~in-band → mostly untouched.

## Promotion rule (unchanged)

All four cells must pass sample-size, class-balance, AUC, Brier-skill, and calibration gates in
sealed evidence, evaluated by the unchanged machinery against V1 under the corrected Platt
metric (metric fix disclosed in V2_2_CANDIDATE_SPEC.md; band [0.5,1.5] untouched; all
candidates re-scored identically). Fail-closed; no partial promotion.
