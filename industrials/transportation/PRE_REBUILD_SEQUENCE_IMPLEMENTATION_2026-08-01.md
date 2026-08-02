# Transportation pre-rebuild sequence implementation

Date: 2026-08-01  
Data cutoff: 2026-07-30  
Status: `COMPLETE`

## Frozen design

The pre-rebuild contract fixed the model before the expensive historical reconstruction:

- exactly 24 outcome-blind, active surface-freight names;
- the same percentile engine for research and production;
- peer-relative normalization within asset-light logistics or asset-based freight;
- fixed metric-slot weights, with unavailable optional metrics assigned a neutral score of 50;
- `operating_ratio` as the only retained specialized scoring metric;
- three component-level candidates fixed before examining the rebuilt results;
- broad specialized parser reruns prohibited.

The eligible tickers are `ARCB, CHRW, CNI, CP, CSX, CVLG, EXPD, FDX, FWRD, GXO, HUBG, JBHT, KNX, LSTR, NSC, ODFL, RLGT, SAIA, SNDR, TFII, UNP, UPS, WERN, XPO`.

The frozen candidates are:

1. `surface_balanced`
2. `surface_quality_efficiency`
3. `surface_balanced_positioning`, with positioning capped at 5%

## Pre-rebuild gates

| Gate | Result |
|---|---|
| Exactly 24 governed eligible tickers | PASS |
| Current names rank-ready | PASS, 24/24 |
| Research/production score-engine parity | PASS |
| Fixed component denominators | PASS |
| Specialized dispositions reconciled | PASS |
| Required-metric historical coverage | PASS |
| PIT positioning feasibility | PASS, 98.87% of rows |
| Pre-registered candidate count | PASS, 3 |
| Broad specialized parser rerun | Prohibited |
| Single versioned historical reconstruction | Technically unblocked and completed |

## Execution status

The single weekly PIT history reconstruction, panel build, independent return validation, three-candidate calibration, shared portfolio-adapter check, readiness audit, and fail-closed promotion attempt are complete. The old v1 release was not overwritten.

The machine-readable prebuild contract is `output/industrials/transportation/prebuild/surface_freight_score_v2/transportation_surface_freight_prebuild_manifest.json`.

The completed results and production decision are documented in `SURFACE_FREIGHT_V2_EXECUTION_RESULT_2026-08-01.md`. Production promotion is blocked by failed validation and walk-forward evidence, not by missing history, metric plumbing, or portfolio-layer integration.
