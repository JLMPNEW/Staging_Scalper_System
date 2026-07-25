# Machinery Pipeline

The stage-by-stage implementation status, special financial metrics, and
acceptance gates are maintained in `IMPLEMENTATION_STATUS.md`.

This package owns machinery-specific universe, identity, scoring, ranking, and
portfolio-handoff behavior. It reuses only the shared infrastructure in
`industrials/core` and `industrials/scripts`; it does not import implementation
modules from defense, technology, biotech, or medical devices.

The machinery and defense subsectors share `industrials.sqlite`. Every
family-owned row is written with `model_family=machinery`, and loaders delete or
replace only machinery-scoped rows. Raw market and SEC facts remain shared by
ticker and source.

The canonical active seed has 114 tickers in five calibration cohorts. The
delisted seed has 50 research candidates. Norgate identity resolution is
explicit: `actual_ticker` is the historical exchange symbol and
`norgate_symbol` is the local Norgate database symbol, including Norgate's date
suffix when present. Unresolved or ambiguous mappings are never substituted
with a same-text but different issuer.

Core commands:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\00_validate_machinery_seed.py
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\01_resolve_machinery_norgate_history.py
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\15_import_machinery_norgate_prices.py --dry-run
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\17_run_machinery_refresh_pipeline.py --asof 2026-07-22 --dry-run
```

The one-time resumable financial-disclosure bootstrap adds
`--bootstrap-sec-archives`. It reuses cached SEC documents and extracts
explicit orders plus funded, authorized, or appropriated backlog tables; it never treats firm or generic
backlog or RPO as funded backlog.

Stage 7 also extracts conservative prose disclosure candidates for orders,
funded backlog, reported backlog, and RPO. Only explicit consolidated values
with a filing date, period, and currency are promoted; ambiguous segment-only
values remain `REVIEW_REQUIRED`. Reviewed issuer policies run before promotion
to canonicalize RPO/backlog aliases, aggregate approved exhaustive segments,
reject contingent or dimensional subsets, and enforce 10-K/10-Q precedence
over duplicate 8-K observations. Suppressed candidates remain in the evidence
ledger with an explicit status and reason. The standalone bounded cache
backfill is:

Orders table extraction also supports explicit `Total orders`, order-intake,
and bookings labels; document-wide amount conventions; mixed monetary/percent
columns; explicit consolidated matrix columns; and issuer-extension XBRL
contexts whose dimension member explicitly denotes the consolidated company.
Segment-only dimensions are rejected. For duplicate observations, 10-K/10-Q
facts take precedence over matching 8-K earnings exhibits, while the raw facts
remain available for audit.

The SEC archive path scans all 8-K/8-K/A filings retained since `2019-01-02`
and every eligible exhibit in each event filing. HTML is parsed directly. PDF
text extraction uses `pypdf`; optional image-only OCR uses PyMuPDF,
Pytesseract, Pillow, and a host Tesseract executable. Install the disclosure
dependencies with:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe -m pip install -r industrials\machinery\requirements-disclosures.txt
```

Reviewed issuer-hosted releases and presentations can be added to
`system_csvs/machinery_issuer_ir_documents.csv`. Each enabled row requires an
exact publication timestamp, HTTPS URL, approved domain, and optional content
hash. A consolidated scope override requires reviewer attribution. Earnings
transcripts are retained as review evidence and are never promoted directly.
The issuer-IR stage is also included in the daily orchestrator:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\07b_sync_machinery_issuer_ir_disclosures.py --asof 2026-07-20 --allow-partial
```

```powershell
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\08b_audit_machinery_disclosure_candidates.py --asof 2026-07-21 --scan-cache --limit 40 --max-filings-per-ticker 12
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\08c_audit_machinery_recoverable_coverage.py --asof 2026-07-21
```

The daily orchestrator runs the bounded 40-ticker cache pass after SEC and
issuer-IR synchronization but before Stage 4 financial projection. The scan
uses the latest availability snapshot known by the requested as-of date for
priority selection. A separate post-build audit then reports remaining gaps.
This ordering guarantees that newly promoted facts reach the same refresh's
financial, scoring, and portfolio contracts. Parser improvements can therefore
recover recent cached filings without another SEC request or a one-day
projection delay. The explicit full-history recovery is local, restartable,
and includes ended/delisted point-in-time members:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\08b_audit_machinery_disclosure_candidates.py --asof 2026-07-21 --scan-cache --scan-start-date 2018-01-01 --include-historical --limit 0 --max-filings-per-ticker 0 --resume
```

## Independent Parser Pilot

The repository-level `dedicated_parser` package now supports a machinery
shadow pilot. It reads the existing industrials database and SEC archive cache,
uses EdgarTools for local submission structure and Arelle for dimensional
XBRL, and writes only additive `sec_parser_*` shadow tables. Enable it with
`--include-dedicated-parser-shadow` and pass the Python environment containing
the pinned dependencies through `--dedicated-parser-python`. The stage is
opt-in and does not alter financial features, scoring, dashboard files, or
historical snapshots. See `dedicated_parser/README.md` for acceptance gates.
Release `0.3.0` also provides an assessment-only command, exact versioned
review policies, extraction-funnel artifacts, a fast complete-cache gate, and
explicit ticker-bounded hydration through the existing machinery SEC sync.
The initial full active-universe audit identified 33 missing accessions across
ATS, BLDP, KRNT, SHMD, and SSYS. The 50-ticker benchmark hydrated all 27 gaps
for ATS, BLDP, KRNT, and SHMD; six SSYS accessions remain outside that frozen
cohort. Parser execution can be blocked until its selected source window is
complete.

The 2018 lower bound intentionally retains prior-year evidence needed for the
2019-01-02 calibration boundary. The per-ticker scan ledger keys completion by
as-of date, scan bounds, and parser version; rerunning the same command after an
interruption skips committed tickers. Full-history mode is not a daily-refresh
mode.

Stage 08c persists a machinery-only missing-cell evidence ledger and publishes
a prioritized recovery queue. It is part of the daily orchestrator and does
not change metric values: projection defects, period/history gaps, unmapped
facts, unresolved prose, registration-statement opportunities, and issuer-IR
work remain separate recovery classes until validated evidence is loaded.

Current refreshes are monotonic. The orchestrator compares the requested date
with successful manifests, dated dashboard manifests, and machinery
availability rows before running any stage; older dates must use the historical
runner. Every attempt receives immutable run-specific orchestration files.
Only a successful non-dry run replaces the root last-success manifest; failed
and dry runs update `machinery_refresh_last_attempt.json` instead.

The Stage 4 metric audit distinguishes `calibration` gates from
`limited_universe_diagnostic` gates. Strict funded backlog and its two derived
metrics use the latter. Applicability requires explicit issuer use of a
funded, authorized, or appropriated backlog definition; government exposure
alone is insufficient. A directly reported value always overrides structural
classification. These diagnostics remain visible in coverage reports but
cannot block or be mislabeled as a broad-universe calibration gate. A diagnostic becomes
`LIMITED_UNIVERSE_READY` only after at least one valid observation exists.

The normal refresh updates the shared SEC Form 4 database before importing
machinery positioning. For the first production bootstrap, add
`--full-positioning-refresh` to populate FINRA, 13F, and IBKR history in
`market_positioning.sqlite`; later daily runs use the incremental positioning
path by default. The shared upstream runner forwards `--model-family machinery`
to its nested industrial positioning import explicitly; it never relies on the
shared config default. `--skip-network` omits both upstream database refreshes.

The historical portfolio calibration sequence is opt-in because a daily
2019-present rebuild is expensive. Add `--include-historical-backfill`; the
orchestrator runs Stage 11 with point-in-time feature rebuilds before it
publishes and validates the current-date dashboard. The orchestrated backfill
excludes the exact as-of date so Stage 10 exclusively owns that snapshot.
Each date first runs the SEC sync wrapper in local `--profiles-only` mode to
create an exact dated reporting-profile snapshot from facts already in
`industrials.sqlite`; historical backfills do not refetch SEC data per date.
Each dated output also records a deterministic build signature covering the
disclosure parser, scoring/model contracts, policy lock date, and required
metric set. `--resume-existing` reuses a date only when that signature matches;
missing or stale metadata requires `--rebuild-features`. Direct historical and
current runners hold the shared industrial refresh lock for the entire run.
`--history-start-date`
defaults to `2019-01-02`, and `--history-frequency` may be `daily` or `weekly`.

Stage 6 consumes `machinery_scoring_eligibility_policy.csv` directly. A row is
rank-ready only when its policy permits eligibility, financial confidence and
data-quality gates pass, market data are current and liquid, and all metric
availability statuses are complete. Same-date feature-source ties follow the
configured primary/fallback order rather than alphabetical source IDs.

To include the optional local Norgate import from the staging-environment
orchestrator, pass `--include-norgate-backfill --norgate-python
C:\Users\josel\miniconda3\python.exe`.

The dashboard publisher writes immutable dated portfolio contracts:

```text
output/industrials/machinery/dashboard/YYYY-MM-DD/machinery_final_rank_table.csv
output/industrials/machinery/dashboard/YYYY-MM-DD/machinery_stage11_survivorship_calibration_panel.csv
output/industrials/machinery/dashboard/YYYY-MM-DD/machinery_final_rank_table_manifest.json
```

Until an OOS calibration is sealed, every dashboard row is shadow-only and
non-investable. The survivorship-corrected sidecar may still be consumed for
research calibration when its row-level eligibility fields pass.
