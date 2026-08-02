# Transportation surface-freight v2 execution result

Date: 2026-08-01  
Data cutoff: 2026-07-30  
Status: `IMPLEMENTATION PASS / PRODUCTION PROMOTION BLOCKED`

## Outcome

The efficient pre-registered sequence is complete for the frozen 24-name surface-freight universe. The run reused the already-loaded point-in-time financial and market store, reconstructed positioning locally, produced one versioned weekly score history, built one independently reconciled outcome panel, and evaluated only the three frozen candidates. No broad filing parse, network fetch, or repeated financial rebuild was performed.

The technical implementation passes. Production promotion does not pass because the repaired ranking model failed validation and walk-forward stability, and the historical evidence is already revealed rather than untouched post-freeze evidence. The promoter was executed once and correctly refused to create a production bundle.

## Completed artifacts

| Artifact | Result |
|---|---:|
| Frozen universe | 24 active surface-freight names |
| Weekly PIT score snapshots | 396/396 PASS |
| Score-history period | 2019-01-04 through 2026-07-30 |
| OOS panel rows | 18,070 |
| OOS-eligible rows | 15,028 |
| OOS weekly snapshots | 382 |
| Outcome horizons | 21 and 63 sessions |
| Independent return recomputation | 18,070/18,070 PASS |
| Maximum return reconciliation error | 5.12e-13, below 1e-9 tolerance |
| Portfolio adapter | 24 rows through shared `industrial_family`; PASS |
| Investable/OOS-valid rows | 0/24, correctly fail-closed |

The versioned v1/release artifacts were not overwritten. The v2 roots are under `output/industrials/transportation/surface_freight_v2/`.

## Frozen-candidate results

### Validation

| Candidate | Mean IC | Net top excess | Hit rate | Result |
|---|---:|---:|---:|---|
| `surface_balanced` | -0.0156 | -1.71% | 33.33% | FAIL |
| `surface_balanced_positioning` | -0.0098 | -1.51% | 33.33% | FAIL |
| `surface_quality_efficiency` | -0.0362 | -0.97% | 42.86% | FAIL |

`surface_quality_efficiency` was selected using validation only. Selection did not use holdout outcomes.

### Holdout diagnostic

The selected candidate produced mean IC +0.0666, net top excess +2.41%, and a 64.94% hit rate. This is encouraging but cannot override the failed validation or be reused as untouched promotion evidence.

### Walk-forward stability

| Block | Selected candidate | Mean IC | Net top excess | Hit rate | Result |
|---:|---|---:|---:|---:|---|
| 1 | `surface_quality_efficiency` | +0.0407 | -1.42% | 41.46% | FAIL |
| 2 | `surface_quality_efficiency` | -0.1970 | -2.60% | 34.15% | FAIL |
| 3 | `surface_quality_efficiency` | +0.0043 | -2.94% | 24.39% | FAIL |
| 4 | `surface_balanced_positioning` | +0.0608 | +5.99% | 95.24% | PASS |

Walk-forward pass rate is 25%, below the required 50%.

## Readiness decision

The readiness audit itself passes and now reports only substantive blockers:

1. Validation failed minimum mean IC, minimum mean net top excess, and minimum hit rate.
2. Walk-forward stability passed only one of four blocks.
3. The completed calibration is therefore not promotion eligible.
4. No untouched post-freeze promotion evidence exists yet.

History validation, outcome-return reconciliation, artifact hashes, current rank integrity, and shared portfolio-adapter integration all pass. The active-only frozen production cohort does not require delisted names to be injected into its score history; delisted transportation data remains retained outside this production cohort for research and broader survivorship work.

## Correct next operational path

Do not remove losing tickers, change candidates, or tune weights against this revealed panel. That would convert diagnostic history into selection leakage and force another expensive but invalid rebuild.

Keep the 24-name universe, score policy, component definitions, and three candidates frozen. Accumulate genuinely post-freeze weekly shadow observations and their matured 63-session outcomes. Re-enter calibration only through a pre-registered, bounded decision gate after the minimum evidence window is met. Promotion may occur only if validation, holdout, walk-forward, artifact, adapter, readiness, lock, and release gates all pass on eligible evidence.

If the frozen model remains unable to generate positive cross-sectional IC and net top-bottom spread on that clean evidence, retire transportation ranking to monitor-only status or consider a separately governed eligibility-screened equal-weight sleeve. Do not manufacture promotion by weakening thresholds.
