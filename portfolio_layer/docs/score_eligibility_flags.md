# Score Eligibility Flags

This table defines the eligibility and OOS fields that flow from sector score exports into the portfolio-layer `stocks_scores.csv` contract.

| Field | Layer | Meaning | Primary Consumer |
| --- | --- | --- | --- |
| `portfolio_candidate_gate` | Sector dashboard export | Strict live-style allocation gate. A row is a candidate only when the sector model is complete, rank-ready, calibration-eligible, and OOS-valid. | Portfolio-layer adapters |
| `oos_score_valid_flag` | Sector export and canonical contract | The score is valid as a frozen/live out-of-sample model on the row date. Pre-lock snapshots must be `0`. | Portfolio allocation and OOS validation |
| `research_calibration_input_eligible_flag` | Sector dashboard export | The row has usable score and point-in-time feature provenance, ignoring whether the snapshot universe is survivorship-correct. | Source-level diagnostics only |
| `survivorship_corrected_panel_flag` | Sector or future Stage 11 export | The snapshot source includes point-in-time active and delisted members rather than replaying only the current universe. Dashboard rank snapshots default to `0`. | Stage 11 calibration guardrail |
| `stage11_calibration_input_eligible_flag` | Sector dashboard export | The row is allowed into Stage 11 input only if it is strict OOS or explicitly from a survivorship-corrected panel. Current dashboard snapshots set this to `0` for pre-lock rows. | Portfolio-layer adapters |
| `calibration_sample_role` | Sector export and canonical contract | Source/intrinsic role before Stage 1 guardrails: `strict_oos`, `pre_lock_research`, or `excluded`. | Provenance and joins back to source files |
| `stage1_sample_role` | Canonical contract | Portfolio-layer verdict after adapter guardrails. Stage 1 invariants use this field, not `calibration_sample_role`. | Stage 1 validation and downstream portfolio code |
| `calibration_research_eligible` | Canonical contract | Stage 1 verdict that the row may be used by portfolio-layer calibration/research consumers. Historical dashboard replay rows are `0` until a survivorship-correct panel export exists. | Future Stage 11 calibration |
| `investable_eligible` | Canonical contract | Allocation eligibility after all adapter gates. Must imply `stage1_sample_role='strict_oos'`, `oos_score_valid_flag=1`, and `calibration_research_eligible=1`. | Portfolio construction |

Important distinction: historical technology dashboard rank tables replay the current universe and are not a survivorship-correct calibration panel. They are useful dashboard snapshots, but Stage 11 historical calibration must come from a separate survivorship-correct sector diagnostics export stamped with `survivorship_corrected_panel_flag=1`.
