# Software Infrastructure Dedicated Parser

## Ownership Boundary

- The software-infrastructure pipeline owns metric definitions, applicability,
  source scope, parser adapters, evidence review, and feature promotion.
- The shared `dedicated_parser` package supplies document parsing and shadow
  evidence infrastructure only.
- Technology code must not import `industrials` modules or use the industrials
  database, cache, manifests, adapters, or output directories.
- Semiconductor and technology-hardware integrations must use separate metric
  registries and adapters when they are implemented.

## Current Foundation

1. Install the additive shadow schema and technology compatibility views:

   ```powershell
   python technology/scripts/00a_init_technology_dedicated_parser_schema.py
   ```

2. Measure standardized-XBRL coverage and parser gaps, read-only:

   ```powershell
   python technology/software_infrastructure/scripts/07a_audit_software_infrastructure_parser_baseline.py --asof YYYY-MM-DD
   ```

3. Build the filing/document cache scope, read-only:

   ```powershell
   python technology/software_infrastructure/scripts/07b_build_software_infrastructure_parser_source_scope.py --asof YYYY-MM-DD
   ```

The source-scope output is not a parser execution manifest while any row has
`cache_status=MISSING_CACHE`. The manifest then reports
`parser_execution_allowed_flag=0`.

## Output Contracts

- Baseline:
  `output/technology_reports/software_infrastructure/dedicated_parser/baseline`
- Dated source scope:
  `output/technology_reports/software_infrastructure/dedicated_parser/<asof>`
- Document cache:
  `output/technology_cache/dedicated_parser/sec_archive_xbrl`
- Database:
  the existing `technology.sqlite`

Schema installation is additive. Baseline and source-scope commands open
SQLite in read-only, query-only mode. They never change production facts,
features, scores, calibration weights, or dashboards.

## Required Promotion Sequence

1. Hydrate only the filing documents required by the source scope.
2. Seal every input with a SHA-256 hash.
3. Run the software adapter into shared shadow evidence tables.
4. Compare parser evidence with standardized XBRL and reject conflicts.
5. Promote accepted evidence to `fact_technology_specialized_metric`.
6. Build PIT features in `feature_technology_specialized_metric`.
7. Run coverage, sign, unit, period, amendment, and look-ahead validators.
8. Measure IC and walk-forward behavior with zero production weight.
9. Promote a signal only through the existing lockbox/model-governance process.

Production promotion is intentionally not part of the current foundation.
