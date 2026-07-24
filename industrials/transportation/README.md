# Industrials Transportation

Transportation is a model family in the shared industrials pipeline. It uses
the shared `industrials.sqlite` database and scopes taxonomy, membership,
features, issues, and runs with `model_family=transportation`.

The canonical active and delisted inputs live in `system_csvs`. Files under
`ticker_mapping` are intake sources only and are checked for drift by the seed
validator.

Foundation command order:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\00_validate_transportation_seed.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\01_resolve_transportation_norgate_history.py
C:\Users\josel\miniconda3\python.exe industrials\scripts\00_init_industrials_db.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\01_load_transportation_universe.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\01b_load_transportation_historical_membership.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\01c_load_transportation_ticker_aliases.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\01d_load_transportation_security_continuity.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\02_validate_transportation_universe.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\02b_validate_transportation_identity_reconciliation.py
```

Market and survivorship command order:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\03_sync_transportation_prices.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\04_audit_transportation_market_data_policy.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\05_build_transportation_market_features.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\06_validate_transportation_market_stage.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\15_import_transportation_norgate_delisted_prices.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\28_export_transportation_delisted_price_contract.py
```

One-time historical raw-data bootstrap and acceptance gate:

```powershell
$asof = "2026-07-22"
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\03_sync_transportation_prices.py --start-date 2019-01-02 --asof $asof --allow-partial
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\15_import_transportation_norgate_delisted_prices.py --allow-partial
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\28_export_transportation_delisted_price_contract.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\07_sync_transportation_sec_fundamentals.py --include-historical --asof $asof --allow-partial
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\11_sync_transportation_fx_rates.py --start-date 2019-01-02 --end-date $asof --allow-partial
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\04_audit_transportation_market_data_policy.py --asof $asof --allow-partial
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\15b_validate_transportation_historical_raw_load.py --asof $asof
```

SEC runs before the final FX sync in this bootstrap so currency units newly discovered in raw
filings are included. The known transportation set is also pinned explicitly:
BRLUSD, CADUSD, CLPUSD, CNYUSD, COPUSD, EURUSD, GBPUSD, MXNUSD, and NOKUSD.
Additional reporting currencies discovered from filings, currently JPYUSD, remain included.

Script `15b` never initializes or writes the database. It opens SQLite with `mode=ro` and writes
only a 160-row coverage CSV plus a JSON acceptance manifest under
`output/industrials/transportation/historical_load`. `PASS_WITH_REVIEW` permits documented source
gaps while any missing required price series, benchmark, FX pair, membership role, or reporting
profile fails the command. Use `--strict-review` only when reviews should also fail CI.

Targeted recovery for the reviewed listing/SEC cohort is controlled and cacheable:

```powershell
$asof = "2026-07-22"
$reviews = "ABF,DDMX,EGL,ELOG,FDXF,FRTZ,NWA,SB,SWFT,VLRS"
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\07_sync_transportation_sec_fundamentals.py --include-historical --tickers $reviews --force-submissions --force-companyfacts --force-archive --asof $asof --allow-partial
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08b_audit_transportation_xbrl_tag_candidates.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\07_sync_transportation_sec_fundamentals.py --include-historical --profiles-only --profiles-all-members --asof $asof --allow-partial
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\15b_validate_transportation_historical_raw_load.py --asof $asof
```

The six security-continuity policies are fail-closed. AZUL and LTM have structural-break
boundaries; PSIG has a SPAC recapitalization boundary; ECO, HAFN, and HSHP keep Oslo and U.S.
listings as separate price series. The Oslo symbols are optional issuer proxies only after
separate equivalence/corporate-action review and NOK/USD conversion. No policy authorizes a
direct return-series append.

Financial, specialized-feature, scoring, and shadow-publication order:

```powershell
$asof = "2026-07-22"
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\05_build_transportation_market_features.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\06_validate_transportation_market_stage.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\11_sync_transportation_fx_rates.py --end-date $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\07_sync_transportation_sec_fundamentals.py --incremental --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08_build_transportation_financial_features.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08_validate_transportation_financial_stage.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08c_sync_transportation_specialized_disclosures.py --asof $asof --allow-partial
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08c_validate_transportation_specialized_disclosures.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08a_build_transportation_specialized_metrics.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08a_validate_transportation_specialized_metrics.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08d_audit_transportation_required_metric_gaps.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\10_validate_transportation_scoring_eligibility_policy.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\06a_build_transportation_scoring_features.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\06a_validate_transportation_scoring_features.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\17_publish_transportation_shadow_rank_table.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\18_validate_transportation_shadow_rank_table.py --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\20_validate_transportation_portfolio_adapter_shadow.py --asof $asof
```

This order is gated. The one-time historical raw-data bootstrap and read-only `15b` acceptance
gate must finish before current-date feature materialization. Market and financial features are
then built from the already-loaded history before metric coverage is measured. Targeted taxonomy,
concept-alias, or conditional-applicability fixes may be applied only after that measurement, after
which Stage 4 is rebuilt and revalidated. Scoring and shadow publication may proceed when the
current-date contracts pass; they do not authorize a historical parser expansion or walk-forward.

The read-only `08d` audit is the financial-parser freeze gate. It checks every still-missing
required operating-margin, FCF-margin, capex/revenue, cash-runway, and capital-raise-dependence
input against currently loaded standard US-GAAP and IFRS facts. Scoring may proceed only when the
audit reports `financial_parser_rule_freeze_status=READY`; reusable mapping candidates or approved
aliases that have not yet been remapped keep the parser unfrozen. Source/period and TTM-alignment
gaps remain explicit data gaps and are not solved with unsafe concept aliases.

Stage 4 uses the shared industrials SEC and financial infrastructure. The transportation metric
registry expands that generic data into one explicit availability row for every active
ticker/metric pair. Missing data remains missing: only `REPORTED`, `DERIVED`, or reviewed `PROXY`
values enter a score. Metrics are compared inside one of the four calibration cohorts, while the
industry tag narrows applicability for operating-ratio and purchased-transportation metrics.
The validator also writes `transportation_metric_coverage.csv` with per-metric denominators and
observed counts. Cash runway is conditionally `NOT_APPLICABLE` for issuers with zero cash burn;
capital-raise dependence is explicitly zero for those cash-generative issuers even when financing
proceeds are only partially disclosed.

Stage `08c` is intentionally bounded to the latest annual and latest interim filing per issuer.
It scans active and inactive issuers, preserves every source document in the SEC cache, and writes
source URL, document hash, accession, period, unit, evidence text, confidence, and parser status
for each candidate. Growth candidates without resolved reporting-period alignment, subjective
commercialization milestones, and conflicting values in one document remain
`REVIEW_REQUIRED`; only unambiguous accepted values can enter `08a`.

For the air cohort, issuer operating-statistics tables are parsed only when the filing resolves
the comparative period and issuer scope. RPM/RPK and ASM/ASK current/prior rows can produce
traffic and capacity growth; current load-factor and passenger-yield rows can produce levels.
Global IATA traffic, regional/submarket series, capacity-purchase costs, and per-ASM expense rows
are rejected. When quarter and year-to-date rows coexist, the shortest unambiguous duration is
selected. These rules are deliberately narrow and are now frozen for the bounded historical run.

The independent `08c` validator has separate gates for universe/taxonomy integrity, document
recovery, provenance/status, cohort signal, and historical-scale readiness. A valid bounded
parser can pass while the scale decision remains `PARSER_EXPANSION_REQUIRED`. That status blocks
the full historical specialized-disclosure backfill and avoids repeating an expensive parse
before metric coverage is adequate.

Once the bounded scale and financial-parser freeze gates pass, run the resumable historical
disclosure load and its independent validator:

```powershell
$asof = "2026-07-22"
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08c_sync_transportation_specialized_disclosures.py --historical-backfill --asof $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08e_validate_transportation_historical_disclosures.py --asof $asof
```

Historical mode is cache-first and append-safe. It scans annual filings, 10-Qs, and only those
6-Ks linked to loaded raw XBRL facts. This excludes thousands of event-driven foreign-issuer 6-Ks
that are not periodic financial disclosures. Every parsed document receives a database
checkpoint, including documents with zero candidates, so a retry processes only missing
accessions. The 2017-11-28 lower bound supplies a 400-day lookback for the first 2019-01-02
research observation. Specialized candidates older than 400 days are not carried into a PIT
snapshot.

After `08e` passes, build and freeze the point-in-time feature history:

```powershell
$asof = "2026-07-22"
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\19_build_transportation_pit_feature_history.py --end-date $asof
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\19a_validate_transportation_pit_feature_history.py
```

Stage 19 uses the shared industrials SEC-profile, market-feature, and financial-feature builders,
then materializes the transportation metric registry for the exact membership alive at each
date. Month-end is an observation and coverage cadence for this research table; it is not a
portfolio rebalance decision. Each date is resumable, writes separate stage evidence, and must
have exact market, financial, reporting-profile, and 39-metric database coverage, a `PASS` build
row, and all four nonempty snapshot CSVs before it can be skipped on a retry. The independent
validator then rechecks membership and future-data boundaries before hash-freezing the selected
panel.

The next expensive stages are authorized only in this order:

1. `08c` must report `READY_FOR_BOUNDED_HISTORICAL_BACKFILL` for every mature cohort and `08d`
   must report `financial_parser_rule_freeze_status=READY`.
2. Build and validate point-in-time historical market, financial, and specialized feature
   snapshots for active and inactive membership.
3. Freeze the eligible historical panel and feature contract.
4. Run walk-forward OOS calibration once against that frozen panel.

If item 1 fails, work only the bounded parser/review queue and rerun the coverage gate. Do not
launch the full historical specialized parse or walk-forward.

The publisher is intentionally shadow-only. It creates a dated, immutable-by-default final-rank
CSV and hash manifest; reruns require `--force`. The adapter validator proves the shared portfolio
layer can read the artifact while all investment, research-calibration, survivorship, and OOS gates
remain false.

The identity contract contains 160 reviewed Norgate mappings. All 112 active names and 46
delisted names are calibration-usable. CGI and RRTS remain retained in the 48-row delisted seed
but are excluded from calibration because Norgate still classifies those OTC symbols as current
and supplies no terminal date. The market wrappers pin `model_family=transportation`, benchmarks
`IYT,XTN,SPY`, family output paths, and the transportation policy.

The portfolio layer consumes only flat published artifacts. Its transportation source is optional,
uses the shared `industrial_family` adapter, and requires a valid OOS flag before allocation.
