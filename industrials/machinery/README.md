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

The canonical active seed has 113 tickers in five calibration cohorts. The
delisted seed has 51 research candidates. Norgate identity resolution is
explicit: `actual_ticker` is the historical exchange symbol and
`norgate_symbol` is the local Norgate database symbol, including Norgate's date
suffix when present. Unresolved or ambiguous mappings are never substituted
with a same-text but different issuer.

The current production refresh and portfolio contract pass as of 2026-07-24.
The complete 28-metric review is in
`ALL_METRICS_REVIEW_2026-07-24.md`.

Core commands:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\00_validate_machinery_seed.py
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\01_resolve_machinery_norgate_history.py
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\15_import_machinery_norgate_prices.py --dry-run
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\17_run_machinery_refresh_pipeline.py --asof 2026-07-24 --dry-run
```

Sealed calibration and portfolio-validation commands:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\21_run_machinery_stage8_calibration.py --force --require-stage9-ready
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\21_validate_machinery_stage8_calibration.py --require-stage9-ready
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\22_run_machinery_stage9_backtest.py --force --require-stage12-ready
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\22_validate_machinery_stage9_backtest.py --require-stage12-ready
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\23_build_machinery_stage12_governance_lock.py --force
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\23_validate_machinery_stage12_governance_lock.py
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\10b_validate_machinery_dashboard_reports.py --asof 2026-07-24
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\20_validate_machinery_portfolio_adapter.py --asof 2026-07-24 --expect-production
```

Stage 8 consumes weekly survivorship-corrected sidecars through `2025-12-31`
and never queries a price after that date. It writes component/raw-signal
diagnostics, purged splits, constrained trials, expanding walk-forward blocks,
D+1 adjusted-open execution labels, acceptance, and hash manifests under
`output/industrials/machinery/stage8`.

Stage 9 reads only those sealed files. It evaluates baseline and calibrated
weights across top-decile/top-quintile, equal/score-weighted, long-only and
long-short variants. Portfolio results use non-overlapping D+1 adjusted-open
windows, transaction and short-borrow costs, holdings-level attribution,
turnover, cohort concentration, ADV coverage, and trade-capacity limits. The
validation-only selection is `long_only_q20_equal`; its untouched holdout and
5x configured-AUM capacity gates pass. Its production-policy parity artifact
reconstructs the selected names and weights exactly across all 26 validation
and holdout periods. Stage 9 remains report-only.

Stage 12 is active as of 2026-07-24. It freezes upstream hashes, verifies the
$300K AUM contract, and applies the validated `long_only_q20_equal` policy
through the industrial-family portfolio adapter. The production file contains
113 rows, keeps 99 names broadly OOS-valid, selects 20 names, and reconciles
exactly 20 candidates to 20 adapter-investable rows. The portfolio optimizer
preserves equal weights within the machinery sleeve and produced a 4.1636%
sleeve allocation under the approved 5% cap.

Script 25 is the production transaction coordinator. On or after 5:00 p.m. ET
on the approved date it holds the master orchestration lock, runs one incremental machinery
refresh, prepares the candidate, changes only machinery `required` and its
approved 5% cap, publishes the rank file, and runs one complete strategic
portfolio smoke. The smoke requires exact Stage 1 membership, exact optimizer
membership, equal within-sleeve weights, the 5% cap, all production portfolio
groups, and a passing final-book manifest. Any failure restores the exact
portfolio config and shadow dashboard bytes. Only after that smoke passes does
the transaction write the hash-sealed production activation state consumed by
later daily scorers. The 2026-07-24 transaction passed using hash-validated
prefix reuse and a bounded downstream smoke; it did not rerun historical
snapshots or macro stages. Subsequent dates reconstruct the approved
top-quintile equal-weight policy; missing, changed, or inconsistent activation
evidence fails closed. Transaction evidence is written under
`output/industrials/machinery/stage12/activation_transactions/<date>/`.

Script 25 remains the fail-closed coordinator for a future, separately
governed activation. It must not be rerun for the already active 2026-07-24
policy.

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
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\07b_sync_machinery_issuer_ir_disclosures.py --asof 2026-07-24 --allow-partial
```

```powershell
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\08b_audit_machinery_disclosure_candidates.py --asof 2026-07-24 --scan-cache --limit 40 --max-filings-per-ticker 12
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\08c_audit_machinery_recoverable_coverage.py --asof 2026-07-24
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
C:\Users\josel\miniconda3\python.exe industrials\machinery\scripts\08b_audit_machinery_disclosure_candidates.py --asof 2026-07-24 --scan-cache --scan-start-date 2018-01-01 --include-historical --limit 0 --max-filings-per-ticker 0 --resume
```

## Independent Parser

The repository-level `dedicated_parser` package supports all sector adapters
and is production-enabled for machinery. It reuses the existing industrials
database and SEC archive cache, uses EdgarTools for local submission structure
and Arelle for dimensional XBRL, and retains additive `sec_parser_*` evidence
tables. Reviewed promotion writes only approved facts to the shared
`dedicated_parser_production` taxonomy; the machinery financial builder then
projects those facts under its normal period, scope, currency, and PIT gates.
Assessment-only runs remain available through
`--include-dedicated-parser-shadow`. See `dedicated_parser/README.md` for the
promotion and acceptance contracts.

Active-universe run 36 evaluated all 113 tickers and all 4,403 cached
accessions with zero cache gaps and zero failures. It was not promoted
wholesale because validation found three false MWA orders observations.
Adapter `v3.6` now rejects that pattern. Bounded runs 37 through 41 validated
the reviewed corrections, and promotions 8 through 12 completed with zero
conflicts. Complete results are in `ALL_METRICS_REVIEW_2026-07-24.md`.

Before rebuilding historical partitions after production promotions, run the
read-only depth and impact preflight with explicit promotion IDs:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\18a_preflight_machinery_historical_promotions.py --config industrials\machinery\config.yaml --promotion-ids 9,10,12
```

The preflight does not update the database or publish historical files. It
uses the existing survivorship sidecars as a conservative lower bound, credits
no unmaterialized promotion gains, subtracts every observation potentially
exposed to a suppression, and writes the go/no-go decision plus the exact
affected partition list under
`output/industrials/machinery/historical_backfill/preflight/`.

After a `GO_AFFECTED_PARTITIONS_ONLY` decision, materialize the exact
fingerprinted partition list with:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\18b_materialize_machinery_historical_promotions.py --config industrials\machinery\config.yaml --resume
```

The materializer reruns the preflight and refuses stale fingerprints. It
restores each historical sidecar into temporary feature rows, rebuilds only
the affected tickers, publishes and validates the full partition, then
restores the database to its exact prior state. The live end-date partition
is refreshed in place and is never compacted. Completion requires combined
coverage and the industrial-family portfolio adapter to pass all 1,900 dated
files. Promotions 9, 10, and 12 passed 689/689 affected partitions under
fingerprint
`bc2f7a17a2df2c6e27a951e2a608a53c11c8b105aee7607e39ff18d9233a8b34`.

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

Stages 8 and 9 are sealed and Stage 12 is active. The 2026-07-24 production
rank table is the live OOS contract: 20 rows are investable and 99 remain
research-eligible. The separate survivorship-corrected sidecar remains the
immutable shadow research and calibration source of record.
