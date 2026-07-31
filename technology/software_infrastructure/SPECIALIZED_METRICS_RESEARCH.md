# Software Specialized Metrics Research

## Scope

This workflow is owned by `technology/software_infrastructure`. It uses the
shared `dedicated_parser` engine, but it does not import industrial adapters,
policies, manifests, caches, databases, or outputs.

The workflow is research-only. It does not update production financial facts,
component weights, calibrated scores, Optuna candidates, ranks, or dashboards.

## Source Policy

| Channel | Metrics | Policy |
| --- | --- | --- |
| Structured XBRL primary | RPO, deferred revenue, selling and marketing | Use standardized, dimensionless XBRL first. |
| Prose primary | ARR, NRR, billings, subscription revenue, current RPO | Require human adjudication and plausibility gates. |
| Prose reconciliation/gap fill | RPO and deferred revenue | Reconcile to same-period XBRL; reject conflicts. |
| Censored event | Customer threshold counts | Keep as disclosure events; never treat as comparable levels. |
| Excluded from prose numeric panel | Selling and marketing, customer concentration | Do not infer numeric features from prose. |

Non-USD monetary candidates fail closed until a PIT FX normalization is
implemented. All accepted evidence must have a 64-character SHA-256 source
document hash.

## Sealed Release

Run:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe `
  technology\software_infrastructure\scripts\07f_seal_software_metric_adjudication.py `
  --write
```

The gate creates and validates:

- `dedicated_parser/golden_corpus/software_metrics_v1.json`
- `dedicated_parser/golden_corpus/software_metrics_policy_v3.json`
- `technology/software_infrastructure/review_policies/software_metrics_v1_policy.csv`
- `technology/software_infrastructure/data/software_metrics_v1_expansion_queue.csv`
- `technology/software_infrastructure/data/software_metrics_v1_expansion_summary.csv`
- `output/technology_reports/software_infrastructure/dedicated_parser_governance/software_metrics_v1_release_manifest.json`

The release is valid only when its source-row hashes, decision hash chain,
adapter hash, registry hash, and 23/6/48 decision counts all pass.

## Expansion Gate

Each metric family requires at least:

- 20 reviewed examples;
- 5 hard negatives;
- 3 historical or delisted member examples.

The expansion queue is sampled from already cached parser evidence using PIT
membership. A family remains provisional until
`stratified_family_certified_flag=1`. Pending rows require explicit
adjudication; they must not be auto-accepted.

## PIT Materialization

After a policy release is sealed:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe `
  technology\software_infrastructure\scripts\07g_build_software_specialized_metric_pit.py `
  --start-date 2018-01-01 --end-date YYYY-MM-DD --dry-run
```

The dry run exercises the exact database write/read path inside a rollback-only
transaction. Remove `--dry-run` only after validation passes.

Facts are keyed by issuer, metric, definition, period, source, and evidence.
Availability uses the SEC acceptance datetime. Same-period amendments use the
latest visible filing. YoY comparisons require the same definition variant and
compatible period kind. The panel uses PIT universe membership, including
historical and delisted members.

## Stage 8A

Run the existing software diagnostics:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe `
  technology\software_infrastructure\scripts\07_run_software_infrastructure_signal_diagnostics.py

C:\Users\josel\miniconda3\envs\scalper-staging\python.exe `
  technology\software_infrastructure\scripts\07_validate_software_infrastructure_signal_diagnostics.py
```

In addition to the established Spearman IC, Newey-West t-stat, and quintile
spread outputs, software measurement candidates produce:

- `measurement_signal_coverage.csv`
- `measurement_regime_ic.csv`
- `measurement_incremental_ic.csv`
- `measurement_quantile_monotonicity.csv`
- `measurement_rank_decay.csv`
- `measurement_diagnostics_summary.json`

The incremental test is partial Spearman rank IC controlling for the existing
production component composite. Regimes use the PIT 252-trading-day QQQ trend.

Insufficient coverage is a valid result. It must produce
`coverage_status=INSUFFICIENT_CROSS_SECTION`, zero production weight, and no
predictive claim.

## Promotion

No signal enters Optuna or production automatically. Promotion requires:

1. stratified corpus certification;
2. adequate PIT cross-sectional coverage;
3. positive Spearman IC with Newey-West significance;
4. quantile monotonicity and regime stability;
5. incremental IC versus existing factors;
6. acceptable decay/turnover;
7. walk-forward and net-of-cost portfolio evidence;
8. an explicit lockbox promotion decision.
