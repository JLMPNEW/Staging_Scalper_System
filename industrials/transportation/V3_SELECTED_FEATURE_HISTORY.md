# Transportation V3 Selected-Feature History

Status: DP8, DP9, G8, and DP10 complete and passing on 2026-07-29.

## Outcome

The final conflict-resolved transportation specialized-metric history was
materialized once
from the frozen evidence and the already-frozen v2 point-in-time snapshots.
The implementation did not retrieve or parse a document, rebuild prices,
rebuild generic financial features, alter membership, write to the database,
invoke the portfolio layer, or run calibration.

The resulting panel contains:

- 92 point-in-time observation dates from 2019-01-02 through 2026-07-22.
- 9,496 historical ticker/date memberships.
- All 90 finalized specialized discovery metrics and explicit applicability
  states: 854,640 rows.
- The 18 frozen generic metrics plus all 90 discovery metrics: 1,025,568 rows.
- Three calibration-subset selections: `fleet_utilization`,
  `operating_ratio`, and `passenger_load_factor`.

`fleet_utilization` has 485 point-in-time value rows across nine issuers,
`operating_ratio` has 651 across 14 issuers, and
`passenger_load_factor` has 381 across nine issuers. Deferred,
diagnostic-only, and excluded metrics remain in the research panel but are not
selectable by the calibration-subset manifest.

The final reviewed evidence lineage is evaluations 11/8/5/9. Before
publication, the materializer failed closed on two same-period passenger-load
factor conflicts. Cached filing tables confirmed ALK 83.7% and UAL 82.4% as
the consolidated values; the regional/unlabeled alternatives were suppressed
through exact review policies. No source was retrieved or parsed again.

## Efficient sequence

DP8 (`19b`) is a read-only impact preflight. It verifies:

- Every frozen v2 snapshot hash.
- The 9,496-row membership contract.
- The 14,400-row specialized scope and final coverage contracts.
- The 90-row registry and final-disposition contracts.
- The exact reviewed evaluation and parser-run evidence lineage.

Because v3 adds a 90-metric discovery registry to v2, DP8 returned
`GO_ALL_SPECIALIZED_PARTITIONS_ONLY`. Market, generic financial,
reporting-profile, membership, price, and FX partitions were not rebuilt.

DP9 (`19c`) streams two deterministic compressed panels:

- `transportation_v3_specialized_discovery_panel.csv.gz`
- `transportation_v3_complete_panel.csv.gz`

Reviewed accepted parser evidence is selected only when its filing/acceptance
date and period end are on or before the snapshot date and it is within the
metric's frozen staleness limit. Exact duplicate scope variants are
deduplicated deterministically, and conflicting accepted values fail closed.
Frozen v2 financial-derived values are reused where the discovery metric has
the same finalized definition. Missing or non-applicable states remain
explicit.

The calibration subset is a hash-bound column-selection manifest over the
complete panel. It does not create a second feature table.

A repeat `19c` invocation verifies the existing artifact hashes and reports
`REUSED_FROZEN_PANEL` with zero materialization invocations. If the DP8 input
hash changes, it fails closed and requires a new versioned output directory
instead of overwriting or silently rebuilding v3.

G8 (`19d`) independently re-streams every row and verifies:

- Exact date/ticker/metric order and membership.
- Exactly 90 specialized rows and 108 complete rows per membership.
- No future availability date or future period end.
- Finite numeric values.
- Exact artifact hashes.
- Only a final `CALIBRATION_CANDIDATE` enters the subset.
- The calibration-input hash equals the complete-panel hash.

DP10 (`19e`) freezes the flag-exception decision and the single calibration
contract. No flag exception is authorized: `going_concern_flag` fails breadth,
depth, and binary-variation checks, while `pre_revenue_flag` has no frozen PIT
values. The 92-date calendar is split into 52 train, 15 validation, 19
holdout, and six purged embargo dates. Month-end remains a research
observation cadence, not a portfolio rebalance rule.

## Commands

```powershell
C:\Users\josel\Miniconda3\python.exe industrials\transportation\scripts\19b_preflight_transportation_parser_impacts.py --output-dir output\industrials\transportation\historical_features\v3_conflict_resolved
C:\Users\josel\Miniconda3\python.exe industrials\transportation\scripts\19c_materialize_transportation_parser_impacts.py --output-dir output\industrials\transportation\historical_features\v3_conflict_resolved
C:\Users\josel\Miniconda3\python.exe industrials\transportation\scripts\19d_validate_transportation_parser_feature_history.py --output-dir output\industrials\transportation\historical_features\v3_conflict_resolved
C:\Users\josel\Miniconda3\python.exe industrials\transportation\scripts\19e_freeze_transportation_walk_forward_calibration_contract.py --output-dir output\industrials\transportation\historical_features\v3_conflict_resolved
```

## Acceptance results

- DP8 acceptance: `PASS`.
- DP8 decision: `GO_ALL_SPECIALIZED_PARTITIONS_ONLY`.
- DP9 acceptance: `PASS`.
- G8 acceptance: `PASS`.
- DP10 acceptance: `PASS`.
- Panel status: `FROZEN`.
- Future availability errors: 0.
- Future period errors: 0.
- Additional parser invocations: 0.
- Additional market/financial feature builds: 0.
- Database writes: 0.
- Calibration invocations: 0.
- Production promotion: false.

The next gate is the single authorized walk-forward calibration against the
hash-sealed complete panel and DP10 contract. It uses a 63-trading-day forward
outcome, IYT primary benchmark, XTN/SPY robustness benchmarks, 20-bps base and
40-bps stress costs, and bounded cohort-specific overlay weights. Production
promotion remains false.
