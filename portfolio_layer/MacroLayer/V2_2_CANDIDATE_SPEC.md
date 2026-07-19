# Macro Regime V2.2 Candidate — Frozen Pre-Registration Spec

**Status: FROZEN as of 2026-07-19, prior to any evidence run or gate test for this candidate.**
Successor to V2.1 (`V2_1_CANDIDATE_SPEC.md`), motivated by V2.1's sealed 2026-07-08 evidence:
P_G_NOW VALIDATED; P_PI_NOW / P_PI_LEAD rejected ONLY on calibration slope (4.59 / 2.50 with AUC
0.952 / 0.859); P_G_LEAD rejected on merit — with a pre-2019 diagnostic showing the lead signal
exists (IG-spread r=+0.48, G_LEAD_V21 r=+0.35, n≈70 quarters) but the training panel is starved
because mandatory predictor G_NOW is only covered from 2009-08 (bounded by CFNAI vintages 2007-07
and the claims 52-week yoy lookback on vintages from 2008-03).

## Identity

| Field | Value |
|---|---|
| model_version | `macro_regime_v2_2_recalibrated_v1` |
| config block | `probability_v2_2:` |
| shadow_only | `true` |
| output_dir | `MacroLayer/out/regime_v2_2` |
| history_start_date | `2001-01-01` |
| probability keys / targets / labels / thresholds / gates | identical to V2 and V2.1 — no gate is relaxed |

## Deltas vs V2.1 (exactly three; everything else byte-identical)

1. **Trailing PIT recalibration layer (all four cells).** At each monthly calibration date C, fit
   the logistic recalibration line (existing `calibration_line`: outcome on predicted logit) on the
   pairs (raw walk-forward prediction at date d, realized label), restricted to target rows whose
   `label_available_date <= C` and whose raw prediction was produced by a model calibrated at or
   before d (walk-forward, PIT by construction). Apply to predictions in [C, next C):
   `p_recal = clip(sigmoid(intercept + slope * logit(p_raw)), floor, 1-floor)`.
   A not-ready fit or non-positive slope passes raw probabilities through.

   **Promotion-metric defect fixed alongside (disclosed):** the pre-existing
   `calibration_line` returned its slope on STANDARDIZED logits (fit_ridge_logistic z-scores
   its design). That metric is (a) invariant under any affine recalibration — no Platt-type
   fix could ever move it — and (b) equal to the predicted-logit SD for a perfectly
   calibrated model, i.e. it punished confident well-calibrated models. The configured band
   [0.5, 1.5] plainly intends the conventional Platt slope (1.0 = calibrated).
   `calibration_line` now de-standardizes before returning, giving the slope its configured
   meaning. The band is unchanged, and ALL candidates (V2, V2.1, V2.2) are re-scored under
   the identical corrected metric — this is a measurement bug fix, not a gate relaxation.
   Frozen parameters: minimum 20 resolved pairs AND ≥5 positives AND ≥5 negatives before the
   recalibration activates; until then the raw probability passes through unchanged. The stored
   `probability_value` for V2.2 IS the recalibrated probability; evidence/promotion machinery
   evaluates it unchanged.
2. **Extended true first-print history (data only, no model change):** CFNAI/CFNAI-MA3 first
   prints extended back to the series' first real-time edition (2001-03) from the Chicago Fed
   real-time workbooks; ICSA first prints extended back to 2000-06 from DOL press releases (the
   52-week yoy lookback makes claims usable from ~2001-06). Same importers, same dedupe contract.
   This extends G_NOW coverage from 2009-08 toward ~2001-2002, roughly doubling the growth
   training panels and deepening every cell's OOS window.
3. **Nothing else.** Composites (G_NOW, G_LEAD_V21, PI_NOW_V21, PI_LEAD, SHOCK), predictor
   tuples, mandatory predictors, feature metrics, the financial_conditions recipe, ridge penalty,
   thresholds, and all evidence gates are inherited from V2.1 unchanged. The G_LEAD predictor set
   is deliberately NOT changed: the pre-2019 diagnostic shows the existing inputs carry the
   signal; the frozen hypothesis is that panel depth (delta 2) plus honest calibration (delta 1)
   are what the cell lacked. If this hypothesis fails at the gate, the next candidate (V2.3) may
   redesign predictors — not this one, and not retroactively.

## Benchmarks and promotion rule (unchanged)

V2.2 is evaluated against deployed V1 and shadow V2/V2.1 under identical thresholds by the same
promotion machinery, model_version-scoped. Promotion to `macro.regime_source` requires EVERY
required cell to pass sample-size, class-balance, AUC, Brier-skill, and calibration gates, plus
the decision-confidence gates, in sealed evidence — fail-closed, no partial promotion, no gate
edits. The pre-2019 restriction of the diagnostic preserved the OOS tail; this candidate gets one
shot at the gate as frozen here.
