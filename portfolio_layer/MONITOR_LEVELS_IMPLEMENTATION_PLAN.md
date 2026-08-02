# Monitor and Levels - Final Implementation Plan

Status: DESIGN FROZEN - ready for implementation.
Frozen: 2026-07-28.
Latest amendments:

- `MONITOR_LEVELS_PROVIDER_AMENDMENT_2026-07-31.md`
- `MONITOR_LEVELS_RETENTION_AMENDMENT_2026-07-31.md`

The 2026-07-31 amendments override Sections 2, 3, 16, and 17 where provider status, provider
selection, retention policy, coverage tiers, or implementation sequencing conflict.

Implementation status (2026-07-31):

- Increment 0 provider preflight is implemented under `expectations_monitor/`.
- `FMP_API_KEY`, `TIINGO_API_KEY`, `ALPHAVANTAGE_API_KEY`, and the separate
  `ALPHAVANTAGE_PREMIUM_API_KEY` are stored as Windows user environment variables; values are
  absent from source, configuration, manifests, and reports. The free Alpha key remains unchanged
  for established consumers.
- FMP Premium is active. Its sealed 50-symbol probe completed `PASS`: 297 of 300
  endpoint-symbol checks were available, including analyst estimates, earnings, grade actions,
  grade counts, historical ratings, and price-target consensus.
- Alpha Vantage Premium was tested on the identical 50-symbol FMP universe. It returned non-empty
  `EARNINGS_ESTIMATES` for 40/50 symbols (80.0%) with no provider/quota errors, versus FMP's 50/50.
  Alpha adds 7/30/60/90-day prior-estimate and recent revision-count fields, but failed the 90%
  coverage floor and remains `diagnostics_only` while retention rights are unconfirmed.
- Tiingo demonstrated complete EOD-price access for the sample; free news and broad fundamentals
  were unavailable.
- Normalized FMP and Alpha snapshots are authorized for provisional local/private retention under
  `provisional_retention_v1`; raw payloads remain prohibited. Provider-derived signals remain
  shadow-only and exact purge/lineage controls are mandatory.
- Increment 1 monitor foundation is operational: the isolated SQLite schema, writer lock,
  deterministic tier builder, sealed portfolio-universe synchronizer, append-only normalized
  estimate store, exact dependency lineage, and controlled purge path are implemented.
- The sealed 2026-07-24 universe contains 1,147 names: 62 Tier 0, 292 Tier 1, and 793 Tier 2.
- The first 50-symbol provisional snapshot cycle completed with zero provider/normalization errors:
  Alpha 40/50 and FMP 50/50; no raw payloads were retained.
- Provider semantics validation and monthly reconciliation are implemented. The policy never
  averages providers; it records exact comparable pairs, single-source rows, disagreement flags,
  and a gated downside candidate. It assigns no central estimate before prospective accuracy
  evidence. Exact source-snapshot and universe digests, purge dependencies, and
  superseded-artifact invalidation are enforced.
- The sealed 50-symbol v2 reconciliation passed: 778 active source rows had zero active semantic
  failures; 650 canonical active records contained 128 exact cross-provider pairs, six configured
  disagreement flags, and 522 explicit single-source records. The amended downside policy computed
  128 diagnostic candidates but authorized zero for downstream use because currency was unverified;
  ten pairs also failed the two-analyst floor. Provider-by-sector accuracy remains pending
  prospective realized outcomes and cannot be inferred from this agreement report.
- Symmetric boundary plumbing is specified and implemented at reconciliation: provider-native low/high
  ranges, intersection, outer envelope, and no-overlap state are retained without averaging. Native
  ranges remain diagnostic until prospective 10%/90% conformal residual bands meet coverage gates.
  The sealed pilot has 122 overlapping provider ranges and six no-overlap disagreements; zero
  symmetric action boundaries are eligible because reporting currency is not yet verified.
- Tiered prospective capture is implemented with deterministic 50-name batches, immutable retry
  cycles, Tier 0/1 daily cadence, Tier 2 Friday cadence, event-name priority, resume checks, and
  sealed dry-run/operational manifests.
- Read-only broker-source selection is implemented in script 49. `static` is the default and selects
  the latest non-stale `IB_reports/U*.csv` statement without connecting to IB. Static statements
  support holdings but explicitly defer current pending-order coverage. `live` connects read-only to
  the configured real-account TWS port and can feed pending-only tickers into Tier 0 through script
  39. `require_pending_orders` cannot pass from a static Activity Statement.
- The script-50 daily orchestrator is operational. It verifies the monitor universe and original
  source hashes, runs/resumes provider capture, independently validates and reconciles every cycle,
  runs/reuses the event cycle, and seals every child manifest. The accepted 2026-07-31 Tier 1 run
  returned `PASS_WITH_DEFERRED`, with six cycles, 14 hash-valid child manifests, and
  state publication disabled. The monitor enforces seven advisory states, human-only `exit_review`,
  and a permanent source-scan gate against executable IB order API methods.
- The estimate-basis contract is implemented. FMP statement currency is retained separately from
  provider estimate semantics, so an observed USD reporting currency cannot authorize an EPS or
  revenue comparison by itself. Provider currency, units, split basis, and EPS definition remain
  explicitly unverified.
- The append-only actual-outcome foundation is implemented in a hash-chained v2 ledger. FMP
  earnings dates are report dates, not fiscal-period ends; exact local calendar mapping and trusted
  release timing are required before forecasts can be scored. A three-name canary appended 24
  observations with zero eligible links, which is the correct current fail-closed result. The 24
  pre-fix canary-v1 rows are preserved only in quarantine and cannot enter active evidence.
- Exact quarterly period resolution is implemented without nearest-date inference. Alpha
  `EARNINGS` supplies historical `reportedDate` to `fiscalDateEnding` mappings; the sealed Alpha
  bulk calendar supplies prospective mappings when per-symbol history is unavailable. The live
  three-name canary retained 354 exact mappings and 353 provider-matched Alpha EPS actuals.
- Forecast/outcome linkage is provider-matched and uses a strict pre-report-date cutoff in
  `America/New_York`. Same-day snapshots are excluded, so unverified release timestamps cannot
  create lookahead. Historical canary forecasts correctly produced zero links because they were
  fetched after the associated reports.
- FMP capture now stores annual and quarterly estimates atomically. The first accepted prospective
  Tier 0 session covered 62 names in two batches: FMP passed 124/124 endpoint-symbol calls; Alpha
  returned estimates for 24 names and explicit empty coverage for 38. The pre-fix long-path session
  is sealed as invalidated, and scheduler acceptance now requires return-code, database, manifest,
  and output-hash agreement.

Normative inputs:

- `expectations_monitor/EXPECTATIONS_MONITOR_SPEC.md`
- `expectations_monitor/EXPECTATIONS_MONITOR_AMENDMENT_2026-07-28.md`
- `levels/LEVELS_ENGINE_SPEC.md`
- `MONITOR_LEVELS_PROVIDER_AMENDMENT_2026-07-31.md`
- `MONITOR_LEVELS_RETENTION_AMENDMENT_2026-07-31.md`

This document resolves the remaining implementation, data-provider, observability, and calibration
decisions. Future design changes require a dated amendment; implementation must not silently change
these contracts.

## 1. Objective

Build a provider-independent monitoring and advisory-levels system that:

- monitors every open holding, current target, investable name, and other scored name;
- detects credible thesis deterioration as early as available data permits;
- suspends unsafe additions before negative evidence is averaged away;
- distinguishes a valuation-supported opportunity from a merely lower price;
- publishes auditable valuation ranges and executable market-structure bands;
- records prospective evidence from the first shadow run;
- never changes Stage 1 scores or broker positions directly. The only authorized optimizer effect is
  the sealed `monitor_optimizer_entry_v1` entry gate; the optimizer itself re-solves the final weights.

The system is an early-warning and advisory tool. It cannot guarantee detection before every decline
or infer private/unpublished information.

## 2. Final data stack

The minimum approved stack is:

| Source | Primary responsibility | Authority |
|---|---|---|
| SEC EDGAR and issuer IR | filings, structured 8-K items, guidance, offerings, authoritative announcements | highest for company facts |
| Existing sector databases | scores, classifications, sector-specific fundamentals, catalysts, PIT history | highest for sector contracts |
| FMP Premium, conditional | estimates, analyst actions, news discovery, broad fundamentals | optional enrichment |
| Yahoo | primary broad EOD OHLCV and adjusted-price history | canonical daily market-data source |
| IBKR | secondary current quotes, bid/ask, bounded price recovery, liquidity, borrow, positions, and priority-name options | current-market confirmation authority |
| Tiingo | tertiary independent EOD OHLCV recovery and cross-check | non-authoritative recovery source |
| Existing MacroLayer and portfolio artifacts | regime, target book, covariance, liquidity, ledger | internal sealed authority |

Yahoo + IBKR + Tiingo, together with conditional FMP Premium, are sufficient for Phase 1 monitoring
and the long-only Phase 1 levels engine when SEC/issuer and internal sector data remain part of the
stack. Price-source precedence is Yahoo -> IBKR -> Tiingo. Conflicting observations are retained and
quality-gated; they are never averaged.

They are not sufficient to reconstruct historical PIT analyst consensus. Consensus begins only with
prospectively retained estimate snapshots whose storage rights and vintage semantics are verified.

## 3. Provider purchase and licensing gate

No paid provider is assumed available merely because an API key works.

Before purchasing FMP or enabling it as a retained data source, the implementation must record:

- exact endpoint access for estimates, revisions, analyst actions, news, fundamentals, and calendars;
- symbols and exchanges covered, including small-cap and delisted-name behavior;
- batch support, pagination, request limits, bandwidth, and observed latency;
- publication timestamps, fiscal-period identifiers, currency, analyst count, and split basis;
- historical-vintage semantics;
- permission to cache raw responses;
- permission to retain append-only estimate snapshots;
- permission to retain internally derived signals;
- post-subscription retention or deletion obligations;
- personal versus commercial-use classification.

The result is written to a versioned `provider_entitlements.yaml` plus a sealed capability-probe
artifact. Secrets are never included.

Decision policy:

- Start with FMP Premium only if required endpoints and retention rights pass.
- Do not start with Ultimate. Upgrade only if required batch delivery is unavailable in Premium and
  measured engineering/call-volume savings justify the price.
- If retention rights fail, FMP may be used transiently for alerts only where permitted. It cannot
  support the permanent consensus-history contract.
- Do not add EODHD or Massive initially.
- Use Tiingo as the third price source for independent recovery/cross-checking after Yahoo and IBKR;
  do not use it to overwrite a valid canonical Yahoo observation silently.
- Add Massive Options only if IB cannot supply the required priority-name IV/skew history.

Provider costs and plan features are rechecked at purchase time; no price is hard-coded into logic.

## 4. Source hierarchy and duplicate control

For conflicting facts:

```
SEC filing / issuer release
  > structured company guidance
  > direct analyst action record
  > reputable secondary reporting
  > inferred/LLM classification
```

Cross-provider copies of the same event are clustered by ticker, event driver, publication window,
filing/accession/source ID, and normalized content. The highest-authority record supplies the facts.
Secondary copies receive zero novelty and no additional LES impact.

An uncorroborated secondary report cannot create a confirmed thesis break.

## 5. Realistic observability contract

### 5.1 Objective observed facts

The system can directly record:

- event type, source, publication and availability times;
- classifier severity, credibility, novelty, relevance, and assumed half-life;
- monitor state before and after processing;
- escalation and add-suspension actions;
- stock and sector-benchmark price outcomes;
- later monitor states and subsequent detected events.

### 5.2 Derived objective outcomes

After the necessary market sessions mature, the system derives:

- 1/5/20/60/120-trading-day stock returns;
- matched-window sector ETF returns and sector-excess returns;
- daily maximum adverse and favorable excursion;
- time to recover the pre-event reference price;
- time to a further deterioration or recovery state.

Outcome timing uses `available_at_utc`, not the provider's nominal event date. The default evaluation
price is the first executable session open after the information was available. If the required open
is unavailable, the outcome remains unresolved rather than substituting a future-known price.

### 5.3 Counterfactual outcomes

"Prevented a bad addition" is not an observed fact. It is stored only as a labeled counterfactual:

```
an active level existed before the event
AND the event suspended the action
AND the frozen execution policy would otherwise have filled
THEN measure the hypothetical policy result
```

Counterfactuals never count as causal proof or realized portfolio P&L.

### 5.4 Manual and incomplete observations

True false positives and missed events require adjudication:

- `classification_false_positive`: event type was classified incorrectly;
- `materiality_false_positive`: event was real but immaterial;
- `economic_outcome_inconsistent`: event was real but price response had the opposite sign;
- `missed_event`: a detectable reference event existed but no qualifying monitor event was created.

Missed-event recall is measured against structured SEC events, labeled biotech guidance, sampled
filings/news, and post-hoc reviews of large unexplained drawdowns. It cannot measure unpublished or
unavailable information.

## 6. Monitor evidence schema

Use four append-only surfaces.

### 6.1 `monitor_event_ledger`

Immutable event and decision facts:

```
event_id
ticker
event_type
source
source_uid
published_at_utc
available_at_utc
severity
credibility
novelty
relevance
assumed_half_life_td
prior_state
new_state
action_taken
taxonomy_version
classifier_version
config_sha256
code_sha256
input_manifest_sha256
row_sequence
previous_row_sha256
row_sha256
```

### 6.2 `monitor_event_outcomes`

One append-only row per event/horizon resolution:

```
event_id
horizon_trading_days
evaluation_start_date
start_price
end_date
end_price
stock_return
sector_etf
sector_return
sector_excess_return
maximum_adverse_excursion
maximum_favorable_excursion
recovery_days
later_state
outcome_available_at_utc
data_quality_status
resolver_version
row_sha256
```

### 6.3 `monitor_policy_counterfactuals`

```
event_id
suppressed_action
prior_level_id
hypothetical_fill_date
hypothetical_fill_price
hypothetical_return
avoided_loss_or_missed_gain
policy_version
counterfactual_flag
resolved_at_utc
```

### 6.4 `monitor_event_adjudications`

```
event_id
classification_correct
materiality_correct
false_alert_label
missed_event_reference
reviewer
reviewed_at_utc
reason
supersedes_adjudication_id
```

Corrections append a superseding row; they never rewrite history.

## 7. Monitor calibration

The monitor receives the same evidence discipline as the levels engine.

Research outputs:

- post-event sector-excess returns by event category and horizon;
- realized decay curves versus configured half-lives;
- state-transition outcome tables;
- add-suspension counterfactual summaries;
- false-alert, missed-event, and coverage reports;
- source and classifier latency.

Calibration order:

1. Preserve catastrophic escalation rules as hard safety rules.
2. Estimate pooled effects for broad event categories.
3. Fit event-specific effects only when independent sample size permits.
4. Shrink sparse estimates toward category defaults.
5. Keep hand-set defaults when evidence is insufficient.
6. Validate changes with purged walk-forward tests and an untouched confirmation set.

Historical SEC/guidance replay is `historical_research`. FMP estimates/news, operational states, and
real add suspensions are prospective only. Reconstructed history cannot count as lockbox evidence.

## 8. Classifier benchmark

Build and freeze a labeled corpus from:

- existing biotech forward-guidance events;
- known earnings guidance changes;
- `NT 10-K` and `NT 10-Q`;
- structured 8-K items;
- accounting, financing, delisting, regulatory, and executive-departure events;
- sampled material and immaterial filings.

Required measurements:

- precision and recall by event type;
- recall for material/catastrophic events;
- unclassified coverage;
- detection latency;
- false-alert rate;
- source-specific performance;
- deterministic replay;
- drift after taxonomy, rules, or classifier changes.

Rules classify first. Ambiguous material events enter a manual-review queue. LLM output alone cannot
confirm a thesis break.

## 9. Structured event additions

Add explicit handling for:

- `NT 10-K` and `NT 10-Q`;
- 8-K Item 4.02, non-reliance on prior statements;
- 8-K Item 4.01, accountant/auditor change;
- 8-K Item 3.01, listing or delisting failure;
- 8-K Items 2.04 and 2.05, obligations and restructuring costs;
- 8-K Item 5.02, senior executive departures;
- dilutive registrations, ATM offerings, and material financing;
- covenant/default warnings;
- customer or contract loss;
- repeated guidance reductions.

Structured filing/form/item metadata takes precedence over keyword and LLM inference.

## 10. Forward-estimate provenance

Every forward valuation input declares:

```
forward_estimate_method
forward_estimate_source
forward_estimate_available_at_utc
forecast_horizon
normalization_method
estimate_reliability
company_guidance_flag
mechanical_extrapolation_flag
```

Allowed methods:

- `company_guidance`;
- `pit_consensus`;
- `normalized_history`;
- `trailing_run_rate`;
- `sector_specialist`;
- `unavailable`.

Mechanical extrapolations receive lower reliability and wider uncertainty penalties. An opaque
`forward` value without provenance is invalid.

## 11. Market and options data

Yahoo is the primary broad EOD OHLCV source, with retries, sealed source observations,
corporate-action checks, and final-session guards. It is not an execution authority.

IBKR supplies:

- current bid/ask and spread;
- current holdings and position quantities;
- borrow availability and rates;
- priority-name current market confirmation;
- option quotes, IV, Greeks, and open interest where subscribed.

IBKR is the second price source. It confirms current/priority-name observations and may recover
bounded gaps, with requests batched at no more than 90 instruments. It is not used to transmit
orders and does not silently replace a valid Yahoo daily bar.

Tiingo is the third price source. It supplies independent EOD recovery when Yahoo is unavailable
and IBKR confirmation is absent or operationally unsuitable. Daily production calls are
fallback-only by default to conserve quota; controlled canaries may request explicit cross-checks.
Cross-source disagreements are stored with all source values and cannot be resolved by averaging.

Options monitoring is limited to holdings, targets, active alerts, and earnings-window names. IB
requests are chunked below the account's concurrent-line limit. Missing option entitlements disable
only option-derived signals and create an explicit coverage flag.

Options signals are initially diagnostic. They require separate coverage and outcome validation
before affecting LES or entry activation.

## 12. LES aggregation hardening

- Cluster events by underlying driver before adding impact.
- Correlated duplicate evidence cannot accumulate independent novelty.
- Confirmed catastrophic events override additive point totals.
- Additive caps do not weaken escalation rules.
- Automatic same-industry peer links begin at low confidence.
- Weak peer evidence cannot create a thesis break.
- Store every component so alternate aggregation can be replayed.

## 13. Database and operational contract

The monitor database uses:

- SQLite WAL mode;
- one serialized writer;
- `busy_timeout`;
- transactional batches;
- writer lock with verified stale-lock recovery;
- atomic run exports;
- immutable raw-response envelopes;
- append-only, hash-chained evidence rows;
- code/config/input hashes in manifests;
- explicit schema and taxonomy versions.

Provider keys are read from environment variables only and are redacted from errors and request
logs.

## 14. Queues and action logic

### 14.1 Risk review

`risk_review_queue` priority is a deterministic function of:

```
portfolio exposure
x event severity
x source credibility
x market confirmation
x state deterioration
```

Credible negative fundamental evidence may suspend additions before price confirmation. Price
weakness alone creates review evidence but cannot declare the thesis broken.

### 14.2 Opportunity discovery

An actionable opportunity requires:

```
investable_eligible
AND valid/fresh valuation
AND active execution zone at or below the valuation ceiling
AND green/stable expectations
AND acceptable liquidity/data quality
AND no blocking event window
AND stabilization or positive confirmation
```

Before levels exist, emit `opportunity_observation_queue` with
`deferred_levels_unavailable`. A lower price alone is never an opportunity.

## 15. Reliability gates

Hard gates:

1. Every open holding appears in the monitor universe and EOD state artifact.
2. Every provider record has source, fetched time, available time, and payload hash.
3. Stale cache cannot create a fresh event or estimate.
4. Duplicate secondary reports add no impact.
5. Structured filing fields override inferred classifications.
6. No thesis break is confirmed solely by an LLM or uncorroborated secondary source.
7. Every event-ledger and outcome row verifies its hash chain.
8. Outcomes cannot resolve before their horizon is fully available.
9. Counterfactuals remain separate from realized outcomes.
10. Provider failure cannot stop SEC, universe, market, or holdings monitoring.
11. No active level exists for an ineligible, unvalued, stale, or suspended name.
12. Monitor/levels runs leave scores and broker ledger unchanged. The frozen entry overlay may trigger
    a second optimizer/final pass; levels and action recommendations remain advisory.

## 16. Implementation sequence

### Increment 0 - Provider and contract preflight

Deliver:

- `provider_entitlements.yaml` schema and redacted local instance;
- provider protocol and normalized record dataclasses;
- FMP capability probe;
- representative-symbol probe set;
- capability manifest and report;
- explicit proceed/defer decision for retained FMP estimate snapshots.

Definition of done:

- probe is deterministic and key-safe;
- endpoint/schema/quota failures are classified;
- no paid-plan assumption remains in code;
- retention rights are recorded or consensus is explicitly deferred.

### Increment 1 - Monitor foundation

Deliver:

- monitor configuration;
- SQLite schema and migrations;
- WAL/single-writer/locking implementation;
- three-source universe synchronization;
- held-but-unscored handling;
- sealed universe artifact and validator.

Definition of done:

- all holdings, targets, and Stage 1 names are represented exactly once;
- priority and eligibility are independent;
- rerun is idempotent;
- provider absence does not block.

### Increment 2 - Authoritative ingestion

Status: implemented 2026-07-31 in scripts 53-54 with economic-event deduplication, PIT timestamps,
local SEC/Form 4 and issuer-guidance sources, and sealed source coverage.

Deliver:

- SEC/issuer and Form 4 adapters;
- structured filing/item parser;
- raw-item deduplication and immutable cache;
- new taxonomy events from Section 9;
- feed-state and coverage reporting.

Definition of done:

- accession/form/item replay is deterministic;
- duplicate filings/news do not multiply events;
- source and availability times are PIT-correct.

### Increment 3 - Optional FMP adapter

Deliver:

- FMP adapter enabled only by capability records;
- news, analyst-action, and estimate-snapshot normalizers;
- endpoint budgets, cursors, TTLs, retry/backoff, and circuit breakers;
- immutable response envelopes;
- coverage and freshness report.

Definition of done:

- FMP outage leaves authoritative/core monitoring green with explicit WARN;
- no stale payload creates a fresh event;
- retained estimates are disabled if entitlement is not proven.

### Increment 4 - Classifier and benchmark

Status: deterministic structured classifier implemented. A labeled ambiguity corpus and optional
LLM-assisted classification remain deferred; neither is required for structured v1 events.

Deliver:

- rules-first classifier;
- golden labeled corpus;
- ambiguity/manual-review queue;
- benchmark and drift report;
- synthetic catastrophic-event probes.

Definition of done:

- per-event precision/recall/latency are reported;
- material misses fail acceptance;
- thesis-break confirmation rules are enforced;
- deterministic replay matches.

### Increment 5 - Market signals and state engine

Status: implemented 2026-07-31 in scripts 55-57. State-only evidence cannot authorize buy/add;
those states are emitted only after the levels engine passes valuation and activation gates.

Deliver:

- complete-union EOD market signals;
- IB priority-name confirmation adapter;
- LES component computation;
- asymmetric state transitions and escalation rules;
- risk-review queue;
- deferred opportunity-observation queue;
- sealed `expectations_state.csv`.

Definition of done:

- held names cannot be orphaned;
- credible negative events suspend adds immediately;
- price weakness alone cannot mark `broken`;
- components and reasons are fully auditable.

### Increment 6 - Monitor evidence and calibration plumbing

Status: publication and resolution ledgers are implemented in script 58. Monthly adjudication and
calibration reports remain prospective work after outcomes mature.

Deliver:

- four evidence tables from Section 6;
- daily outcome resolver;
- sector-relative returns and MAE/MFE;
- manual adjudication workflow;
- monthly monitor outcome report;
- historical-research versus prospective-evidence separation.

Definition of done:

- horizons resolve only when mature;
- reference prices follow availability/execution rules;
- counterfactuals never enter realized outcomes;
- hash-chain tamper and truncation probes pass.

### Increment 7 - Shared OHLCV and valuation contracts

Status as of 2026-08-01: OHLCV and valuation contract v2 are implemented. Valuation inputs are read
from `portfolio_layer/output/runs/<date>/raw/*_scores.csv`, never from mutable sector dashboards,
and each copy must match its exact Stage 1 manifest hash. The shared adapter may recover trailing
FCF/share from a same-row decimal FCF yield and price for three explicitly configured tech pipelines;
the recovered cash-flow numerator, not price, is the anchor. Sector-specialist methods require an
allowlisted method plus PIT low/base/high/confidence fields. Missing inputs remain explicit per-name
invalid contracts and do not block unrelated valid names. July 31 has intentionally not yet been
rerun under v2.

Deliver:

- split-consistent adjusted OHLCV in `expectations_monitor/51_build_monitor_ohlcv.py`;
- independent source-selection, finality, lineage, conflict, and coverage validation in
  `expectations_monitor/52_validate_monitor_ohlcv.py`;
- fixed source precedence Yahoo -> read-only IBKR -> Tiingo, with no averaging and all source
  observations retained;
- corporate-action and partial-session guards;
- sector valuation-input schema, shared trailing-FCF adapter, and specialist-range contract;
- forward-estimate provenance enforcement;
- sector adapter coverage report.

Implemented acceptance behavior:

- current-session bars are not admitted before the configured 18:00 America/New_York finality
  cutoff;
- Yahoo supplies the SPY master-session calendar and primary adjusted bars;
- IBKR requests use `readonly=True`, a maximum group size of 90, and no order API surface;
- Tiingo is called for configured priority tiers and Yahoo recovery cases;
- latest-session source conflicts above the hard threshold fail closed; historical adjusted-series
  conflicts remain sealed WARN diagnostics because provider dividend/split conventions can differ;
- Tier-0 latest-session coverage has a 98% operational target and a 90% systemic-outage hard
  floor. Coverage between those thresholds seals `PASS_WITH_WARNINGS`; affected tickers are
  isolated per name with zero market contribution, inactive entry/add bands, and
  `MARKET_DATA_UNAVAILABLE`, while current names continue through the monitor and levels chain;
- producer and independent-validator manifests hash code, configuration, universe lineage, and
  every output.
- monitor OHLCV includes a sealed SPY/sector-ETF benchmark panel used directly by market signals;
  this removes the accidental same-date Stage 2 dependency from current monitoring;
- current monitor and level publications are independent of future-label availability. Missing
  resolution data is explicit `PASS_WITH_DEFERRED`, and immutable source-alias ledgers preserve
  provenance across economically identical reseals without allowing publication drift.

Definition of done:

- all valuation inputs are PIT and source-linked;
- invalid methods fail closed;
- no market/trend field enters intrinsic valuation; the disclosed trailing-FCF reconstruction uses
  same-row price only to recover FCF/share from a published yield and never uses price as fair value.

### Increment 8 - Long-only levels engine

Status: implemented 2026-07-31 in scripts 60-64, including immutable publication and separate
120-session resolution ledgers. Runs with no active validated levels seal PASS_WITH_DEFERRED.

Deliver:

- valuation ranges and component artifact;
- uncertainty/margin-of-safety decomposition;
- market-structure candidate zones;
- valuation-ceiling and monitor-state activation;
- trim/risk outputs for all holdings;
- levels validator and prospective outcome ledger.

Definition of done:

- no active level exists without valuation and investability;
- every holding receives review/risk coverage;
- execution zones never exceed the valuation ceiling;
- upstream books remain byte-unchanged.

### Increment 9 - Shadow operation and calibration

Deliver:

- scheduled monitor and levels runs;
- coverage, false-alert, missed-event, and opportunity reports;
- purged walk-forward parameter research;
- untouched confirmation set;
- explicit retain/revise/reject recommendation.

No automatic execution is authorized. Optimizer feedback is limited to the separately sealed,
fail-closed `monitor_optimizer_entry_v1` policy described in the Stage 3 contract.

## 17. Immediate next steps

1. Close the provider semantic contract: obtain written definitions for estimate currency, units,
   split adjustment, EPS basis, revenue basis, timestamps, and post-cancellation retention. Do not
   convert the current `unverified` configuration values without source-backed evidence.
2. Continue the accepted prospective capture sequence. Tier 0 and Tier 1 are active as of
   2026-07-31; run Tier 2 on Fridays after confirming the weekly request budget. Explicit provider
   `EMPTY` rows are coverage evidence, not fabricated estimates. Never backfill current consensus
   snapshots as historical vintages.
3. Use `50_run_expectations_monitor_daily.py` as the standing monitor entry point. It owns the
   scheduler, per-cycle semantic/reconciliation validation, and script-48 event sequence, and keeps
   incomplete dependencies visible as `PASS_WITH_DEFERRED` rather than hiding them.
4. Keep `pending_orders.source_mode: static` for file-only operation. If current pending orders are
   required, change it to `live`, confirm the real-account TWS API port/account, run script 49
   read-only, and verify pending-only names enter Tier 0 before enabling `require_pending_orders`.
   Neither mode can transmit, modify, or cancel an order.
5. Publish sector-owned specialist ranges where shared methods remain unavailable. The exact v2
   fields are `sector_valuation_low`, `sector_valuation_base`, `sector_valuation_high`,
   `sector_valuation_method`, `sector_valuation_confidence`, and
   `sector_valuation_available_at_utc`. The method must be allowlisted in `levels.valuation_contract`.
   Existing alternatives remain positive `eps_forward`; positive `fcf_forward` plus
   `diluted_shares`; or positive `ebitda_forward` plus `net_debt`, `senior_claims`, and diluted
   shares. Three tech pipelines may additionally use the configured `fcf_yield_ttm` reconstruction.
   Missing methods isolate that name; they do not suspend ranges for covered names.
6. After outcomes mature, implement provider-by-sector accuracy and conformal boundary calibration.
   Until then, no central provider preference and no buy/add/trim/sell action is authorized.

Implemented evidence as of 2026-07-31:

- Tier 1: 292 names, six accepted batches, 876 endpoint-symbol requests, 715 available, 161 explicit
  empty coverage, zero hard errors; every semantic and reconciliation run PASS.
- Event cycle: 55 Tier 0/1 names in two batches, PASS after reusing sealed successful children and
  retrying only six transient Alpha failures.
- Linker: 5,250 historical diagnostics, zero eligible links, because forecasts did not predate those
  reports and metric-basis semantics were not eligible at forecast cutoff.
- Production effect: none. All provider estimates, reconciliations, outcomes, and event-cycle
  artifacts remain shadow-only and cannot change scores, weights, holdings, orders, or levels.

## 18. Explicitly deferred

- Historical consensus backfill without proven vintages.
- Automated action based on option signals.
- Fully automated peer/customer/supplier graph.
- LLM-only material-event decisions.
- Automated order placement from levels.
- Any optimizer feedback from levels, action labels, or monitor fields other than the frozen
  `monitor_optimizer_entry_v1` eligibility overlay.
- Systematic short entry levels.
- Claims that a prevented action is causal realized alpha.

These items require separate evidence or data contracts and are not prerequisites for the robust
Phase 1 monitor.
