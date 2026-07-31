# Industrials Transportation

Transportation is a model family in the shared industrials pipeline. It uses
the shared `industrials.sqlite` database and scopes taxonomy, membership,
features, issues, and runs with `model_family=transportation`.

The canonical active and delisted inputs live in `system_csvs`. Files under
`ticker_mapping` are intake sources only and are checked for drift by the seed
validator.

Dedicated-parser design documents:

- `DEDICATED_PARSER_INTEGRATION_PLAN.md`
- `TRANSPORTATION_SPECIALIZED_METRIC_UNIVERSE.md`

DP0 discovery-contract build and strict validation:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\00b_build_transportation_dp0_contract.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\00b_validate_transportation_dp0_contract.py
```

DP0 materializes 160 reviewed identity/archetype rows and a complete
`160 x 90 = 14,400`-row applicability scope. It records hashes for the frozen
v2 metric, feature-history, rank, and portfolio baselines. Seven non-scoring
supporting parser operands are separately frozen in a `160 x 7 = 1,120`-row
scope. The complete one-pass search set is 84 metrics: 77 final direct targets
and seven supporting operands. Both
`production_enabled` and `parser_execution_authorized` remain false; these
commands perform no filing hydration, parsing, feature rebuild, calibration,
or portfolio publication.

The transportation adapter is
`industrials/transportation/dedicated_parser_adapter.py`. Its offline contract
is fail-closed: all 84 search metrics have positive and cross-archetype
prohibited fixtures; unit, period, issuer-scope, bounds, conflict, duplicate,
and deterministic-order gates are explicit; one document is semantically
parsed once for all requested metrics; and broad discoveries remain
`REVIEW_REQUIRED`. The adapter has no production mappings.

The sector-neutral DP1 policy-replay implementation is now present in
`dedicated_parser.review_replay`, with a distinct
`dedicated_parser.policy_replay_cli` command. It persists immutable evaluation
overlays, fails closed when base evidence is already policy-mutated, limits
materialization to sealed documents and requested metrics, verifies the base
hash before and after replay, and records zero source/provider/OCR counters.
The machinery 5% strategic weight is reconciled and its registration gate
passes. DP3 now seals all 160 identities over 4,267 base and 243 supplemental
accessions, including 1,414 pre-2017 accessions for inactive legacy issuers.
All 7,761 selected documents are cached and SHA-256 verified; no gap was
approved or left unresolved. DP4 passes an exact manifest-only plan for all
4,510 accessions and all 84 parser metrics with zero cache misses.

The one-pass exhaustive search is complete. Execution run 57 parsed all 4,510
work items and persisted 61,384 evidence rows and 13,169 normalized facts.
Its final recovery-report step exposed an empty-string legacy baseline bug;
after that shared conversion was fixed, canonical run 58 resume-linked all
4,510 completed work items without reparsing. Run 58 is `COMPLETED` with zero
failed work. DP6 coverage passes for all 14,400 final scope rows, all 90
metrics, and all 1,120 supporting scope rows. No feature build or calibration
was invoked. Parser execution authorization has been returned to false.

Build and validate the census without network or database writes:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\00c_build_transportation_source_census.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\00c_validate_transportation_source_census.py --verify-content-hashes
```

The exact hydration batch is
`data/transportation_dedicated_parser_cache_gaps.csv`; the sealed manifest is
`data/transportation_dp3_source_census_manifest.json`.

The post-search coverage artifacts are under
`output/industrials/transportation/dedicated_parser/2026-07-22`. The current
coverage gate is `PASS`: 2,535 applicable final pairs, 45.64% discovery
coverage, 30.02% usable pre-adjudication coverage, and 5.09% accepted coverage.
Broad parser discoveries remain review-only by design.

DP6A applies the predeclared breadth gates without rebuilding features:
broad coverage requires the greater of five active issuers or 30% of active
applicable issuers; exact-archetype coverage requires the greater of three
issuers or 25%. It explicitly targets metrics that are one or two usable
active issuers short of either gate. The current result is:

- Six financial-derived metrics already pass with accepted coverage.
- Thirty-nine metrics pass the usable breadth gate but still require evidence
  adjudication and the mandatory precision gate.
- Five metrics are one issuer short: `ancillary_revenue_per_passenger`,
  `aviation_maintenance_intensity`, `going_concern_flag`,
  `owned_or_managed_aircraft_count`, and
  `weighted_average_lease_term_remaining`.
- Seven metrics are two issuers short: `aircraft_movements_growth`,
  `commercialization_stage`, `fleet_age`,
  `newbuild_capacity_commitments`, `offhire_or_drydock_ratio`,
  `rail_intermodal_volume_growth`, and `service_reliability_rate`.
- Twenty metrics have zero active-ticker discovery and thirteen remain below
  the bounded near-gate threshold.

The resulting queue contains 1,028 existing-evidence pairs. Priorities 1-3
select 932 pairs, all of which have evidence in the compact DP6B package:
4,157 preview rows, including derived-metric evidence resolved through parser
operands, with zero missing-evidence pairs. This is the first and cheapest
coverage-lift lane because it requires no filing reparse.

DP6A also screened 513 already-cached index pages for excluded 8-K, 6-K, and
registration filings. It found 83 bounded 8-K/8-K-A filing candidates, all
for FTAI and all based on material-event items rather than a metric-alias
match. They remain `PENDING_REVIEW`; the screen does not authorize document
hydration or parsing. No cached 6-K or registration index produced a metadata
candidate.

Rebuild the deterministic review packages locally with:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08h_build_transportation_coverage_lift_package.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08i_build_transportation_coverage_lift_evidence.py
```

Both commands are read-only with respect to the shared database, perform no
network/provider/parser calls, and do not build features or calibrate. The
next gate is manual evidence and source-candidate review, followed by
policy-only replay. Only then can projected accepted coverage determine the
reduced calibration metric set.

DP6C and DP6D implement the first conservative adjudication pass:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08j_adjudicate_transportation_coverage_evidence.py --reviewed-at 2026-07-27T12:00:00-05:00 --apply
C:\Users\josel\miniconda3\python.exe -m dedicated_parser.policy_replay_cli --db C:\Users\josel\Documents\STAGING\DB\industrials.sqlite --adapter industrials.transportation.dedicated_parser_adapter:extract_metric_evidence --policy-replay-run-id 58 --review-policy industrials\transportation\review_policies\dedicated_parser_review_policy.csv --output-json output\industrials\transportation\dedicated_parser\2026-07-22\transportation_policy_replay_manifest.json
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08k_build_transportation_post_review_coverage.py --evaluation-id 1
```

DP6C makes a positive decision only when the parser observation exactly
matches a previously accepted transportation disclosure for the same issuer,
accession, document, period, unit, and value. Three v2-to-v3 mappings are
explicitly allowed after archetype scoping: asset utilization to equipment
utilization, airline load factor to passenger load factor, and marine
TCE/day-rate to TCE day rate. No other retired composite is silently mapped.

The 610 priority-1/2 pairs now have 42 `ACCEPT`, 23 `REJECT`, and 545 `DEFER`
decisions. The policy registry contains 218 exact positive evidence policies
and 100 representative frozen-contract rejection policies. Policy replay
evaluation 1 processed all 61,384 base evidence rows, applied all 318 policies,
opened zero source documents, invoked no provider or OCR, and preserved the
run-58 base scope hash. Repeating the replay returns `idempotent_reuse=true`.
The generated 418-expectation golden corpus passes against evaluation 1.

Post-review accepted coverage increased from 129 to 171 applicable pairs
(+42), while usable coverage is unchanged because accepted rows were promoted
from the existing review population. The formal 90-metric result is one
calibration candidate, 12 diagnostic-only metrics, 52 deferred-review metrics,
and 25 insufficient-evidence exclusions. `operating_ratio` is the one metric
that currently passes accepted breadth, exact-confirmation precision, four
median accepted periods, three-year median history, and inactive-member
coverage. No priority-2 near-gate pair received a safe acceptance; those
shortfalls therefore remain open rather than being filled with ambiguous
values.

DP6E closes the source-universe blind spot before any feature rebuild. It
enumerates the SEC submissions JSON and every overlapping submissions-history
shard directly, rather than assuming that `fact_sec_filing` contains the
complete SEC disclosure universe:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08l_audit_transportation_source_exhaustion.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08m_validate_transportation_source_exhaustion.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08n_hydrate_transportation_source_metadata.py --phase submissions --execute
# Rerun 08l after submissions-history hydration, then:
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08n_hydrate_transportation_source_metadata.py --phase indexes --priority-max 3 --workers 4 --execute
# Rerun 08l and 08m after index hydration, load only missing filing registry
# metadata, and rerun 08l/08m so the final document delta has a current seal:
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08o_load_transportation_source_registry.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08o_load_transportation_source_registry.py --execute
# Inspect the sealed request count, then hydrate only the selected document delta:
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08n_hydrate_transportation_source_metadata.py --phase documents --priority-max 3 --workers 4 --execute
# Rerun 08l/08m after document hydration (including any sealed fallback),
# then build and validate the append-only parser plan:
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08p_build_transportation_delta_parser_manifest.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08q_plan_transportation_delta_parser.py
# Execute with the dependency-complete project environment; 08r keeps the
# general parser switch false and authorizes only this hash-sealed delta:
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\transportation\scripts\08r_run_transportation_sec_delta.py --workers 4 --execute
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\transportation\scripts\08s_build_transportation_sec_union_coverage.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08t_build_transportation_non_sec_residual_audit.py
# Repair only the 14 bounded PDF failures, review the repaired union, and
# seal one discovery root per issuer:
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08u_build_transportation_parser_repair_manifest.py
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\transportation\scripts\08v_run_transportation_parser_repair.py --workers 2 --execute
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08w_build_transportation_repaired_sec_union_coverage.py --base-evaluation-id 1 --artifact-prefix transportation_repaired_sec_union_pre_policy
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08x_adjudicate_transportation_union_evidence.py --base-evaluation-id 1 --coverage-prefix transportation_repaired_sec_union_pre_policy --artifact-prefix transportation_union_pre_policy
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08y_build_transportation_union_review_policy.py --base-evaluation-id 1 --adjudication-prefix transportation_union_pre_policy --apply
C:\Users\josel\miniconda3\python.exe -m dedicated_parser.policy_replay_cli --db C:\Users\josel\Documents\STAGING\DB\industrials.sqlite --adapter industrials.transportation.dedicated_parser_adapter:extract_metric_evidence --policy-replay-run-id 58 --review-policy industrials\transportation\review_policies\dedicated_parser_review_policy.csv --output-json output\industrials\transportation\dedicated_parser\2026-07-22\transportation_union_policy_replay_run58.json
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08w_build_transportation_repaired_sec_union_coverage.py --base-evaluation-id 2
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08x_adjudicate_transportation_union_evidence.py --base-evaluation-id 2
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08t_build_transportation_non_sec_residual_audit.py --coverage-prefix transportation_repaired_sec_union
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08z_build_transportation_non_sec_endpoint_manifest.py
# Freeze all parser semantics, all 697 evidence fixtures, and all 45
# financial repair formulas before any non-SEC document retrieval:
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09a_freeze_transportation_semantic_fixtures.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09b_freeze_transportation_financial_repairs.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09c_build_transportation_one_pass_preflight.py
# Enumerate every live/archive root once. The repair policy supplies only
# reviewed replacement roots and bounded archive fallbacks; retrieval and
# parsing remain disabled.
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09d_enumerate_transportation_primary_documents.py --execute --workers 4 --active-timeout-sec 30 --archive-timeout-sec 30 --max-retries 1 --max-navigation-pages-per-root 12 --max-sitemaps-per-root 8 --max-navigation-depth 2 --max-archive-year-slice-failures-per-lane 3
```

The completed SEC metadata phase hydrated all 21 referenced submissions
history files and all 23,097 required archive indexes. The final audit has
zero submissions-history, index-metadata, or database-registry gaps. It
enumerates 93,460 filings for all 160 identities, classifies 32,273 relevant
filings, and selects 23,410 accessions that were not in the sealed DP3 corpus.
The append-only registry load inserted 17,494 missing filing rows with zero
updates/deletes and was then rerun idempotently. The document phase hydrated
the exact selected delta, including 36 validated complete-submission
fallbacks for legacy numeric primary filenames.

The sealed shared-parser source contract contains 26,241 document rows and
26,219 unique content hashes. Its offline plan passes with all 160 identities,
23,410 scheduled accessions, 26,241 scheduled documents, all 84
parser-addressable metrics, and zero missing-cache accessions. The one-shot
execution produced run 59: all 23,410 work items completed, zero failed, and
40,642 new evidence rows were persisted. Run 58 remains immutable and is
combined only in the coverage-only gate; the two runs have zero accession
overlap.

The reviewed run-58/run-59 union increased discovery coverage from 45.64% to
50.49% and usable coverage from 30.02% to 34.52%. Accepted coverage remains
171 of 2,535 applicable pairs (6.75%); this is expected because newly
discovered evidence is not promoted without review. No feature, historical
panel, calibration, rank, or portfolio artifact was rebuilt.

The DP6E audit remains local and read-only. Its staged hydrator may download
only the submissions, index, or selected-document artifacts authorized by the
current hash-sealed manifest; it performs no database writes, parser calls,
feature construction, historical materialization, calibration, portfolio
work, or promotion. Generic `6-K` and material `8-K` primary documents are
retained because SEC index filenames are not a reliable negative-disclosure
test. The selector also reconciles periodic `10-K`/`10-Q`/`20-F`/`40-F`,
merger and information proxies, and `FWP` disclosures that were outside the
database-derived DP3 census. The intervening registry load is append-only:
it inserts missing `fact_sec_filing` metadata required by the shared planner,
but never updates/deletes rows or writes parser evidence. The delta manifest
then drives all 84 parser-addressable metrics on new document hashes; the six
financial-derived specialized metrics remain in the 90-metric coverage audit
without being misrouted through document parsing.

DP6E is explicitly SEC-scoped. DP6G classifies the 2,364 remaining applicable
pairs before any further retrieval: 1,556 are eligible for a single sealed
non-SEC retrieval pass, 704 already have evidence that must be adjudicated
first, 59 require targeted PDF/parser repair, and 45 require financial-input
repair. The external lanes cover issuer IR releases/decks/supplements,
non-EDGAR annual reports, operating-statistics sources, local-exchange
filings, and archived delisted-company disclosures. The final systematic
production audit occurs after those extraction lanes and final metric
dispositions are frozen, while fail-closed artifact/hash/cache audits continue
at every intermediate gate.

DP6H then repaired the exact 14 PDFs responsible for all run-59 parser failures.
Run 60 completed all 14 with zero failed work and zero remaining parser-failure
evidence. DP6I reviewed all 704 review-required pairs, promoted seven pairs
through 17 hash-exact policies in policy-only evaluation 2, and left 697 pairs
in a 1,942-row semantic-fixture queue. Accepted coverage is now 178/2,535
(7.02%). The 704-pair pre-policy coverage and adjudication are retained under
the `*_pre_policy` prefixes, so the applied policy manifest remains traceable
to immutable source hashes after the post-policy artifacts are published.

The post-repair residual has 2,357 pairs: 1,615 retrieval-eligible, 697 semantic
fixture reviews, and 45 financial-input repairs. DP6K maps every retrieval pair
to exactly one of 160 sealed issuer discovery roots—112 live issuer roots and
48 archived issuer roots—with zero unresolved mappings. Document retrieval is
still disabled at the DP6K gate pending the semantic-fixture and
financial-input repair freezes completed in DP6L-DP6N.

DP6L-DP6N completed those freezes without retrieving a document. The semantic
gate seals all 84 parser-addressable metric rules, all 697 deferred
ticker/metric fixtures, and the immutable 1,942-row evidence corpus; none of
those fixtures authorizes acceptance. The financial gate freezes six formulas,
45 pair contracts, and 92 operand requirements. It classifies 23 pairs as
repairable from already-loaded canonical facts, nine cash-runway pairs as
formula-defined not applicable, and only 13 pairs as needing new primary-source
documents.

The all-inclusive preflight reconciles all 2,357 residual pairs across all 160
issuers and all 90 metrics. Document discovery is required for 2,325 pairs:
1,615 original retrieval requirements, all 697 semantic-fixture pairs, and 13
financial-source gaps. The remaining 32 financial pairs require no new
document. Every future retrieved document is pinned to the complete applicable
84-metric parser scope for its issuer, rather than only the search term that
found the document. Retrieval and parser execution remain disabled until the
primary document URLs are enumerated, deduplicated, content-hashed, and sealed.

DP6O-DP6P completed that enumeration and root-repair seal with `PASS`. The
point-in-time manifest contains 9,268 document candidates for 102 tickers,
7,193 unique ticker/URL identities, and 4,413 archive source digests. It
quarantines 23 documents dated after the 2026-07-22 census cutoff. The 27-row
reviewed repair policy recovered 15 roots or archive lanes and leaves 12
explicit access limitations (11 issuer sites and one archive index) with
named SEC/exchange/issuer fallback lanes. It does not disable TLS validation.

DP6Q completed the compact endpoint and external-asset review with `PASS`.
All 160 endpoints now have an explicit disposition, including the 47
zero-document, 19 partial-discovery, and 12 access-limited review queues. All
130 external rows map to 26 reviewed ticker/domain policies: 111 issuer,
exchange, regulator, or issuer-authorized vendor assets are retained and 19
social, secondary-media, unrelated, or non-financial false positives are
excluded.

The frozen one-time hydration scope contains 9,249 approved documents. It
reuses 166 bodies already captured during discovery and maps the remaining
9,083 documents to 8,404 content/URL-identity requests, avoiding 679 duplicate
physical retrievals before content SHA-256 deduplication. The next gate is
`HYDRATE_HASH_AND_CONTENT_DEDUPLICATE_PRIMARY_DOCUMENTS_ONCE`. Retrieval and
parser execution remain disabled; no parser run, feature rebuild, historical
materialization, calibration, portfolio call, or production promotion has
been authorized.

After a future pre-policy base run exists, reviewed policies are evaluated
without reparsing with:

```powershell
C:\Users\josel\miniconda3\python.exe -m dedicated_parser.policy_replay_cli `
  --db C:\path\to\industrials.sqlite `
  --adapter industrials.transportation.dedicated_parser_adapter:extract_metric_evidence `
  --policy-replay-run-id <BASE_RUN_ID> `
  --review-policy industrials\transportation\review_policies\dedicated_parser_review_policy.csv
```

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

## DP6R primary-document hydration

DP6R is implemented in `primary_document_hydration.py` and
`scripts/09f_hydrate_transportation_primary_documents.py`; the full contract,
recovery lanes, and acceptance gates are documented in
`DP6R_PRIMARY_DOCUMENT_HYDRATION.md`. The complete 8,404-request ledger now has
6,562 content-ready requests, 1,840 explicit failures, and two terminal
non-financial exclusions. The canonical mapping contains 7,095 content-ready
document contexts, 2,152 source-gap contexts, 6,594 unique catalog hashes, and
98 represented tickers. The final reseal performs zero parser or network work
and remains `PASS_WITH_REQUIRED_RECOVERY` because the unavailable sources stay
explicit rather than being silently dropped.

The efficient parser sequence is implemented in scripts `09g` through `09l`
and documented in `DP6S_EFFICIENT_PARSER_BATCH.md`. DP6S closes all
residual source decisions and seals 6,591 new unique content hashes across 6,958
ticker/content contexts. DP6T plans all 84 direct parser metrics offline. DP6U
extracts each physical body once into a content-addressed text cache. Only after
that cache passes may DP6V invoke the independent parser once, resumably. Run 65
completed all 6,958 contexts with zero failures and zero re-extractions. DP6W
then builds union coverage and adjudication from stored evidence without another
source retrieval or parser invocation. DP6X (`09l`) freezes all 90 final
dispositions and selects only `operating_ratio` for calibration. The remaining
719 ambiguous pairs are fixtures, not an authorization to parse the source set
again. Feature construction, historical materialization, calibration, portfolio
writes, and production authorization remain false until their downstream gates
are executed.

The final parse-free gates are:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09k_build_transportation_all_source_union_coverage.py --base-evaluation-id 2
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08x_adjudicate_transportation_union_evidence.py --coverage-prefix transportation_all_source_union --artifact-prefix transportation_all_source_union --reviewed-at 2026-07-28
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09a_freeze_transportation_semantic_fixtures.py --adjudication-prefix transportation_all_source_union
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09b_freeze_transportation_financial_repairs.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09l_freeze_transportation_final_metric_dispositions.py
```

The bounded post-freeze repair is implemented by `bounded_repair.py` and the
scripts `09m_freeze_transportation_bounded_repair_scope.py`,
`09n_execute_transportation_bounded_repairs.py`, and
`09o_build_transportation_bounded_repair_coverage.py` (the `09m`/`09n`
prefixes are shared with the separate fixture-priority pair, so always
reference these files by full name). It must be used before feature
materialization:

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09m_freeze_transportation_bounded_repair_scope.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09n_execute_transportation_bounded_repairs.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09o_build_transportation_bounded_repair_coverage.py
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08x_adjudicate_transportation_union_evidence.py --base-evaluation-id 2 --coverage-prefix transportation_bounded_repair_union --artifact-prefix transportation_bounded_repair_union --reviewed-at 2026-07-29
C:\Users\josel\miniconda3\python.exe -m dedicated_parser.policy_replay_cli --db C:\Users\josel\Documents\STAGING\DB\industrials.sqlite --adapter industrials.transportation.dedicated_parser_adapter:extract_metric_evidence --policy-replay-run-id 58 --review-policy industrials\transportation\review_policies\dedicated_parser_review_policy.csv --output-json output\industrials\transportation\dedicated_parser\2026-07-22\transportation_bounded_repair_policy_replay_run58.json
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09a_freeze_transportation_semantic_fixtures.py --adjudication-prefix transportation_bounded_repair_union
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09l_freeze_transportation_final_metric_dispositions.py --coverage-prefix transportation_bounded_repair_union --financial-execution-manifest transportation_bounded_repair_execution_manifest.json --policy-replay-manifest transportation_bounded_repair_policy_replay_run58.json
```

Current bounded coverage is 184 accepted of 2,526 applicable pairs (7.28%),
with 35.75% usable and 53.60% discovered coverage. Six financial gaps were
recovered and nine observations were removed as formula-defined not applicable.
The remaining 30 financial gaps, 100 no-value pairs, and 719 deferred evidence
pairs are explicit. The bounded OCR/truncated-PDF recovery is documented in
`OCR_RECOVERY_IMPLEMENTATION.md`; no additional broad specialized parser batch
is authorized.

The subsequent fixture-priority batch is documented in
`DP6Y_FIXTURE_PRIORITY_REVIEW.md`. It reviewed all 93 single-value pairs and
the remaining top-six metric queue from stored evidence, applied exact
run-scoped policies through evaluations 3-6, and then overlaid the already
sealed financial repairs. The combined
`transportation_fixture_bounded_union` has 201/2,526 accepted pairs (7.96%)
and 696 deferred pairs. Only `operating_ratio` remains a calibration
candidate; no parser, feature build, or calibration ran in that batch.

DP8, DP9, G8, and DP10 are now complete. Scripts `19b` through `19e` verify the
frozen v2 inputs, materialize all 90 discovery metrics once, combine them with
the 18 frozen generic metrics, and independently validate the resulting
hash-sealed v3 panel. Exact outputs are 854,640 specialized rows and 1,025,568
complete rows across 9,496 historical memberships and 92 dates. The
calibration-subset manifest selects `fleet_utilization`, `operating_ratio`, and
`passenger_load_factor` from the same complete-panel hash; it does not trigger
another feature build. DP10 rejects the proposed flag-specific exception,
freezes the purged 52/15/19 train/validation/holdout calendar plus six embargo
dates, and authorized exactly one calibration run. See
`V3_SELECTED_FEATURE_HISTORY.md` and
`RECOMMENDED_DECISION_SEQUENCE_RESULTS.md`.

The conflict-resolved v3 panel now supersedes the old all-metric lineage.
Additional broad parser batches and feature rebuilds are not authorized.
The walk-forward outcome, split, embargo, benchmark, transaction-cost, cohort,
and optimization contracts are hash-frozen. Calibration executed exactly once,
independent validation passed, and all three specialized challengers retained a
zero final overlay after failing holdout confirmation. Production promotion
remains false. The final completion command and gates are documented in
`TRANSPORTATION_IMPLEMENTATION_COMPLETION.md`.
