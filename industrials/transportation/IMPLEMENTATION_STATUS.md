# Transportation Implementation Status

Status date: 2026-07-31

## Definitive historical rebuild and calibration result (2026-07-31)

The required implementation and execution sequence has been completed against the
latest available market date, 2026-07-30. The implementation and data gates pass;
the new generic model does **not** pass the statistical promotion gates.

- PIT feature history: PASS and frozen; 93 snapshots from 2019-01-02 through
  2026-07-30, 39 registered metrics, 9,607 membership rows, 374,673 metric
  availability rows, and zero validation errors.
- Daily market history: PASS for 1,904 dates. Each local price series was loaded
  once; network requests and parser invocations were both zero. The Celadon `CGI`
  cache boundary was corrected to the verified 2019-12-09 terminal membership date.
- Daily score history: PASS for all 1,904 dates with active and delisted membership
  sources present. Rank-ready breadth ranges from 12 to 84 names.
- Generic OOS panel: PASS; 78,562 rows, 35,230 eligible rows, 382 weekly snapshots,
  and independently validated train/validation/embargo/holdout splits.
- Calibration-input preflight: PASS for all seven frozen candidates. Complete-row
  coverage is 11,509 / 11,652 = 98.7727%, above the unchanged 90% minimum. The old
  zero-valuation-coverage blocker is resolved.
- Calibration procedure: PASS as an artifact and governance execution. Validation
  selected `growth_quality` without using the holdout. Its validation mean IC was
  0.03147, mean net top-basket excess return was 0.02236, and hit rate was 60.32%.
- Promotion evidence: FAIL. On the sealed holdout, `growth_quality` produced mean IC
  -0.04612, mean net top-basket excess return -0.00328, and a 36.36% hit rate. All
  four walk-forward blocks failed, for a 0% pass rate versus the 50% minimum.
- Production readiness audit: PASS as an independent audit, with
  `promotion_eligible=false` and the three statistical blockers explicitly sealed.
- Current rank and portfolio adapter: PASS at 2026-07-30 with 112 rows and 83
  rank-ready names. `oos_score_valid_flag` and the portfolio-candidate gate remain
  zero by design, so transportation remains optional and zero-allocation.

No promotion, production lock activation, or portfolio cap/source activation was
performed. Doing so would violate the unchanged holdout and walk-forward gates. A
new calibration is permitted only after genuinely new, outcome-blind observations
accumulate under the governed monitoring/recalibration contract; selecting a
different candidate after viewing this holdout would be data leakage.

## Implemented Batches

- [x] Family-scoped fail-closed configuration resolver.
- [x] Active seed: 112 reviewed rows.
- [x] Delisted seed: 48 curated rows.
- [x] Four-cohort taxonomy and universe policy.
- [x] Seed, active, historical/delisted, alias, universe, and identity scripts.
- [x] Shared family-scoped universe implementation.
- [x] Cross-family isolation tests.
- [x] Norgate symbol reconciliation: 160 reviewed mappings, 159 calibration-usable.
- [x] Exact provider final-date contracts for 46 delisted seeds plus one reviewed economic-terminal contract.
- [x] Celadon `CGI` maps only to provider `CGIP`, with a 2019-12-09 economic cutoff stored separately from the provider's continuing OTC quote series.
- [x] RRTS is the sole approved price exclusion.
- [x] Family-pinned price sync, policy audit, market-feature, and market validation wrappers.
- [x] Explicit-map Norgate delisted price importer and portfolio-layer price/event exporter.
- [x] Optional portfolio-layer shadow-source configuration with `require_oos_score_valid=true`.
- [x] Family-pinned SEC financial, financial-feature, financial-validation, eligibility, and FX wrappers.
- [x] Reporting override/graduation inputs and a reviewed 31-rule scoring eligibility policy.
- [x] Cohort/industry-aware 39-metric registry with explicit availability and provenance states.
- [x] Specialized metric matrix builder and independent coverage/status validator.
- [x] Cohort-relative scoring with required-input, liquidity, staleness, financial-confidence,
  specialized-coverage, and total-confidence gates.
- [x] Deterministic 112-row shadow rank publisher and independent rank validator.
- [x] End-to-end `industrial_family` portfolio-adapter validator.
- [x] Production foundation load in the configured shared industrials database.
- [x] Historical adjusted prices for all 112 active names and three benchmarks.
- [x] Norgate total-return histories for 47 calibration-usable delisted names.
- [x] Portfolio-layer delisted price/event contract export.
- [x] Historical SEC submissions and CompanyFacts load for all 160 active-plus-delisted names.
- [x] Nine-pair transportation FX contract, including NOKUSD, plus post-SEC currency discovery.
- [x] Read-only raw historical coverage validator and machine-readable acceptance manifest.
- [x] Primary-source security-continuity policies for AZUL, ECO, HAFN, HSHP, LTM, and PSIG.
- [x] Targeted SEC recovery for all ten reviewed issuers, including 10-12B/A and legacy
  complete-submission fixed-width statement parsing.
- [x] Reviewed VLRS IFRS revenue alias and SB/VLRS raw-tag audit; both now have canonical
  revenue and assets.
- [x] Bounded specialized-disclosure recovery for all 160 issuers, source-linked candidate
  persistence, review queue, current availability materialization, and independent acceptance
  gates.
- [x] Current-date market and financial feature materialization, metric coverage report,
  cohort-relative scoring build, deterministic shadow rank publication, and portfolio-adapter
  validation for 2026-07-22.
- [x] Air-cohort period/scope parser expansion, bounded 160-issuer rerun, historical-scale gate,
  required-financial-metric gap audit, and generic financial-parser rule freeze.
- [x] Resumable historical specialized-disclosure backfill with per-document checkpoints and an
  independent exact-coverage gate.
- [x] Active-plus-inactive point-in-time market, financial, reporting-profile, and specialized
  feature history with a frozen 92-date evidence manifest.
- [x] Dedicated-parser DP0 discovery contract: 90 unique specialized metrics, 160 reviewed
  operating-archetype assignments, 14,400 explicit ticker/metric applicability rows, immutable
  v2 baseline hashes, deterministic rebuild, and independent fail-closed validation.
- [x] Dedicated-parser operand correction: seven non-scoring supporting extraction metrics and
  a deterministic 1,120-row ticker/operand scope, closing all one-pass DP-D input gaps.
- [x] Transportation dedicated-parser adapter phase A: 84 frozen search metrics, fail-closed
  archetype applicability, one semantic parse per document, normalized-fact discovery,
  review-only broad candidates, and positive/prohibited offline fixtures for every search metric.
- [x] DP1 sector-neutral policy replay: immutable evaluation overlays, explicit CLI, exact
  policy application/materialization, base-hash preservation, idempotency, and persisted
  zero-document/provider/OCR counters without modifying existing dirty shared-parser files.
- [x] DP6A/DP6B bounded coverage-lift package: formal active breadth shortfalls, explicit
  near-gate targeting, a prioritized existing-evidence queue, derived-operand evidence
  resolution, and cached excluded-source screening with hydration and parsing disabled.
- [x] DP6C/DP6D conservative adjudication and post-review coverage: complete priority-1/2
  decisions, exact prior-accepted disclosure confirmation, policy-only replay, generated
  golden validation, historical-depth gates, and explicit metric dispositions.
- [x] DP6E source-universe reconciliation: direct SEC submissions and history-shard
  enumeration, database/DP3 gap attribution, form and per-metric inventories, resumable
  staged hydration, an append-only filing-registry loader, validation, and explicit
  downstream authorization blocks.
- [x] DP6E archive-index/document hydration and parser handoff: zero metadata or registry
  gaps, 23,410 new accessions represented by 26,241 content-hashed documents, and a passing
  complete-cache offline plan covering all 84 parser-addressable metrics.
- [x] DP6F one-shot SEC delta execution and coverage-only union: run 59 completed all 23,410
  new accessions with zero failed work, then unioned its evidence with reviewed run 58 without
  rebuilding features, calibration, ranks, or portfolio outputs.
- [x] DP6G non-SEC residual-source audit: all 2,364 unresolved applicable pairs classified
  into existing-evidence adjudication, parser/financial repair, or one-pass external-primary-
  source retrieval lanes.
- [x] DP6H targeted PDF repair: the 14 hash-sealed PDFs responsible for all 214 parser-failure
  evidence rows were reprocessed under bounded 125 MB/180-second limits in run 60, with OCR
  disabled and zero failed work or residual parser-failure states.
- [x] DP6I systematic union adjudication and policy replay: all 704 review-required pairs were
  evaluated under the exact-prior-accepted-source rule; 17 evidence rows promoted seven pairs
  through policy-only evaluation 2, while 697 pairs remain in a compact semantic-fixture queue.
- [x] DP6J/DP6K post-repair residual and endpoint seal: the residual universe is 2,357 pairs,
  including 1,615 retrieval-eligible pairs mapped to one hash-sealed discovery root for each
  of all 160 issuers (112 live issuer roots and 48 archived issuer roots).
- [x] DP6L semantic-fixture freeze: all 84 parser-addressable metric semantics, all 697
  deferred ticker/metric fixtures, and all 1,942 representative evidence rows are
  hash-sealed without accepting evidence or mutating review policies.
- [x] DP6M financial-repair freeze: all 45 missing financial-derived pairs map to six
  formula contracts and 92 source/period/unit dependency requirements; only 13 require
  new source documents, while 23 use existing facts and nine require not-applicable
  reclassification.
- [x] DP6N all-inclusive one-pass preflight: all 2,357 residual pairs, 160 endpoint roots,
  and 90 metrics reconcile, with every future document pinned to all applicable parser
  metrics rather than only the discovery term that located it.
- [x] Stage 5 positioning integration and the one bounded research walk-forward calibration.
- [x] Daily current-refresh orchestration and the final implementation/calibration
  completion gate. Production promotion is a separate evidence decision and was
  denied by the frozen holdout results rather than left as unfinished implementation.

## Verification

- Configured production database: `C:\Users\josel\Documents\STAGING\DB\industrials.sqlite`.
- Foundation: 112 active, 159 calibration-usable, and 48 delisted seeds; universe and identity
  gates PASS. The 159 usable rows comprise all active names and 47 delisted names; only RRTS
  remains an approved provider-identity exclusion.
- Active prices: 172,377 adjusted bars across all 112 active names through 2026-07-22.
- Benchmarks: IYT, XTN, and SPY all pass the 2026-07-22 current-through gate.
- Corrected market policy audit: 115 rows, zero failures, and four short-history reviews
  (AZUL, ELOG, FDXF, RUBI).
- Delisted prices: 184,706 database bars across 47 usable delisted names; the portfolio export
  contains 184,952 rows and 47 event contracts.
- FX: all nine explicitly pinned transportation pairs pass, including NOKUSD; JPYUSD is also
  retained from reporting-currency discovery.
- SEC: 160/160 reporting profiles and filing coverage, 21,147 filings, 1,704,895 raw facts,
  and 379,886 canonical mapped facts. Profiles include seven legacy/archive text-table issuers,
  two reviewed FPI hybrids, 15 IFRS issuers, and 136 US-GAAP issuers.
- Read-only raw-load gate: `PASS`, zero errors/warnings, and all 160 ticker rows PASS.
- Security continuity: six primary-source policies loaded; all former price-start reviews now
  pass without stitching structurally different or venue-specific return series.
- SB/VLRS tag audit: both have canonical revenue and assets; receivables, passenger revenue, and
  cargo/mail revenue remain excluded from total-revenue aliasing.
- Current financial build: 112/112 active rows, 111 complete and one review (`PAL:
  revenue_not_annual`). Reviewed capex and IFRS borrowing aliases were remapped for the affected
  issuers before rebuilding Stage 4.
- Required-financial-metric gap audit: `PASS` and parser freeze `READY`; all 47 remaining gaps are
  source/period gaps (39) or TTM-alignment gaps (8), with zero reusable mapping reviews and zero
  approved aliases waiting to be remapped.
- Specialized bounded scan: 160/160 issuers have recovered source documents; 373 candidates
  across 78 issuers. Conservative review controls leave 82 accepted candidates and 291
  review-required candidates rather than promoting period-ambiguous or non-issuer values.
- Current metric availability: 4,368 active ticker/metric rows under registry
  `transportation_metrics_v2_shadow`; 1,459 `REPORTED`, 70 `DERIVED`, 52
  `DISCLOSED_UNPARSED`, 697 `NOT_DISCLOSED`, and 2,090 `NOT_APPLICABLE`.
- Current metric coverage: 1,529/2,278 applicable metrics observed (67.12%); required rank
  coverage is 909/970 (93.71%). Specialized coverage is 133/552 (24.09%). Cash-generative
  development issuers are excluded from the cash-runway denominator instead of being counted as
  missing.
- Specialized scale decision: `READY_FOR_BOUNDED_HISTORICAL_BACKFILL`. Accepted active-ticker
  coverage is 25.0% surface, 40.9% air, 52.4% marine, and 13.8% development-stage. Every mature
  cohort clears the 25% gate; development-stage remains measured but is not a mature-cohort scale
  blocker.
- Historical specialized-disclosure gate: `PASS`; 3,019/3,019 eligible annual, quarterly, and
  raw-XBRL-linked 6-K filings scanned across 112 active and 48 inactive tickers, zero missing
  filings, and 4,450 source-linked candidates (1,130 accepted; 3,320 review-required).
- Point-in-time feature-history gate: `PASS`, panel status `FROZEN`; 92 observations from
  2019-01-02 through 2026-07-22, 9,496 exact membership rows, 370,344 metric-availability rows,
  all 39 metrics per member, four hashed evidence files per date, and zero future-data errors.
- Specialized PIT availability uses the conservative SEC filing date. Three after-market-close
  submissions whose UTC acceptance date preceded their SEC filing date were excluded from the
  prior close and their affected snapshots were rebuilt before the panel was frozen.
- Dedicated-parser DP0 gate: `PASS`; 90 metrics comprise 77 direct parser targets, seven
  parser-derived metrics, and six financial-derived metrics. All 112 active and 48 delisted
  identities are represented in a deterministic 14,400-row scope, including 2,535 applicable
  and 11,865 explicit `NOT_APPLICABLE` pairs. The contract records 29 development overlays,
  thirteen primary operating archetypes, the exact input/scope hashes, and the immutable v2
  baseline hashes. Seven additional non-scoring operands produce a 1,120-row supporting scope
  with 83 applicable and 1,037 explicit `NOT_APPLICABLE` pairs. The actual one-pass parser search
  set is 84 metrics. Production remains disabled and exhaustive parser execution is fail-closed
  again after completion.
- Dedicated-parser DP2 offline fixtures: 77 final direct targets plus seven supporting operands
  are registered; every one has a positive offline discovery fixture and a prohibited
  cross-archetype fixture. Unit, period, issuer-scope, value-domain, conflict, duplicate, and
  deterministic-order gates are explicit. Broad evidence remains review-only and no production
  mapping exists.
- Dedicated-parser DP1 implementation: `PASS`; the new replay API and CLI are separate from
  assessment-only `--reassess-run-id`, require an immutable pre-policy base run, preserve the
  base evidence hash, and store evaluated overlays without source-document or provider calls.
  The machinery strategic-weight assertion is reconciled to the activated 0.05 value.
- Dedicated-parser DP3 source census: `PASS`; all 160 identities have selected sources. The
  expanded active/inactive lifecycle scope contains 4,267 base and 243 supplemental accessions,
  including 1,414 pre-2017 accessions for inactive issuers. All 4,510 accessions and 7,761
  selected documents are cached and byte-hash verified, with zero approved or unresolved gaps.
- Dedicated-parser DP4 offline plan: `PASS`; 160 requested identities, 13,440 parser
  ticker-metric targets, 4,510 exact work keys, 7,761 exact documents, zero cache misses,
  unlimited limits inside the sealed manifest, Arelle and EdgarTools enabled, and OCR disabled.
- Dedicated-parser DP5 one-pass search: execution run 57 completed all 4,510 ledger work items,
  yielding 61,384 evidence rows and 13,169 normalized facts. A shared recovery-report conversion
  failed on one empty-string legacy baseline after parsing was complete. The conversion now
  treats blank/non-finite baselines as non-comparable; canonical run 58 resume-linked all 4,510
  completed work items without reparsing and completed with zero failed work.
- Dedicated-parser DP6 coverage-only gate: `PASS`; 14,400 final scope rows, 90 metric summaries,
  142 cohort-metric rows, and 1,120 supporting scope rows. Across 2,535 applicable final pairs,
  discovery coverage is 45.64%, usable pre-adjudication coverage is 30.02%, and accepted coverage
  is 5.09%. Coverage states are 129 financial-derived covered, 632 review-required covered,
  259 discovered/rejected, 137 text-hit/no-value, 1,333 searched/not-found, and 45 missing
  financial inputs. No feature build or calibration was invoked.
- Dedicated-parser DP6A coverage lift: `PASS`; six metrics pass on accepted coverage, 39 pass
  usable breadth pending adjudication, five are one active issuer short, seven are two active
  issuers short, 20 have zero active discovery, and 13 are below the bounded target. The
  deterministic queue contains 1,028 existing-evidence pairs (551 priority 1, 59 priority 2,
  322 priority 3, and 96 priority 4).
- Dedicated-parser DP6B evidence package: `PASS`; priorities 1-3 select 932 queue pairs and 964
  underlying parser-source pairs. The compact review artifact has 4,157 evidence rows, including
  491 operand-backed rows for derived metrics, and zero queue pairs without evidence.
- Cached delta-source screen: 513 excluded cached index pages inspected with zero network calls;
  83 FTAI 8-K/8-K-A filings produced 498 metric/filing review candidates based on material-event
  items. No metric-alias match and no 6-K or registration metadata candidate was found. All
  candidates remain pending; hydration and parser execution are unauthorized.
- Dedicated-parser DP6C adjudication: `PASS`; all 610 priority-1/2 pairs have an explicit
  decision—42 accepted, 23 rejected, and 545 deferred. The applied registry has 318 exact
  policies: 218 accepted observations confirmed against previously accepted transportation
  disclosures and 100 representative frozen-contract rejections.
- Policy-only replay evaluation 1: `COMPLETED`; all 61,384 base evidence rows evaluated, all 318
  policies applied, 367 evidence rows changed, zero materialized observations, zero source
  document/provider/OCR operations, and identical run-58 scope hashes before and after. A repeat
  returns idempotent reuse. All 418 generated golden expectations pass.
- Dedicated-parser DP6D post-review coverage: `PASS`; accepted applicable pairs increased from
  129 to 171 (+42), comprising 42 parser-accepted and 129 financial-derived pairs. Formal
  dispositions are one calibration candidate (`operating_ratio`), 12 diagnostic-only, 52
  deferred-review, and 25 insufficient-evidence metrics. No priority-2 near-gate pair passed the
  conservative acceptance rule.
- Dedicated-parser DP6E SEC source-universe audit: `PASS_WITH_REQUIRED_DELTA`; all 160 main
  submissions files and all 21 overlapping history shards are cached, leaving zero
  submissions-history gaps. The final audit enumerates 93,460 filings, 32,273 relevant
  filings, and 23,410 new parser accessions. All 23,097 required archive indexes are cached.
  The append-only registry load inserted 17,494 missing `fact_sec_filing` rows, performed
  zero updates/deletes, and passed an idempotent rerun with zero remaining registry gaps.
- Dedicated-parser DP6E document seal and offline plan: `PASS`; the 23,410 new accessions map
  to 26,241 exact cached document rows and 26,219 unique content hashes across all 160
  identities. The shared parser schedules all 23,410 accessions and 26,241 documents with
  zero missing-cache accessions and all 84 parser-addressable metrics enabled. The other six
  specialized metrics remain in the 90-metric contract as financial-derived metrics.
  Parser, feature, materialization, calibration, portfolio, and production authorization
  remain false.
- Dedicated-parser DP6F SEC delta: `PASS`; shadow run 59 executed all 23,410 sealed work
  items over 26,241 documents, persisted 40,642 evidence rows, and had zero failed work or
  cache gaps. Run-58 accessions and run-59 accessions have zero overlap.
- DP6F reviewed SEC-union coverage: `PASS`; the union contains 27,920 completed work items.
  Discovery coverage increased from 45.64% to 50.49% (+4.85 points) and usable coverage
  increased from 30.02% to 34.52% (+4.50 points). Accepted coverage remained 171/2,535
  applicable pairs (6.75%), so the new SEC evidence is reviewable rather than automatically
  accepted. The union states are 171 accepted/financial-derived, 704 review-required,
  289 rejected-only, 116 text-hit/no-value, 1,151 searched/not-found, 59 parser-failure-only,
  and 45 missing financial inputs.
- DP6G residual non-SEC audit: `PASS`; 2,364 unresolved pairs across all 160 identities and
  90 metrics. Of these, 1,556 are eligible for one sealed non-SEC retrieval pass, 704 must
  adjudicate existing evidence first, 59 require targeted PDF/parser-failure repair, and 45
  belong to the financial-input pipeline. Retrieval and further parser execution remain
  unauthorized.
- DP6H targeted repair: `PASS`; run 60 processed exactly 14 PDFs across 12 issuers and
  superseded all 214 run-59 failure evidence rows. It produced 83 readable evidence rows,
  zero parser failures, and converted the 59 pair-level failures into 58 searched/not-found
  and one rejected-only state.
- DP6I policy-only review: `PASS`; systematic adjudication found seven exact-confirmation
  pairs backed by 17 evidence rows. Review evaluation 2 opened zero documents and invoked
  no parser/provider/OCR path. Accepted coverage increased from 171 to 178 of 2,535
  applicable pairs (6.75% to 7.02%); 697 pairs remain explicitly deferred. The pre-policy
  704-pair coverage and adjudication are retained as hash-sealed `*_pre_policy` artifacts,
  preventing the post-policy publication from invalidating policy provenance.
- DP6J/DP6K endpoint preparation: `PASS`; 2,357 residual pairs remain, of which 1,615 are
  retrieval-eligible, 697 require semantic fixtures, and 45 require financial-input repair.
  All 1,615 retrieval pairs map to 160 sealed issuer discovery roots: 94 roots from cached
  issuer-profile metadata and 66 issuer domains inferred from cached filing links. Retrieval,
  parsing, feature construction, calibration, and portfolio publication remain unauthorized.
- DP6L semantic freeze: `PASS`; 84 metric semantic contracts, 697 fixture-pair contracts,
  and 1,942 immutable fixture evidence rows are sealed. All 697 remain review-required;
  automatic acceptance and review-policy mutation counts are zero.
- DP6M financial repair freeze: `PASS`; 45 pairs across six metrics and 22 issuers map to
  92 dependency requirements. The audit found 13 true source-retrieval gaps, 23
  alignment/formula repairs using already-loaded canonical facts, and nine cash-runway
  observations that are formula-defined not applicable.
- DP6N one-pass preflight: `PASS`; all 2,357 residual pairs reconcile across 160 issuers
  and all 90 metrics. The sealed future discovery scope contains 2,325 document-search
  requirements and 32 no-document financial repairs. Each retrieved document must be parsed
  against every applicable parser metric and supporting operand for that issuer.
- Current scoring and shadow publication: 112 rows, 26 rank-ready and 86 blocked by explicit
  coverage/liquidity/quality gates; portfolio candidates, OOS-valid rows, research-eligible rows,
  and survivorship-corrected rows all remain zero.
- Transportation suites: 355 passed in the final implementation/calibration
  regression run. The focused primary-document hydration suite has five passing
  tests.
- Full industrials regression after final completion integration: 606 passed.
- Shared dedicated-parser suite: 107 passed in the latest full regression run.
- DP1 compatibility suites: 86 dedicated-parser and 32 defense tests passed.
  The targeted machinery activation gate passes at strategic weight 0.05.
- Industrials plus portfolio-layer regression suite: 151 passed.
- Ruff: PASS for the changed transportation/shared files.

Raw-load evidence is written to
`output/industrials/transportation/historical_load/transportation_historical_raw_load_coverage.csv`
and `transportation_historical_raw_load_validation.json`. The validator opens the configured
database in SQLite read-only mode; it writes reports only.

The item-5 price-start and SEC review queues are resolved. The market minimum-history policy
still carries four expected short-history observations (AZUL, ELOG, FDXF, and RUBI), which are
not raw-load failures. Current-date feature construction, coverage measurement, scoring, shadow
publication, the portfolio-adapter contract, the historical specialized parse, and the complete
point-in-time feature panel were complete at this milestone. Walk-forward calibration was
deliberately deferred in that batch until the next research batch froze its
training/validation windows, forward-return and cost contracts, cohort treatment,
optimization bounds, and promotion gates against the panel. DP10-DP14 later completed
that exact freeze, single calibration, and independent validation sequence.

The model remains research-only. A shadow publication always writes
`portfolio_candidate_gate=0`, `oos_score_valid_flag=0`, and
`survivorship_corrected_panel_flag=0`. The optional portfolio connection therefore ingests the
rows for diagnostics but cannot allocate to them until a separate reviewed OOS promotion freezes
the calibration contract.

DP6E exhausts the declared SEC source universe, not every possible global primary source.
DP6H-DP6N completed the bounded PDF repair, exact-confirmation replay, post-repair residual
audit, endpoint-root seal, semantic-fixture freeze, financial-repair freeze, and all-inclusive
one-pass preflight.

DP6O-DP6P then completed the all-root enumeration and failed-root repair gate with `PASS`.
The sealed manifest contains 9,268 candidate documents across 102 tickers, 7,193 unique
ticker/URL identities, 4,413 archive digests, and 23 point-in-time future exclusions. The
27-row repair policy recovered 15 roots or archive lanes and records 12 reviewed access
limitations without bypassing TLS. No unreviewed root error remains.

DP6Q completed the endpoint/external-asset review and retrieval-manifest freeze with `PASS`.
All 160 endpoints have a reviewed disposition: the 47 zero-document, 19 partial, and 12
access-limited queues reconcile exactly. The 130 external rows map to 26 ticker/domain policies;
111 are retained and 19 social, secondary-media, unrelated, or non-financial false positives
are excluded. The 9,268-row reviewed document manifest contains 9,249 approved documents, reuses
166 verified discovery-cache bodies, and maps 9,083 remaining bodies to 8,404 physical hydration
requests, saving 679 duplicate requests. Retrieval and parser execution remain unauthorized.

DP6R completed all 8,404 request dispositions. The final zero-network reseal has
6,562 content-ready requests, 1,840 explicit failures, two terminal exclusions,
7,095 ready ticker/document mappings, 2,152 source-gap mappings, 6,594 unique
catalog hashes, and 98 represented tickers. No unresolved retrieval is
authorized.

DP6S and DP6T now pass. The efficient delta contains 6,591 new unique content
hashes, 6,958 ticker/content contexts, and 95,191 scoped metric contexts. The
84 direct parser metrics are frozen; seven derived metrics remain formula-only.
Completed content from parser runs 58, 59, and 60 is excluded by hash.

DP6U-DP6X are complete. DP6U cached all 6,591 unique physical documents with
zero failed extractions. DP6V run 65 completed all 6,958 logical contexts with
one semantic parser invocation, zero failed work, and zero physical
re-extractions. The corrected all-source union uses policy evaluation 2 and
contains 49 parser-accepted plus 129 financial-derived applicable pairs. Its
usable coverage is 35.38% and discovery coverage is 53.18%.

All later decisions were parse-free. The final adjudication defers 719
ambiguous pairs and creates no new policy candidate; the fixture freeze seals
2,007 evidence rows. The financial freeze keeps 23 alignment/formula gaps,
nine not-applicable cases, and 13 source/period gaps explicit. The DP6X final
90-metric freeze passes with one calibration candidate (`operating_ratio`), 54
deferred-review, 14 diagnostic-only, and 21 excluded metrics. No additional
specialized parser batch is required.

DP8, DP9, and G8 are complete. The read-only impact preflight verified every
frozen v2 snapshot and authorized only the new specialized partitions. The
one-time v3 materialization produced 854,640 specialized discovery rows and
1,025,568 complete 108-metric rows across 9,496 memberships and 92 dates. The
independent streaming validator found zero future-availability or future-period
errors and froze the panel. The calibration subset is a hash-only selection of
`operating_ratio`; no second feature materialization is required.

The walk-forward sequence later completed through DP16. The single bounded
research calibration rejected all three specialized overlays on untouched
holdout evidence; the portfolio-layer shadow adapter remains fail-closed and
the outcome-blind monitor is active. No production promotion was authorized.

### 2026-07-29 bounded residual repair

The post-freeze bounded sequence is complete and `PASS_WITH_EXPLICIT_LIMITATIONS`.
The exact 898-item scope was sealed before execution. Six financial pairs were
recovered without reparsing: four from aligned feature operands and two from
reviewed same-period cached primary-source tables (EHLD and HMR). Nine
cash-runway pairs were proven not applicable, reducing the applicable
denominator from 2,535 to 2,526. Accepted coverage is now 184 pairs (7.28%);
usable coverage is 35.75% and discovery coverage is 53.60%.

The remaining residuals are explicit: 30 financial gaps, 100 terminal
text-hit/no-value pairs, and 719 deferred stored-evidence pairs. The subsequent
OCR/truncated-PDF recovery resolved 33 of 34 source hashes; the remaining USAK
historical fact sheet is an explicit unavailable-source limitation. Its
bounded parser result did not change any ticker/metric coverage state. The
idempotent policy replay opened zero source documents.

The final disposition freeze still selects only `operating_ratio` for
calibration and records zero additional parser batches required. The one-time
selected-feature and point-in-time history build is complete and G8 passes.
`operating_ratio` has 651 historical ticker/date values across 14 issuers,
covering 28.98% of its 2,246 applicable memberships. At the end of this
bounded-repair batch the next gate was the walk-forward calibration-contract
freeze. DP10-DP14 subsequently froze, executed exactly once, and independently
validated that calibration.

### 2026-07-29 fixture-priority review

The fixture-bound efficient sequence is complete. It froze the full 719-pair
queue, reviewed all 93 single-value pairs and the remaining top-six metric
pairs (234 unique pairs total), and applied 124 exact policies without
retrieving or parsing any source document. Sixteen ticker/metric pairs were
accepted, 66 were hard-rejected, and 152 remained deferred in the selected
batch.

Run-scoped policy replays for parser runs 58, 59, 60, and 65 produced review
evaluations 3, 4, 5, and 6. All 663 scoped golden expectations pass, base
evidence hashes are unchanged, and source-document/provider/OCR counts are
zero. The reviewed parser result was combined with the prior bounded financial
repairs rather than replacing them.

The combined `transportation_fixture_bounded_union` result has 201 accepted
pairs out of 2,526 applicable pairs (7.96%), comprising 66 parser-accepted and
135 financial-derived pairs. The review queue fell from 719 to 696. Final
metric dispositions are one calibration candidate (`operating_ratio`), 19
diagnostic-only, 49 deferred-review, and 21 excluded. Zero additional parser
batches are required.

The calibration candidate set is unchanged, so this batch did not rebuild the
historical panel or run calibration. The existing v3 full-panel lineage
predates this fixture freeze; perform one versioned rebuild only after the
remaining fixture queue is explicitly closed, or first implement a
candidate-stability gate if the intent is to reuse only the unchanged
`operating_ratio` calibration input. See `DP6Y_FIXTURE_PRIORITY_REVIEW.md`.

### 2026-07-30 immediate production-readiness implementation

The post-calibration immediate batch is complete:

- Stage 0-4 production-readiness audit: `PASS`, 22 of 22 required checks,
  read-only database access, production and OOS authorization both false.
- Stage 5 upstream recovery completed in one cache-only pass over 30 already
  downloaded SEC 13F archives: 753,324 matched holdings were refreshed for
  147 active-plus-historical transportation tickers, producing 4,034
  industrial snapshots with zero network requests.
- Stage 5 local positioning import: 160 active-plus-delisted fact identities,
  exactly 112 current feature rows at 2026-07-22, 39,635 Form 4 rows, and
  2,147 13F rows.
- Stage 5 validation: `PASS`. Form 4 source coverage is 87/87 non-exempt
  active issuers at the fixed 100% gate: 85 have eligible transactions and
  DSX/TOPP have SEC ownership submissions but no eligible non-derivative
  transaction. The 25 foreign/private issuers with no SEC ownership
  submissions are explicit `not_applicable` exemptions.
- Active 13F coverage is 109/112 at the required 2026-03-31 period. ELOG and
  NCEW have no holdings in the cached quarter and FDXF is a post-quarter
  listing/spinoff; all three have time-bounded review exemptions through
  2026-10-15. The ten anchor issuers pass the availability gate.
- Final active positioning quality is 85 `complete` plus 27
  `policy_exempt`, with no unresolved required-source failures.
- FINRA history is loaded in one transportation-only pass from the existing
  cache: 176 available settlement files produced 18,087 imported rows and
  112/112 active issuer coverage. All active issuers have short shares, 110
  have percent-of-float, and 111 have a three-month change. Raw short-interest
  coverage is now a required Stage 5 gate; percent-of-float remains diagnostic,
  consistent with defense.
- IBKR historical `FEE_RATE` is loaded in one transportation-only live-session
  pass: all 112 active contracts qualified, zero tickers failed, and 150,744
  rows were written or refreshed. The imported active-plus-historical scope
  contains 152,658 rows; all 112 frozen-date active features have a non-stale
  rate. Borrow coverage is now a required Stage 5 gate. The separate live
  shortable-share snapshot was intentionally skipped, and no neutral rate was
  synthesized.
- Cross-family active-state leakage was fixed in the shared importer and
  validator. A family/source/date snapshot replacement removes stale rows.
  Defense and machinery positioning features were unchanged.
- Automatic outcome-free DP16 source exporter and guarded month-end wrapper
  are implemented. The first signal date remains 2026-07-31.
- DP16 status refreshed at 2026-07-30: `PASS`, zero of 12 signals, no outcome
  access, no recalibration, and no production promotion.

The generic baseline remains a research control, not a production-qualified
model. The specialized-overlay experiment neither defined nor passed the
separate absolute OOS, portfolio-risk, capacity, concentration, and governance
gates required to certify the generic model. Current portfolio and OOS-valid
row counts therefore correctly remain zero.

### 2026-07-30 current-data refresh completion

The current pipeline is populated through the latest available completed date,
2026-07-30. No 2026-07-31 data was requested, synthesized, or used.

- Raw market loading passed with zero failures. All 112 active securities and
  all three benchmarks (IYT, XTN, and SPY) have a 2026-07-30 adjusted-price
  bar. XTN has 3,899 bars through 2026-07-30 with a closing and adjusted
  closing value of 109.36000061035156. Yahoo timestamps the final completed
  XTN tick at 15:59:59 ET while declaring the regular-session boundary at
  16:00:00 ET; the shared loader now recognizes that one-second final-tick
  convention without accepting earlier intraday payloads.
- Required FX pairs are loaded through their latest source observations from
  2026-07-28 through 2026-07-30. The exact-date feature builders apply the
  configured PIT carry rules; no future FX value is used.
- Incremental SEC loading reached filings dated 2026-07-30 and added/confirmed
  107 transportation filings dated 2026-07-23 through 2026-07-30.
- The bounded specialized-disclosure refresh covered all 160 active and
  inactive identities, processed 319 documents, produced 399 candidates, and
  recorded zero fetch failures. The 57 `DISCLOSED_UNPARSED` v2 rows are
  explicit semantic-review states, not skipped documents or missing ticker
  rows. The completed v3 dedicated-parser freeze remains authoritative; no
  historical reparse was repeated.
- Exact 2026-07-30 derived coverage is 112/112 market rows, 112/112 financial
  rows, 112/112 positioning rows, and 4,368/4,368 metric-status rows (39 per
  ticker). Required metric coverage is 914/972, or 94.03%, with zero
  required metrics in review status.
- Positioning validation passes with 84 `complete` and 28 `policy_exempt`
  active rows. AZUL's absent 2026-Q1 13F observation is now a documented,
  time-bounded post-restructuring source exception through 2026-10-15; no zero
  holding or stale pre-restructuring holding was fabricated.
- Scoring and dashboard publication pass for all 112 active members; 27 rows
  are rank-ready. Portfolio candidates and OOS-valid rows remain zero by the
  existing fail-closed research policy.
- The isolated current v3 panel is hash-sealed with 12,096 rows:
  112 members by 108 metrics (18 generic plus 90 specialized). The
  outcome-blind monitoring source contains ten rows across the three frozen
  candidate metrics.
- DP16 passes with zero captured signals because its first permitted signal
  date is 2026-07-31. Outcomes, calibration, optimization, portfolio writes,
  and production promotion were not executed.

The current orchestrator now writes its PIT build report and manifest under
`current_panels/<asof>` instead of overwriting the frozen historical build
controls. The original 92-date historical manifest was restored byte-for-byte
to SHA-256
`05ae0107256fb035b695f8f43dcccf3d534fd570f76b68c6e5109742af176918`;
the frozen validation manifest remains
`7e029207c9f23f63735d8ad49a0dec4f26181cea20c9106b0b4c6660ebca7c5e`.
The 2026-07-30 isolated PIT adoption manifest records one complete date and
zero rebuild attempts.

### 2026-07-30 implementation and calibration completion

The final completion gate now reconciles the July 30 current pipeline, the
frozen historical panel, the exactly-once walk-forward calibration, DP15
zero-overlay decision, and the live `portfolio_layer` shadow connection.
All 12 completion gates pass without parsing, rebuilding history, accessing future outcomes,
recalibrating, or writing portfolio/production state.

The implementation is complete in validated shadow mode. Calibration is
complete with final specialized weights of zero for `fleet_utilization`,
`operating_ratio`, and `passenger_load_factor`; each validation-selected 10%
challenger failed the untouched holdout gate. Production promotion is therefore
not authorized, which is a completed evidence decision rather than a pending
implementation task. See `TRANSPORTATION_IMPLEMENTATION_COMPLETION.md`.
