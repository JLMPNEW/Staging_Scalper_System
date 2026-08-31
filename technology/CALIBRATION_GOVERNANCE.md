# Technology Calibration Governance

The three technology families keep independent scoring implementations. Shared
governance code standardizes evidence sealing and promotion rules only.

| Field / artifact | Meaning | Consumer |
|---|---|---|
| `stage8_gate_pass` | Candidate passed the untouched-holdout IC, Newey-West, spread, turnover, concentration, and fold gates. It is preliminary. | Stage 8C |
| `promotion_candidate` in Stage 8 | Always `0`. Stage 8 alone cannot authorize promotion. | Governance reports |
| `post_lock_data_included` | Research override used data after the declared training lock. Such a run is never promotable. | Stage 8/8C validators |
| `procedure_adds_value` | Walk-forward procedure passed win-rate, gate-rate, constraints, paired-t, IC, hit-rate, and spread requirements. | Final promotion gate |
| `final_promotion_eligible` | Stage 8 and Stage 8C both passed on identical config/panel hashes. Still requires human approval. | Promotion receipt tool |
| `stage8_run_manifest.json` | Hashes current artifacts and their immutable `runs/<run_id>` copies. | Stage 8 validator |
| `walk_forward_run_manifest.json` | Hashes walk-forward artifacts and binds them to the Stage 8 run ID. | Stage 8C validator |
| Promotion receipt | Immutable record of approver, model version, effective date, candidate runs, and production weights. | Lockbox publisher/validator |
| `production_model_version` | Stable identity of the weights in production. A new promotion must use a new value. | OOS provenance |
| `production_model_effective_date` | First date the named version can be a live OOS score. It must equal the production start and cannot reuse an old model date. | OOS provenance |
| `oos_score_valid_flag` | Live-style score from the named frozen model after its effective date. Deep historical replays remain false. | Portfolio allocation/OOS tests |
| `research_calibration_input_eligible_flag` | PIT research input eligibility before forward returns are joined. It is not an OOS claim. | Stage 11 input assembly |
| `stage11_calibration_input_eligible_flag` | Survivorship-corrected research panel or strict OOS row accepted for Stage 11. | Stage 11 calibration |

## Required sequence

1. Run Stage 8 through the family wrapper. The wrapper hardens and seals it.
2. Run the Stage 8 validator. Stale config, stale panel, mixed run IDs, or tampering fail closed.
3. Run Stage 8C through the family wrapper, then its validator.
4. Only when `final_promotion_eligible=1`, create a receipt with
   `technology/scripts/21_create_technology_promotion_receipt.py`.
5. Review the receipt, update production weights and governance config, and set
   a new model version/effective date. Never move an old lock date forward to
   relabel already-observed returns as OOS.

The software v1 model predates promotion receipts. It is explicitly labeled
`legacy_pre_receipt_grandfathered`; this is a disclosed evidence limitation,
not a claim that the overwritten original candidate artifacts were recovered.
