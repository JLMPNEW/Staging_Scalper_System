# Software Parser Hydration And Shadow Execution

## Purpose

This workflow closes specialized software metric gaps from 10-K, 10-Q, 20-F,
40-F, 8-K, 6-K, S-1, and F-1 filings without changing production scores.

The hydrator retrieves each selected accession once and reuses valid cached
content on every later run. It stores:

- accession `index.json`;
- primary filing document;
- every text, PDF, and XBRL support attachment listed by the index;
- earnings releases and presentations, including EX-99 attachments;
- full-submission SGML.

All parser inputs are SHA-256 sealed before shadow execution.

## Cache Ownership

```text
output/technology_cache/dedicated_parser/sec_archive_xbrl/
  CIK##########/
    <accession-without-dashes>/
```

This tree is owned by technology. It is independent from all industrial
caches, databases, manifests, and adapters.

## Commands

Dry-plan a representative canary without network calls:

```powershell
python technology/software_infrastructure/scripts/07c_hydrate_software_infrastructure_parser_documents.py `
  --asof YYYY-MM-DD --tickers AI --max-filings-per-ticker 3
```

Hydrate the canary after confirming no other SEC synchronization or hydration
process is running:

```powershell
python technology/software_infrastructure/scripts/07c_hydrate_software_infrastructure_parser_documents.py `
  --asof YYYY-MM-DD --tickers AI --max-filings-per-ticker 3 --execute
```

Run the sealed canary in shadow mode:

```powershell
python technology/software_infrastructure/scripts/07d_run_software_infrastructure_parser_shadow.py `
  --asof YYYY-MM-DD `
  --source-manifest output/technology_reports/software_infrastructure/dedicated_parser/YYYY-MM-DD/hydration/software_parser_hydrated_source_manifest.csv
```

Use `--plan-only` on the shadow command to validate planner scope and hashes
without parsing.

## Safety Rules

- Never run technology SEC hydration concurrently with another SEC downloader.
- Full historical hydration defaults to `--max-filings-per-ticker 0` (unlimited).
  Use a positive filing cap only for a named canary or staged expansion batch.
- Executed accession hydration must remain uncapped at the document level.
  The default `--max-documents-per-filing 0` means complete accession content.
- Prose-derived ARR, NRR, billings, subscription revenue, and customer KPI
  values are `REVIEW_REQUIRED`.
- Only standard-taxonomy, dimensionless, deterministic XBRL facts can be
  accepted automatically in shadow evidence.
- No shadow evidence is promoted into production facts or scores by these
  commands.
- A parser or adapter release change creates new immutable work identities;
  cached source documents are not downloaded again.

## Expansion Sequence

1. Canary: several recent 8-K and periodic filings for one ticker.
2. Cohort sample: issuers with zero ARR/NRR/billings baseline coverage.
3. Current universe: eight most recent supported filings per ticker.
4. Historical members and full history: set
   `--max-filings-per-ticker 0` only after canary review.
5. Rebuild the baseline census and measure accepted/review-required coverage
   separately.
