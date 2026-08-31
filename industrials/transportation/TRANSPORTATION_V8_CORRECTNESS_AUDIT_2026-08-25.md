# Transportation v8 correctness audit — 2026-08-25

## Authoritative status

The cross-sector capital verdict is
[PRODUCTION_PROMOTION_AUDIT_2026-08-25.md](../../PRODUCTION_PROMOTION_AUDIT_2026-08-25.md).
Neither document authorizes capital.

This note supersedes the conflict-resolution, coverage, calibration, and
production-readiness claims in the 2026-08-21 v8 model result. The earlier
claim that 765 of 1,707 accepted-fact conflicts were deterministically
resolved is withdrawn. Those resolutions allowed incomplete or unequal
period/scope identities to be compared. Under the strict v3 identity contract,
all 1,707 conflicts remain fail-closed.

This correction does not activate Transportation. The current state is:

- calibration execution: **PASS**;
- predictive acceptance: **FAIL**;
- production-promotion eligibility: **false**;
- Portfolio allocation: **zero cap**;
- production activation: **false**;
- canonical three-authority trust: **unconfigured, fail closed**;
- prospective evidence clock: **not started**.

An execution pass means the diagnostic ran successfully. It is not evidence
that the model has predictive acceptance.

## Conflict audit correction

The v3 resolver requires equal, complete measurement identities before a
deterministic rule can select a value. Period start, segment, denominator,
weighting basis, capacity basis, unit, definition, and evidence scope are
boundaries. A known value and a missing value are never compatible. An
all-missing identity dimension is usable only when candidates have exact
same-document and semantic-evidence identity. Unresolved values are never
averaged, and the score-time resolver cannot override a fail-closed audit
verdict.

| Item | Corrected result |
|---|---:|
| Accepted replay rows | 11,040 |
| Conflicts before v3 resolution | 1,707 |
| Deterministic v3 resolutions | 0 |
| Residual fail-closed conflicts | 1,707 |
| Period-start collisions | 631 |
| Period-start missing or mixed | 1,076 |
| Evidence-audit rows | 5,554 |

The corrected score history has 3,477 rows over 92 dates. Specialized-pack
qualifying dates are rail 38/92, LTL 85/92, truckload/intermodal 0/92,
asset-light 0/92, integrated parcel 0/92 (predictive gate not applicable), and
oil tankers 0/92. Rail and tanker therefore lose the latest-date passes that
depended on the superseded conflict treatment.

## Exact period-start recovery feasibility

The recovery census is read-only and is tied to the unchanged parser-evidence
snapshot. It does not subtract calendar months, assume a fiscal calendar,
match facts by numeric similarity, or treat an unlinked table/date phrase as a
measurement identity.

| Feasibility class | Conflict groups |
|---|---:|
| Complete but conflicting period starts | 631 |
| All candidates missing an exact bound start | 852 |
| Missing candidates with one known anchor but no exact link | 100 |
| Missing candidates with multiple known anchors | 124 |
| Exactly recoverable now without inference | **0** |

Of 5,554 conflict candidates, 1,610 already have a period start and 3,944 do
not. Among the missing candidates, 2,168 contain duration language, all 3,944
have a semantic table/block locator, 489 have a table-context hash, and one has
an explicit date range that is not linked to the KPI measurement. These are
review leads, not exact recoveries. No canonical fact, accepted replay, or
database row was changed.

## Corrected independent calibration evidence

The truth-labeled v6 calibration uses one common benchmark entry/exit interval
per cross-section, outcome-blind non-overlap selection, independently
recomputed turnover and transaction costs, and an initial long-sleeve turnover
of 1.0. Overlapping statistics are descriptive only. Early terminal proceeds
are carried as zero-return cash to the common benchmark exit; 24 such
observations remain included. Twenty-four recent cross-sections are excluded
only because they are right-censored at the panel end, and there are zero
non-censoring interval-contract failures.

The primary 63-session fixed-block verdicts are:

| Ranked group | Block 1 | Block 2 | Block 3 |
|---|---:|---:|---:|
| Rail networks | PASS | FAIL | FAIL |
| LTL carriers | FAIL | PASS | FAIL |
| Truckload/intermodal | FAIL | PASS | FAIL |
| Asset-light logistics | FAIL | FAIL | FAIL |
| Oil tankers | BLOCKED | BLOCKED | BLOCKED |

Integrated parcel is an eligibility/equal-weight sleeve with predictive gate
applicability `NOT_APPLICABLE`; it is never reported as passing all fixed
blocks. Because every ranked surface group fails at least one independent
block and the required tanker specialized pack is blocked, neither cohort can
be promoted or aggregated into a production score.

## Zero-cap shadow and Portfolio adapter

The v3 truth-bound shadow package contains all **35 of 35 locked policy
tickers**. Twenty-four rows are rank-ready under the corrected evidence; the
remaining rows stay present but fail closed. All 35 rows have portfolio,
OOS-validity, research-calibration, Stage 11, and survivorship claims set to
zero. The Portfolio `transportation_subgroup` adapter consumed all 35 rows and
returned:

- investable rows: 0;
- OOS-valid rows: 0;
- research-eligible rows: 0;
- survivorship-corrected rows: 0;
- adapter errors: 0.

The package is integration evidence only. It carries no expected alpha and no
portfolio allocation authority.

## Immutable artifacts and hashes

- Strict conflict audit:
  `output/industrials/transportation/investable_v5/fact_conflict_resolution_v3/2026-08-25/v2/transportation_v8_fact_conflict_audit.json`
  — `da45d138020611c46a5062bdc818cbbb41f3f08c017ce9d1d033048397f5ae9c`
- V3-bound score manifest:
  `output/industrials/transportation/investable_v5/subgroup_scores_v8_conflict_normalized_v3/2026-08-25/v2/transportation_v8_subgroup_score_history.json`
  — `2ad00a390f19e9781968fc708da9ec4a30def7d1fc0331454c20e97134d812be`
- Truth-labeled calibration v6:
  `output/industrials/transportation/investable_v5/subgroup_calibration_v8_independent_v3/2026-08-25/v6/transportation_v8_subgroup_calibration.json`
  — `17ea02e007e5d8f176c1c513b0de1fcb5a1f902d56b14ca54ff4c272cd399a40`
- Period-start feasibility manifest:
  `output/industrials/transportation/investable_v5/period_start_recovery_feasibility_v1/2026-08-25/v1/transportation_v8_period_start_recovery_feasibility.json`
  — `5ce42f68761740b0b2a92cdedafa385975907e05c1939beb61ea409d4cb9a638`
- V3 truth-bound shadow manifest:
  `output/industrials/transportation/subgroup_v8_shadow/2026-07-30/conflict_normalized_census_v3_truth_bound/transportation_v8_subgroup_shadow_manifest.json`
  — `495ed6c84432ae1cb65bd94bc948fcb1e5890f61d41b3e974716ddad6ec6e6df`
- Shadow rank table:
  `output/industrials/transportation/subgroup_v8_shadow/2026-07-30/conflict_normalized_census_v3_truth_bound/transportation_final_rank_table.csv`
  — `7106fbd44b0dafc1aba55051216b922a33139da677f517d4bd6b05d4ba69a86f`
- Portfolio adapter validation:
  `output/industrials/transportation/subgroup_v8_shadow/2026-07-30/conflict_normalized_census_v3_truth_bound/transportation_portfolio_adapter_validation.json`
  — `0389f5469b0ff6a83f60e4c49f0585f97265c609e12e02fc8ff3eb1266a3a40b`

## Legacy route status

The old promoter, activation, packaging, release-acceptance, and pre-canonical
future-OOS entry points are disabled as promotion authorities. Their historical
outputs may be inspected as diagnostics, but no direct script call, legacy
production lock, or local signature can override the corrected predictive
verdict or turn a shadow row investable.

The only admissible future route requires three independent authorities:
evidence-content sealing, an external append-only timestamp log, and an
independent market-data export attestation. The registry is intentionally
unconfigured and fail closed. The original `2026-08-24` signal date was missed;
it cannot be reconstructed or backdated. A new first signal may occur only after
the trust registry is approved and the exact frozen contract is externally
timestamped.

## Remaining admissible work

No automatic period-start repair is justified by the bound evidence. A future
repair must add an exact evidence-to-XBRL-context or explicit source-period
link and then create a new immutable replay; duration phrases and table
similarity are insufficient. Predictive promotion additionally requires
future-only, independently evaluated evidence under the frozen policy.
Transportation must accumulate 12 21-session and four 63-session outcomes per
governed sleeve after a valid start, pass every subgroup/sleeve gate, and obtain
a separate independent promotion review. Until all conditions are met,
Transportation remains shadow-only and zero-cap.
