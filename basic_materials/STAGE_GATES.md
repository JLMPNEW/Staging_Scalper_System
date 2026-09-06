# Basic Materials stage gates

## Stage 0 — independence contract

Pass requires matching model-family and sector constants, `shadow_monitor` promotion state, false portfolio and out-of-sample validity flags, output/cache paths under `output/basic_materials`, a database named `basic_materials.sqlite`, and no imports from another sector package.

## Stage 1 — database and source contract

Pass requires an empty or correctly identified Basic Materials database, matching migration checksums through schema v4, the package-owned source registry, and byte-for-byte authoritative manifests before mutation. An unidentified non-empty database is rejected. An owned v1 database may advance only through v2, v3, and v4; an owned v2 database may advance only through v3 and v4; an owned v3 database may advance only through v4.

## Stage 2 — current-universe contract

Pass requires exactly 134 unique active tickers, all required fields, valid ten-digit CIKs, exact cohort counts, exact cohort-to-parent mappings, and `calibration_group = subsector`. Every row remains visible. Current memberships are `current_source_only=1`, `survivorship_corrected=0`, and `calibration_eligible=0`.

## Stage 2B — historical candidate intake

Candidate-intake pass requires exactly 72 unique deactivated-security candidates across all eight cohorts, policy-fixed cohort counts, a matching SHA-256 manifest, valid provider symbols and asset IDs for every unblocked row, and at least 16 initial event-source URLs. Every row remains `candidate_unapproved`, `include_in_historical_universe=0`, and `calibration_eligible=0`. NSR remains explicitly provider-mapping blocked.

Candidate-intake pass does not promote history. Promotion requires effective-dated membership, ticker/security lineage, a primary-source terminal event, adjusted-price continuity, and explicit terminal economics.

## Stage 2B — governed historical reconciliation pilot

Pilot pass requires exactly 20 effective-dated historical memberships across all eight cohorts, four reviewed aliases, 22 security events, and 20 matching terminal-event rows. Every promoted membership must reconcile to the immutable candidate census on ticker, cohort, provider identity, company, industry, and quoted interval. All four CSV fingerprints and schemas must match the historical manifest.

The load must preserve all 134 current memberships, store all four raw payloads, resolve canonical securities without raw-ticker ambiguity, pass foreign-key checks, rerun idempotently, and roll back on failure. Historical memberships remain `calibration_eligible=0`. Before Stage 3, every database terminal flag must be unresolved. After Stage 3, a resolved flag is valid only when it matches the latest evidence-backed `fact_terminal_return_calculation`; resolution never activates calibration.

## Stage 3 — adjusted market data and terminal returns

Contract pass requires exactly 162 roles over 158 stable Norgate assets: 134 current, 20 historical, XLB, SPY, and six event-specific stock-successor roles. The governed CSV hashes, byte sizes, row counts, unique keys, review dates, source IDs, role counts, Stage 1/2 ticker sets, historical asset IDs, and terminal-event keys must match the Stage 3 manifest. Ticker-only historical joins are prohibited.

Provider-load pass requires an unchanged two-database Norgate fingerprint from
extraction start through publication; contracted symbol-to-asset-ID matches;
every current role in `US Equities` with open-ended quoted history and a current
major-exchange flag; identical raw and total-return date sets; unique increasing
in-window dates; positive closes; valid OHLC, volume, and dividends; canonical
per-asset cache hashes; and atomic publication of the provider snapshot, cache
manifest, bars, actions, and SPY sessions.

Coverage pass requires:

- complete XLB and SPY benchmark/calendar history;
- fresh, valid current histories;
- each current expected window beginning at the first Norgate
  major-exchange-listed session on or after the 2009-01-02 history boundary;
- at least 95% of the 134 current plus two benchmark roles rank-ready;
- strict missing-session diagnostics retained for every role;
- sparse current histories eligible only with at least 253 observations, no more than 45% missing SPY sessions, and no gap longer than 120 SPY sessions; and
- recent listings separately labeled and never mislabeled as full-history rows.

Terminal pass requires all 20 events to receive a calculation row and an explicit outcome. Fixed cash uses reviewed per-share cash. Stock conversion uses the reviewed ratio and first valid successor quote within seven calendar days. Mixed consideration preserves its reviewed allocation weights. Every quote date must be on or before the calculation as-of date. Bankruptcy/liquidation rows without verified old-equity distributions retain null values and unresolved status; zero is never inferred.

Feature pass requires one row for every current security, exclusion of every
pre-major-exchange current-security bar, only total-return-adjusted price inputs
for return features, raw close/volume for liquidity, governed XLB/SPY roles,
on-or-before-as-of data, and explicit `full`, `partial_history`,
`insufficient_history`, or `stale` quality state.

The corrected 2026-09-05 Stage 3 run passes at 136/136 rank-ready roles (100%),
with 537,739 bars, 5,648 actions, 4,446 calendar sessions, 134 feature rows, 16
resolved terminal events, and four pending bankruptcy distributions. ARIS,
AUGO, CRH, and MTA are complete; TII is rank-ready with a
`recent_listing_short_history` coverage label. Calibration and portfolio flags
remain false.

## Stage 4A — financial contract and reporting-profile census

Contract pass requires an immutable Stage 4A policy, manifest, concept map, and
reviewed profile census covering exactly 134 current and 20 historical-pilot
issuers. The contract must define source precedence, accepted filing families,
SEC acceptance time as the earliest availability boundary, amendment retention,
reporting cadence, currency rules, 22 canonical metrics, common future features,
and closed promotion flags. Listing currency may never fill missing reporting
currency.

Database pass requires schema v4 and exact loaded counts of 154 issuer profiles,
22 canonical metrics, and 66 metric-to-taxonomy concept links. All artifacts and
each reporting-profile row must pass SHA-256 validation before mutation; loading
must be atomic and idempotent; foreign keys must pass; and no filing facts,
financial features, calibration eligibility, score validity, or portfolio
authority may be activated.

The 2026-09-05 census passes Stage 4A with 140 profiles ready for ingestion, six
requiring Company Facts fallback, six requiring taxonomy review, and two
requiring annual-form review. The explicit 14-profile review queue is permitted
at this contract gate because every exception is visible and fail-closed; it must
be resolved or governed during Stage 4B before affected facts can become usable.
Every Stage 0–3 validator still passes after the schema-v4 load.

## Next implementation gate — Stage 4B point-in-time fundamentals and FX

Stage 4B must ingest SEC submissions and Company Facts and preserve SEC accession, form, acceptance timestamp, fiscal period, period start/end, taxonomy, unit, reported currency, amendment state, and source lineage. A fact may not exist before its acceptance time. US-GAAP, IFRS, Canadian, issuer-extension, annual, semiannual, and quarterly paths must be explicit. FX conversion must retain the rate, date, method, and source while keeping reported values. Missing facts remain null with reasons.

Stage 4B pass also requires common financial features and daily valuation repricing to use only governed point-in-time facts and Stage 3 prices. Every Stage 0–4A validator must still pass. Historical rows remain engineering-only until the later survivorship-correct panel gate.

## Promotion gate

`portfolio_candidate_gate` and `oos_score_valid_flag` remain false until point-in-time panels, specialized applicability, purged walk-forward out-of-sample validation, coverage thresholds, stale-data controls, and portfolio-layer acceptance tests are implemented and reviewed. Stage 0–4A output is research infrastructure, not an investment recommendation.
