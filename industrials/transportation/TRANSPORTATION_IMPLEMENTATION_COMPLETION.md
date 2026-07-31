# Transportation Implementation and Calibration Completion

Status date: 2026-07-30  
Status: complete in validated shadow mode

## Completion decision

The transportation implementation and its authorized historical calibration
are complete using data available through 2026-07-30. A July 31 observation is
not an implementation or calibration dependency.

The final completion gate is:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\22_finalize_transportation_implementation.py --asof 2026-07-30
```

It performs no network request, parsing, historical materialization,
calibration, outcome access, portfolio write, or production configuration
write. It validates and hash-binds the already completed stages.

## Frozen calibration result

The single authorized bounded walk-forward calibration evaluated
`fleet_utilization`, `operating_ratio`, and `passenger_load_factor` within
their approved cohorts. Train and validation selected a 10% challenger weight
for each metric. Untouched holdout evidence rejected all three:

| Metric | Validation-selected weight | Final specialized weight | Decision |
|---|---:|---:|---|
| `fleet_utilization` | 0.10 | 0.00 | Retain zero overlay |
| `operating_ratio` | 0.10 | 0.00 | Retain zero overlay |
| `passenger_load_factor` | 0.10 | 0.00 | Retain zero overlay |

A zero final weight is the completed calibration decision, not an incomplete
run. The idempotent reuse check proves that the calibration manifest,
observations, period results, grid, selection, hashes, and timestamps remain
unchanged and that the recorded invocation count remains exactly one.

## Completion gates

The finalizer requires:

1. Stage 0-4 production-readiness validation at the requested as-of date.
2. All 112 active securities plus IYT, XTN, and SPY through 2026-07-30.
3. Exact-date market, financial, metric-state, scoring, and rank outputs.
4. A 12,096-row current panel: 112 members by 108 metrics.
5. The frozen 92-date, 9,496-membership survivorship-corrected history.
6. One and only one passing walk-forward calibration invocation.
7. Independent calibration validation and sealed zero-overlay decisions.
8. A passing, optional, fail-closed `portfolio_layer` connection.
9. A passing outcome-blind monitor with no future data access.

The 2026-07-30 execution passed all 12 implementation gates. The complete
transportation regression passed 355 tests, and the full shared industrials
regression passed 606 tests.

## Operational posture

The implementation is complete and the shadow model is operational. The
holdout result does not authorize production portfolio allocation:

- `production_model_promoted=false`
- `oos_score_valid_flag=0`
- `portfolio_candidate_gate=0`
- final specialized overlay weights are zero

This is an evidence-based non-promotion decision, not unfinished
implementation. Future month-end signal capture is normal post-implementation
monitoring and cannot retroactively change the completed calibration without a
separate reviewed protocol.
