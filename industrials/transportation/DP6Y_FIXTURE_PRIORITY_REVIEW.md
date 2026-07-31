# Transportation DP6Y Fixture-Priority Review

Status: complete and passing on 2026-07-29.

## Purpose

This batch converts the highest-yield stored review evidence into exact
semantic fixtures without retrieving or parsing the source corpus again. It
implements the efficient order:

1. Review the 93 ticker/metric pairs with one numeric candidate.
2. Review the remaining pairs for `revenue_days`,
   `average_length_of_haul`, `vessel_count`, `fleet_capacity`,
   `tce_day_rate`, and `fuel_surcharge_revenue_ratio`.
3. Apply only exact accepted or hard-rejected evidence policies.
4. Replay those policies against immutable evidence from every contributing
   parser run.
5. Rebuild coverage once and then reapply the already-sealed financial
   repairs.
6. Freeze the combined final metric dispositions.

No step in this batch authorizes source retrieval, document parsing, feature
materialization, calibration, portfolio writes, or production promotion.

## Frozen priority scope

`09m_build_transportation_fixture_priority_batches.py` freezes the complete
719-pair queue and 2,007 evidence rows before selecting work:

- Phase A: 57 strict single-numeric pairs.
- Phase B: 36 single-numeric pairs with nonnumeric text noise.
- Phase C: 141 remaining pairs from the six priority metrics.
- Phase D: 485 remaining pairs, frozen but not included in this batch.

The first two phases contain all 93 single-value pairs. The top-six set has
161 pairs, 20 of which overlap the single-value phases, so the implemented
batch reviews 234 unique pairs, or 32.55% of the original queue.

## Semantic decision gate

`09n_review_transportation_priority_fixtures.py` validates period lineage,
source-metric lineage for derived evidence, unit/value bounds, issuer scope,
and metric-specific definitions. Generic or ambiguous evidence fails closed.
Only narrow, deterministic rejection classes are eligible for policy.

The 234 pair decisions are:

- 16 `ACCEPT`
- 66 `REJECT`
- 152 `DEFER`

The accepted pairs are:

- `average_length_of_haul`: ARCB, SWFT, TFII, USX
- `completion_factor`: AAI, UAL
- `fleet_age`: GASS
- `fuel_surcharge_revenue_ratio`: CP
- `rail_intermodal_volume_growth`: BNI
- `revenue_days`: ASC
- `tce_day_rate`: CISS, NNA, PSHG
- `vessel_count`: ASC, CCEC, GNRT

There are 20 accepted evidence observations behind the 16 accepted pairs.
The review also identifies 104 exact hard-rejection observations. Ambiguous
years, retrieval dates, orderbook quantities, wrong-period values, and
nonissuer or segment-only values are not promoted.

## Exact policy and replay gate

`09s_build_transportation_fixture_review_policy.py` adds 124 exact policies
to the active transportation registry:

- 20 `ACCEPTED`
- 104 `REJECTED_POLICY`

The active registry contains 459 policies after the update. The generated
golden corpus and registry are hash-sealed.

`09t_build_transportation_fixture_replay_views.py` creates exact run-scoped
policy and golden views. This keeps the independent parser reusable and
prevents a policy from being evaluated against a parser run that never
contained its source document.

| Parser run | Review evaluation | Scoped policies | Golden expectations |
|---:|---:|---:|---:|
| 58 | 3 | 437 | 622 |
| 59 | 4 | 13 | 25 |
| 60 | 5 | 0 | 0 |
| 65 | 6 | 9 | 16 |

All four replays completed with identical before/after base-evidence hashes,
zero materialized evidence, and zero source-document, Arelle, EdgarTools, or
OCR operations.

## Combined coverage result

The reviewed parser union is rebuilt first. The sealed bounded financial
overrides are then applied to that result; this ordering prevents the fixture
batch from discarding the six earlier financial recoveries or the nine
formula-defined not-applicable decisions.

The combined artifact prefix is
`transportation_fixture_bounded_union`.

| State | Before fixture review | Combined final |
|---|---:|---:|
| Applicable pairs | 2,535 | 2,526 |
| Parser accepted | 49 | 66 |
| Financial derived | 129 | 135 |
| Total accepted | 178 | 201 |
| Review required | 719 | 696 |
| Discovered rejected | 351 | 357 |
| Financial inputs missing | 45 | 30 |

Accepted coverage is 201/2,526, or 7.96%. Usable coverage is 35.51% and
discovery coverage is 53.60%. Usable coverage is slightly lower than the
earlier bounded result because confirmed false positives were moved out of
the review-usable state; this is an intentional precision improvement.

For the six priority metrics, accepted pair gains are:

| Metric | Accepted before | Accepted after | Review before | Review after |
|---|---:|---:|---:|---:|
| `revenue_days` | 0 | 1 | 37 | 35 |
| `average_length_of_haul` | 0 | 4 | 26 | 22 |
| `vessel_count` | 0 | 3 | 26 | 23 |
| `fleet_capacity` | 0 | 0 | 24 | 22 |
| `tce_day_rate` | 4 | 7 | 24 | 20 |
| `fuel_surcharge_revenue_ratio` | 0 | 1 | 24 | 23 |

## Final disposition gate

`09l_freeze_transportation_final_metric_dispositions.py` now validates all
four run/evaluation mappings and their scoped policy/golden hashes. The final
gate passes with zero golden errors:

- 1 calibration candidate: `operating_ratio`
- 19 diagnostic-only metrics
- 49 deferred-review metrics
- 21 excluded metrics
- 696 deferred ticker/metric pairs
- 0 additional parser batches required

The calibration candidate set did not change. No historical panel was rebuilt
and no calibration was run in this batch. The previously frozen v3 full-panel
lineage predates this fixture freeze, so it must not be represented as the
current all-metric panel. Finish or explicitly close the remaining fixture
review first, then run one versioned historical impact/materialization pass.
If only `operating_ratio` is to proceed, a separate candidate-stability gate
must prove that its accepted evidence and point-in-time values are unchanged
before the old panel is reused.

## Commands

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09m_build_transportation_fixture_priority_batches.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09n_review_transportation_priority_fixtures.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09s_build_transportation_fixture_review_policy.py --apply
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09t_build_transportation_fixture_replay_views.py
```

Run the independent parser's policy-only replay once for each sealed view
(runs 58, 59, 60, and 65), producing evaluations 3, 4, 5, and 6. Then:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09k_build_transportation_all_source_union_coverage.py --base-evaluation-id 3 --delta-evaluation-id 4 --repair-evaluation-id 5 --direct-evaluation-id 6 --comparison-coverage-manifest transportation_all_source_union_coverage_manifest.json --artifact-prefix transportation_fixture_review_union
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09o_build_transportation_bounded_repair_coverage.py --base-coverage-prefix transportation_fixture_review_union --artifact-prefix transportation_fixture_bounded_union
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08x_adjudicate_transportation_union_evidence.py --base-evaluation-id 3 --coverage-prefix transportation_fixture_bounded_union --artifact-prefix transportation_fixture_bounded_union --reviewed-at 2026-07-29
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09a_freeze_transportation_semantic_fixtures.py --adjudication-prefix transportation_fixture_bounded_union
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09l_freeze_transportation_final_metric_dispositions.py --coverage-prefix transportation_fixture_bounded_union --financial-execution-manifest transportation_bounded_repair_execution_manifest.json
```

## Acceptance gates

- Priority scope is exactly 719 pairs and 2,007 evidence rows.
- Selected scope is exactly 234 unique pairs.
- Every selected pair has one explicit `ACCEPT`, `REJECT`, or `DEFER`.
- Only exact semantic decisions become policy rows.
- Every contributing parser run maps to one completed zero-source replay.
- All scoped golden expectations pass.
- Base evidence hashes are unchanged by replay.
- The combined coverage retains both fixture and bounded-financial gains.
- Final dispositions cover all 90 metrics.
- Additional parser batches required remains zero.

