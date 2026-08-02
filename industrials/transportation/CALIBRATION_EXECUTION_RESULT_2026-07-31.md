# Transportation Calibration Execution Result - 2026-07-31

## Decision

The transportation implementation, historical loading, PIT reconstruction, OOS
panel, and calibration procedure are complete. Promotion is not authorized because
the validation-selected model failed the sealed holdout and walk-forward stability
gates. This is a statistical result, not a missing-data, parser, portfolio-adapter,
or production-governance defect.

## Executed sequence and gates

| Stage | Result | Evidence |
| --- | --- | --- |
| Historical raw and identity inputs | PASS | Active and delisted histories are represented; Celadon `CGI` ends 2019-12-09. |
| PIT feature rebuild | PASS | 93 snapshots, 2019-01-02 to 2026-07-30, 39 metrics, zero validation errors. |
| Daily market materialization | PASS | 1,904 dates; price series loaded once; zero network/parser calls. |
| Daily rank history | PASS | 1,904/1,904 dates; active and inactive membership sources present. |
| Generic OOS panel | PASS | 78,562 rows; 35,230 eligible; 382 weekly snapshots; survivorship-corrected. |
| Calibration-input preflight | PASS | 98.7727% complete rows for every frozen candidate versus 90% minimum. |
| Validation selection | PASS | `growth_quality` selected without using holdout results. |
| Sealed holdout | FAIL | IC -0.04612; net excess -0.00328; hit rate 36.36%. |
| Walk-forward stability | FAIL | 0/4 blocks pass versus minimum 2/4. |
| Promotion readiness | FAIL | `promotion_eligible=false`; blockers independently sealed. |
| Shared portfolio adapter | PASS | 112 rows ingested; 83 rank-ready; zero OOS-valid/investable rows. |

## Acceptance interpretation

Two different outcomes must not be conflated:

1. **Implementation acceptance:** passed. The historical data, shared industrials
   infrastructure, survivorship handling, valuation coverage, OOS panel, and
   portfolio adapter work and are independently validated.
2. **Model promotion acceptance:** failed. The model did not demonstrate positive,
   stable OOS economics under the predeclared gates.

The production promoter, readiness audit, effective-dated lock, activation script,
production adapter validator, and immutable production-release tooling are present
and fail closed. They must not be invoked to create a lock or allocation unless a
future governed calibration passes.

## Governed next action

Keep the 2026-07-30 failed calibration immutable. Accumulate new outcome-blind
monitoring observations after the research cutoff, then use the governed
recalibration authorization path to freeze a new panel and run one bounded future
calibration. Do not lower gates, select a candidate based on the revealed holdout,
or rerun the same history looking for a passing result.
