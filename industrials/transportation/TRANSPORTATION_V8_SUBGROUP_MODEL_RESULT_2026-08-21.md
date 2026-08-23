# Transportation v8 subgroup model result — 2026-08-21

## Outcome

The corrected sequence completed through the diagnostic gate:

1. The exhaustive financial-statement and exhibit parse completed once for surface freight and once for tankers.
2. Semantic review completed once for every priority; there were no parser invocations after review.
3. Point-in-time metric coverage was reassessed before the scoring model was changed.
4. The subgroup-specific v8 scoring policy was frozen before reading outcomes.
5. Historical scores were regenerated once from the immutable v6 financial/market panel plus the accepted semantic replay. No financial statement was reparsed.
6. Each ranked comparison group was calibrated separately. Surface aggregation was not run because the group gates did not all pass. Tanker calibration was not run because its required cycle pack failed the historical coverage gate.

This is a completed diagnostic implementation, not a production promotion. Revealed history remains ineligible to authorize production.

## Coverage after semantic replay

The direct metric-domain audit still has only one 75%-coverage pass: `operating_ratio::rail_networks` at 72 of 92 dates. V8 does not reuse the ineffective operating-ratio level. It builds issuer-stable changes, requiring the same definition on the current and prior-year observations and enforcing filing-date point-in-time availability.

| Comparison group | V8 specialized pack status | Passing dates | Required rule |
|---|---:|---:|---:|
| Rail networks | Active | 91 / 92 | latest pass and at least 75% of dates |
| LTL carriers | Active | 92 / 92 | latest pass and at least 75% of dates |
| Truckload/intermodal | Active | 73 / 92 | latest pass and at least 75% of dates |
| Asset-light logistics | Explicit generic fallback | 0 / 92 | pack excluded until coverage passes |
| Integrated parcel | Fixed-weight, non-ranked sleeve | 0 / 92 | two names cannot support an independent specialized calibration |
| Oil tankers | Required pack blocked | 35 / 92 | latest pass but less than 75% of dates |

Latest tanker breadth is seven issuers for `tce_rate_yoy_growth`, seven for `revenue_days_yoy_growth`, two for the TCE/breakeven spread, and one for fleet utilization. The combined pack reaches eight issuers on the latest date, but it does not have enough chronological depth. The score therefore cannot silently fall back to a generic industrial tanker model.

## Scoring corrections implemented

- Generic and specialized percentiles are calculated within the applicable comparison group.
- Final component weights are different for rail, LTL, truckload/intermodal, asset-light logistics, parcel, and oil tankers.
- Asset-light logistics uses an explicit fixed generic fallback until its net-revenue/purchased-transportation pack passes. Parcel is an eligibility-only fixed-weight sleeve and is not represented as a calibrated specialized ranking.
- Operating-ratio level is prohibited. V8 uses issuer-stable year-over-year operating-ratio improvement. Revenue/load and TCE levels are likewise converted to changes or economically defined spreads where appropriate.
- The tanker cycle pack has a 30% final-score weight when coverage passes: 40% TCE growth, 30% TCE-to-cash-breakeven spread, 15% revenue-days growth, and 15% fleet utilization. Fleet age is not used as a token low-weight substitute.
- Every ranked group is gated before cohort aggregation.
- Surface group weights are fixed and outcome blind: rail 25%, LTL 25%, truckload/intermodal 25%, asset-light 15%, parcel 10%. Missing groups cannot donate weight to a larger group.

## V8 63-session diagnostic results

All results below use revealed history and are diagnostic only.

| Group | Full-history IC | Full-history net excess | Full-history hit rate | Fixed-block result |
|---|---:|---:|---:|---|
| Rail networks | -0.1590 | -1.19% | 40.79% | FAIL / FAIL / FAIL |
| LTL carriers | -0.0141 | +6.92% | 60.53% | FAIL / PASS / FAIL |
| Truckload/intermodal | +0.0879 | +1.56% | 47.37% | FAIL / FAIL / FAIL |
| Asset-light logistics | +0.0395 | +1.16% | 47.37% | FAIL / PASS / FAIL |
| Integrated parcel | Not ranked | Fixed sleeve only | Not applicable | Not a ranking gate |
| Oil tankers | Not run | Required cycle history blocked | Not applicable | 35 / 92 coverage dates |

Because every ranked surface group did not pass independently, the fixed-weight surface aggregate was not calibrated. This prevents a strong group or a larger group from hiding a failed comparison group.

## Governance decision

- Historical financial reparses in this v8 rebuild: **0**.
- Post-semantic parser invocations: **0**.
- Historical score regenerations: **1**.
- Historical diagnostic calibration executions: **1**.
- Production activation authorized: **No**.
- Portfolio-layer state changes: **None**.

The next admissible evidence is future-only subgroup monitoring under the frozen v8 policy. Additional parsing under the same definitions is not justified by this run; it would repeat the exhausted source search. Any future extraction work must be triggered by a new source or a precisely identified historical issuer-period gap, not by a failed calibration result.

## Primary artifacts

- Policy: `industrials/transportation/data/transportation_subgroup_score_policy_v8.yaml`
- Score engine: `industrials/transportation/subgroup_scoring.py`
- One-time score regeneration: `industrials/transportation/scripts/41_build_transportation_v8_subgroup_scores.py`
- Group-first calibration: `industrials/transportation/scripts/42_run_transportation_v8_subgroup_calibration.py`
- Score manifest: `output/industrials/transportation/investable_v5/subgroup_scores_v8/2026-08-21/transportation_v8_subgroup_score_history.json`
- Calibration manifest: `output/industrials/transportation/investable_v5/subgroup_calibration_v8/2026-08-21/transportation_v8_subgroup_calibration.json`

