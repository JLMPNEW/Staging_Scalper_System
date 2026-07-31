# Software Metric Expansion Workflow

This workflow remains owned by `technology/software_infrastructure`. It does
not import industrial adapters, policies, databases, caches, or outputs.

## 1. Human Adjudication

Prepare or refresh the expansion workbook:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe `
  technology\software_infrastructure\scripts\07h_prepare_software_metric_expansion_review.py `
  --prepare
```

The workbook is:

`technology/software_infrastructure/review_policies/software_metrics_v2_adjudication_workbook.csv`

Reviewers fill only the decision columns. Source fields, source row hashes,
document hashes, and `review_source_sha256` are immutable. Re-running
`--prepare` preserves decisions only when the source seal is unchanged.

Allowed decisions are `ACCEPTED`, `CORRECTED`, and `REJECTED_POLICY`.
Accepted and corrected rows require a reviewer, UTC review timestamp, reason,
effective metric/value/period/scope, period kind, definition variant, and
calibration eligibility flag. Rejected rows must have
`calibration_eligible_flag=0`.

Validate without refreshing:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe `
  technology\software_infrastructure\scripts\07h_prepare_software_metric_expansion_review.py
```

A pending workbook is valid but not releasable. No row is auto-adjudicated.

## 2. Targeted NRR Discovery

Build the offline plan:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe `
  technology\software_infrastructure\scripts\07i_plan_software_nrr_discovery.py `
  --start-date 2010-01-01 --asof YYYY-MM-DD
```

The planner:

- scopes filings through point-in-time software membership and NRR-applicable
  cohorts;
- scans the existing technology-owned cache before requesting data;
- ranks likely NRR disclosers from existing ARR, subscription, customer, and
  NRR evidence;
- selects 30 issuers by default, including at least 8 historical issuers;
- selects up to 5 representative fiscal years per issuer;
- keeps at most one earnings-adjacent event per selected periodic filing;
- emits exact accessions for resumable hydration.

The default plan is bounded. It must not be replaced with an unbounded
all-filing download merely to increase candidate counts.

Hydrate only when no other SEC workflow is active:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe `
  technology\software_infrastructure\scripts\07c_hydrate_software_infrastructure_parser_documents.py `
  --accession-file "<nrr_discovery>/software_nrr_hydration_accessions.csv" `
  --start-date 2010-01-01 --asof YYYY-MM-DD --execute
```

After hydration, rerun `07i`, then run `07d` against the emitted sealed source
manifest. Regenerate the expansion queue only after the new parser evidence is
persisted.

## 3. Gates

Each metric family requires at least 20 reviewed examples, 5 hard negatives,
and 3 historical/delisted examples. NRR remains provisional until it meets all
three requirements.

The workflow is measurement-only. It does not change production financial
facts, scoring components, Optuna candidates, calibrated weights, ranks, or
dashboards. Calibration and production promotion remain blocked until corpus,
coverage, PIT, statistical, walk-forward, and lockbox gates pass.
