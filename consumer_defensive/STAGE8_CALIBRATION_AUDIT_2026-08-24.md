# Consumer Defensive Stage 8 Calibration Audit

Audit date: `2026-08-24`

## Outcome

Stage 8 report-only constrained calibration is implemented and passes its
isolated acceptance gate. Accepted run `cds8_2a94264294f4b58b1444fb2d`
completed against the Stage 7 rehearsal database and passed 24/24 independent
artifact checks.

The research decision is `retain_stage7_core_baseline`. No candidate passed the
complete validation, walk-forward and final-holdout sequence. No production
weight, Stage 7 row, Portfolio Layer artifact or OOS flag was changed.

## Audit Findings And Corrections

The implementation audit found and corrected three defects before acceptance:

1. The first rehearsal attempted to read a nonexistent Stage 6C
   `label_status` column. Stage 8 now derives completeness from the three sealed
   XLP-relative horizons while preserving the terminal-event status.
2. The validator used a sample-role literal that differed from the authoritative
   Stage 6C value. Stage 8 now uses one exact `deep_replay_research` constant in
   its contract, panel summary and validator.
3. The original 86-date split counted 30 dates before short-interest data made
   the frozen Stage 7 baseline rank-ready. That made complete training evidence
   impossible. Stage 8 now retains all dates in the audit panel but constructs a
   label-blind date census from the frozen baseline and the registered 30-name
   sector floor. The validator independently recomputes the same census.
4. The first Windows artifact writer translated LF to CRLF while immutable
   replay comparison used LF bytes. V3 passed content validation but correctly
   failed the exact replay. Stage 8 now disables newline translation in its
   atomic writer; a regression test and full same-directory replay prove exact
   byte stability.

Attempts `v1`, `v2` and `v3` are retained as failed evidence and are not
acceptance runs. No failed artifact was overwritten or promoted.

## Frozen Inputs

- Rehearsal database: `consumer_defensive_stage7_rehearsal.sqlite`
- Stage 6C run: `3`
- Stage 6C panel SHA-256:
  `d2c7155be91cf21c2826e911e083e662bf203119ee087baf12f754ac2d2adcf0`
- Stage 7 source: `consumer_defensive_stage7_baseline_v3`
- Stage 7 contract SHA-256:
  `d5184d007b89f3be62c61277cd4ddcb864f15ff0ccd09d9234de31922cf909c8`
- Factor campaign:
  `cdfv_20260814_d2c7155be91c_2498172c7161_a6495192b5`
- Factor registry SHA-256:
  `f5e7f2289c092e75fe4d9b35b1db1bf397f4ad000b3d24e4ad8f675a558acdde`
- Accepted specialized cells: `0/174`

## Historical Panel And Split

- Audit panel: 9,036 rows, 116 tickers, 86 dates
- Panel content SHA-256:
  `68bacff674459f9cb992dd133a9d87db29c681706cb43338de9b8a5d436f29cd`
- Frozen whole-ticker price selection SHA-256:
  `f0afe617978c4ffe052f8a6c6e6697fa7d7ec518fab4a515f79d745c71274024`
- Baseline-eligible dates: 56
- Explicitly excluded non-calibration dates: 30
- First eligible date: `2021-07-30`
- Training: 30 dates through `2023-12-29`
- First embargo: 7 dates
- Validation: 6 dates from `2024-08-30` through `2025-01-31`
- Second embargo: 7 dates
- Final holdout: 6 dates from `2025-09-30` through `2026-02-11`

Both embargoes exceed the configured isolation minimum for the 126-session
label. The candidate registry was written before panel construction and label
evaluation.

## Candidate Census And Verdicts

The deterministic registry contains 320 candidates: 64 for the sector and 64
for each of the four cohorts. Core weights obey the 15% component cap, 30% L1
turnover cap, factor-breadth bounds and hierarchical cohort shrinkage. Missing
components contribute the neutral score without weight redistribution.

| Family | Validation | Walk-forward wins | Holdout | Final reason |
| --- | ---: | ---: | ---: | --- |
| Beverages | Pass; improvement `0.014240` | `2/3` | Opened; improvement `0.00005848` | Final improvement below `0.002` |
| Consumer Defensive sector | Pass; improvement `0.002246` | `1/3` | Sealed | Walk-forward gate failed |
| Distribution and retail | Failed | `1/3` | Sealed | 126-day validation IC was negative |
| Household, personal and tobacco | Pass; improvement `0.004811` | `1/3` | Sealed | Walk-forward gate failed |
| Packaged foods and agriculture | Failed; improvement `-0.032509` | `2/3` | Sealed | Validation improvement gate failed |

Beverages was the only family allowed to open the final holdout. Its candidate
had positive holdout ICs and passed constraints, but did not improve sufficiently
over the frozen Stage 7 baseline. All five family verdicts are therefore
`rejected`, not `accepted` or unevaluable.

No specialized candidate was generated because the corrected factor campaign
accepted zero cells. Metric coverage alone cannot create a nonzero weight.

## Immutable V4 Artifacts

Artifact root:
`C:\Users\josel\Documents\STAGING\ConsumerDefensiveRollout\20260824_exhaustive_audit\stage8\2026-08-14\v4`

- Contract payload SHA-256:
  `4bd0b9559dc466ebc2cbc5d0ed3e54e4fc6fc5789a1e608c665d2505610a6214`
- Candidate registry file SHA-256:
  `859c4e7d3d14f685e27e736cefbcb473a7124e5f646c2b8e854beea60eedb09f`
- Historical panel file SHA-256:
  `ff95903a842052bf196058c6413cab3d98a3ea188609b3168b7383a0cd545e20`
- Split manifest file SHA-256:
  `6f8540d6c7c2a8d09cbbd701f6012a40f9d0a34e94a9c5cb6847f1d25d209305`
- Candidate results file SHA-256:
  `1341f6cc3e661a84a4a3b508020679651fa641638d9abfd3789369e6dc6056e6`
- Walk-forward results file SHA-256:
  `0a54b8d57efeef1e58d430b0ded39d1802ab9081e8222cb69b9a6e7c7c268ee1`
- Decision file SHA-256:
  `6b6b47260b1bc041f2e26b51751c5f36552fc9a6bc2f0f11da56d1fc9d20247d`
- Artifact manifest SHA-256:
  `3a175c02746a31079e2ab0038f177456dca7a5081c2c8acd7d999724f54ff8ca`

The independent validator checks artifact hashes and sizes, methodology hashes,
Stage 6C and Stage 7 lineage, factor-campaign identity, candidate
preregistration, split chronology, label-blind date eligibility, row hashes,
holdout governance and all safety locks.

## Verification

- Focused Stage 8/contract/script tests: 35 passed
- Full Consumer Defensive suite: 398 passed, 5 skipped
- Shared factor-validation suite: 89 passed
- Python compilation: passed
- Ruff on the Stage 8 implementation: passed
- Standalone v4 artifact validation: 24/24 PASS
- Same-directory full v4 replay: byte-for-byte PASS

The skipped tests are the existing platform-specific skips. The two unrelated
provider lock files in the dirty worktree were not modified by this work.

## Next Dependency

Stage 9 report-only portfolio backtesting is now unblocked. It must consume the
same PIT panel and immutable Stage 8 registry, compare the frozen Stage 7
baseline with registered candidates under explicit transaction, borrow,
turnover, capacity, concentration and terminal-return policies, and remain
unable to change weights or production gates.
