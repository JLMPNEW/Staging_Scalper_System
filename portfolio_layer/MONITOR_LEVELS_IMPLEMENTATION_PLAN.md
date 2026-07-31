# Monitor and Levels - Final Implementation Plan

Status: DESIGN FROZEN - ready for implementation.
Frozen: 2026-07-28.

Normative inputs:

- `expectations_monitor/EXPECTATIONS_MONITOR_SPEC.md`
- `expectations_monitor/EXPECTATIONS_MONITOR_AMENDMENT_2026-07-28.md`
- `levels/LEVELS_ENGINE_SPEC.md`

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
- never changes Stage 1 scores, optimizer targets, final books, or broker positions.

The system is an early-warning and advisory tool. It cannot guarantee detection before every decline
or infer private/unpublished information.

## 2. Final data stack

The minimum approved stack is:

| Source | Primary responsibility | Authority |
|---|---|---|
| SEC EDGAR and issuer IR | filings, structured 8-K items, guidance, offerings, authoritative announcements | highest for company facts |
| Existing sector databases | scores, classifications, sector-specific fundamentals, catalysts, PIT history | highest for sector contracts |
| FMP Premium, conditional | estimates, analyst actions, news discovery, broad fundamentals | optional enrichment |
| IBKR | current quotes, bid/ask, liquidity, borrow, positions, and priority-name options | execution/current-market authority |
| Yahoo | broad EOD OHLCV and recovery source | non-authoritative market fallback |
| Existing MacroLayer and portfolio artifacts | regime, target book, covariance, liquidity, ledger | internal sealed authority |

IBKR + Yahoo + conditional FMP Premium are sufficient for Phase 1 monitoring and the long-only
Phase 1 levels engine when SEC/issuer and internal sector data remain part of the stack.

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
- Do not add EODHD, Tiingo, Massive, or paid Alpha Vantage initially.
- Add Tiingo only after Yahoo fails a pre-registered EOD reliability threshold.
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

Yahoo supplies broad EOD OHLCV with retries, immutable cache envelopes, corporate-action checks, and
cross-source validation. It is not an execution authority.

IBKR supplies:

- current bid/ask and spread;
- current holdings and position quantities;
- borrow availability and rates;
- priority-name current market confirmation;
- option quotes, IV, Greeks, and open interest where subscribed.

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
12. Monitor/levels runs leave scores, optimizer artifacts, final books, and broker ledger unchanged.

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

Deliver:

- Stage 2 split-consistent OHLCV;
- corporate-action and partial-session guards;
- sector valuation-input schema;
- forward-estimate provenance enforcement;
- sector adapter coverage report.

Definition of done:

- all valuation inputs are PIT and source-linked;
- invalid methods fail closed;
- no market/trend field enters intrinsic valuation.

### Increment 8 - Long-only levels engine

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

No automatic execution or optimizer feedback is authorized by this plan.

## 17. Immediate next steps

Begin with Increment 0:

1. Create the provider protocol and entitlement schema.
2. Build the FMP capability probe without changing production configuration.
3. Select a representative probe universe:
   holdings, targets, large caps, small caps, biotech, recent IPOs, renamed tickers, and delisted
   examples.
4. Run the probe against the current key.
5. Obtain written retention/derived-use clarification before a paid annual commitment.
6. Decide `Premium`, `defer`, or `transient-alerts-only`.
7. Start Increment 1 in parallel because it has no FMP dependency.

## 18. Explicitly deferred

- Historical consensus backfill without proven vintages.
- Automated action based on option signals.
- Fully automated peer/customer/supplier graph.
- LLM-only material-event decisions.
- Automated order placement from levels.
- Optimizer feedback from monitor or levels.
- Systematic short entry levels.
- Claims that a prevented action is causal realized alpha.

These items require separate evidence or data contracts and are not prerequisites for the robust
Phase 1 monitor.
