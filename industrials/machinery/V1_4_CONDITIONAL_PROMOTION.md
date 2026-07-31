# Machinery v1.4 Conditional Promotion

Status: completed; v1.4.1 operational amendment activated effective 2026-07-24

## Production Outcome

The original v1.4 lockbox result below remains immutable and blocked under its
literal turnover definition. A separately versioned v1.4.1 governance
amendment corrected the operational defect: initial portfolio funding is
excluded from recurring-turnover averages while its transaction cost remains
fully charged. No return, membership, model-weight, cost, or lockbox outcome
was changed.

The amendment passed every gate, including exact production reconstruction
parity across five lockbox dates. Stage 12 then activated the fixed
`equal_components` score with the existing `long_only_q20_equal`,
operating-only, equal-weight policy. The active 2026-07-24 contract contains
113 rows, 83 operating-only eligible names, 99 research-eligible names, and 17
selected names. The full portfolio smoke passed with a 4.4293% machinery
allocation under the unchanged 5% cap. The active dashboard rank, shadow
calibration sidecar, and manifest are now published and rolled back atomically.

The final immutable governance evidence is under
`output/industrials/machinery/v141_release_20260724`.

## Original One-Time Result

The one-time 2026 lockbox was opened on 2026-07-29 and is permanently spent.
The fixed `equal_components` candidate produced positive after-cost top-sleeve
excess at both horizons and improved on the active model:

| Horizon | Candidate mean net excess | Active mean net excess | Candidate improvement |
|---|---:|---:|---:|
| 21 trading days | 1.949% | 1.418% | 0.531% |
| 63 trading days | 9.847% | 6.520% | 3.328% |

The candidate also passed the weekly observation, mean, median, hit-rate,
non-overlapping observation, drawdown, position, cohort, liquidity, capacity,
and fixed-cap marginal-contribution gates. Its advisory 90% lower confidence
bounds were positive at 0.310% and 5.476% for 21 and 63 days.

The final decision is `BLOCKED_KEEP_ACTIVE_MODEL`. The sole recorded hard-gate
failure was 63-day average one-way turnover of 77.8% against the frozen 75%
limit. Both the candidate and active model had the same value. With only two
non-overlapping 63-day periods, the calculation averages 100% initial funding
and a 55.6% subsequent rebalance. Transaction costs remain correctly charged,
but the frozen protocol did not distinguish initial funding from recurring
turnover. This result is preserved as observed; the gate is not rewritten
after lockbox outcomes became known.

No conditional Stage 12 candidate was built, no portfolio smoke or rebuild was
run, and no production artifact, model weight, membership rule, or portfolio
cap was changed. The sealed 2026-07-24 machinery model remains active for the
portfolio layer under its existing 5% cap. The conditional production-parity
extract also produced zero lockbox rows; this is a future-protocol defect, but
it cannot affect production because promotion already stopped at the turnover
gate.

This path addresses the narrow governance problem left by v1.3: the fixed
`equal_components` model has usable long-only signal, but its 63-day 90%
confidence bound is marginally below zero. It does not rewrite the v1.3 result
or declare that confidence gate passed.

## Decision Contract

The canonical policy is
`model_protocols/machinery_oos_v1.4.0_conditional_promotion.json`.
It freezes all gates before any 2026 outcome is read and compares the fixed
candidate with the currently active machinery model over the same observations.

Hard requirements are:

- at least 12 matured weekly observations at both 21 and 63 trading days;
- positive candidate mean and median net excess at both horizons;
- at least a 50% positive-excess hit rate at both horizons;
- no after-cost mean-excess deterioration versus the active model;
- existing turnover, drawdown, concentration, ADV, capacity, and cohort gates;
- exact production-policy reconstruction parity;
- no increase above the existing 5% machinery portfolio cap.

The 63-day confidence bound is reported as advisory evidence. It cannot fail or
pass this separate conditional policy. The fixed-cap marginal contribution is
`0.05 * machinery net excess`; it measures the candidate's expected replacement
contribution at the approved sleeve cap. It is not represented as a full
historical cross-sector replay. The actual portfolio must still pass the full
strategic pipeline before replacement becomes active.

## One-Time Sequence

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\31_freeze_machinery_v14_conditional_promotion.py
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\32_open_machinery_v14_conditional_lockbox.py --approval-token OPEN_MACHINERY_V14_LOCKBOX_ONCE
```

If the result is `READY_FOR_PORTFOLIO_SMOKE`, build the isolated replacement
candidate using the latest completed machinery snapshot:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\33_build_machinery_v14_conditional_stage12.py --asof 2026-07-28
```

The existing Stage 12 transaction performs preflight, candidate publication,
the full portfolio smoke, activation-state replacement, and automatic rollback.
It must use the conditional governance directory and skip machinery refresh:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\25_activate_machinery_production.py --asof 2026-07-28 --governance-dir output\industrials\machinery\model_cycles\machinery_oos_v1.4.0\conditional_promotion\stage12_candidate --preflight
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\25_activate_machinery_production.py --asof 2026-07-28 --governance-dir output\industrials\machinery\model_cycles\machinery_oos_v1.4.0\conditional_promotion\stage12_candidate --approval-token ACTIVATE_MACHINERY_STAGE12 --skip-refresh --reuse-risk-price-data
```

If any lockbox gate fails, the sequence stops before Stage 12 candidate creation.
The 2026-07-24 active machinery model and its 5% portfolio cap remain unchanged.
Defense is not read, run, or modified by this path.
