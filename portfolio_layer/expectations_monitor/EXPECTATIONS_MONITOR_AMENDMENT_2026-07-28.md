# Expectations Monitor - Normative Architecture Amendment

> **Authoritative implementation sequence:** `portfolio_layer/MONITOR_LEVELS_IMPLEMENTATION_PLAN.md`
> governs provider selection, realistic observability, evidence ledgers, build increments, and
> definitions of done. This specification governs its domain formulas and contracts.

Status: FROZEN DESIGN AMENDMENT.
Effective: 2026-07-28.
Applies to: `EXPECTATIONS_MONITOR_SPEC.md`.

This amendment is normative and overrides conflicting universe, provider, phasing, and levels-engine
language in the v1 specification. Unchanged taxonomy, scoring, state-machine, and audit provisions
remain in force.

## 1. Universe

The monitor universe is:

```
all names in the latest sealed Stage 1 score contract
UNION actual open broker equity holdings
UNION names with positive weight in the current final target book
```

Priority affects refresh cadence, not holdings coverage:

1. `holding` - open broker position, including held-but-unscored names.
2. `target` - current positive target weight.
3. `investable` - current `investable_eligible=1`.
4. `scored_other` - remaining sealed Stage 1 names.

The complete union receives EOD market monitoring. A holding is never dropped because it becomes
unscored or ineligible. A held-but-unscored name receives `BaselinePoints=0`,
`baseline_available=0`, and remains eligible for review, risk, trim, and exit alerts. It can never
receive an active new-entry or add recommendation.

`monitor_universe` must add:

```
tier
is_holding
is_target
investable_eligible
score_available
universe_reasons_json
```

## 2. Provider independence

FMP, Alpha Vantage, Gemini, and future paid feeds are optional enrichment providers. No provider
credential, entitlement failure, quota exhaustion, timeout, or stale cache may disable:

- universe synchronization;
- SEC and Form 4 ingestion;
- market-signal calculation;
- existing-event decay;
- LES/state transitions;
- holdings alerts;
- EOD expectations artifacts.

Every provider implements a normalized interface:

```
probe_capabilities() -> endpoint capability and quota records
poll_global(cursor, since_utc) -> raw items and next cursor
poll_tickers(tickers, cursor, since_utc) -> raw items and next cursor
fetch_estimate_snapshots(tickers) -> append-only PIT observations
```

Downstream classification consumes normalized records only and never imports provider-specific
payload shapes.

## 3. FMP integration

FMP is an enrichment source, not the source of truth. SEC filings, company announcements, and
existing sector guidance remain authoritative when sources conflict.

Required behavior:

- Probe each endpoint and persist access, batch support, history support, vintage safety, observed
  quota, remaining quota, probe time, response hash, and detail.
- Prefer global/bulk, date-window, and paginated endpoints over one request per ticker.
- For ticker endpoints, allocate quota in this order: holdings, targets, active alerts, investable,
  scored-other.
- De-duplicate the ordered ticker list before requesting.
- Persist cursors, endpoint TTLs, ETags where available, immutable response envelopes, and hashes.
- Reserve configurable quota for retries and newly material holdings.
- Honor `Retry-After` and use bounded exponential backoff for 429/5xx responses.
- Open a circuit breaker on authentication, entitlement, malformed-schema, or exhausted-quota
  failures. Disable only the affected endpoint and emit a visible coverage WARN.
- Never write API keys to logs, SQLite, raw caches, artifacts, or manifests.
- A stale cached response may support provenance and continued decay of an existing event. It cannot
  create a newly dated event or estimate snapshot.
- If provider publication time is absent, set `available_at_utc=fetched_at_utc` and apply a
  confidence haircut.
- Data returned today for a historical fiscal period is not a historical vintage.
- Price targets are diagnostic context and never a fair-value or consensus anchor.
- Cross-provider duplicates are clustered before scoring. The highest-authority source supplies the
  event facts; secondary copies add no novelty or impact points.
- A material FMP news item should be linked to the primary filing or issuer release when one exists.
  Uncorroborated secondary reporting keeps the lower external-intel credibility and cannot, by
  itself, produce a confirmed thesis break.

FMP may contribute:

- secondary news and press-release discovery;
- analyst upgrades/downgrades and estimate-revision events;
- prospectively captured estimate snapshots;
- diagnostic price-target changes.

Only append-only estimates with fiscal period, value, analyst count, currency, provider publication
time, fetch time, available time, source ID, and payload hash may later enter a consensus valuation
anchor.

## 4. Capability and estimate tables

Add:

```sql
CREATE TABLE provider_capabilities (
  provider TEXT NOT NULL, endpoint_id TEXT NOT NULL,
  accessible INTEGER NOT NULL, supports_batch INTEGER NOT NULL DEFAULT 0,
  supports_history INTEGER NOT NULL DEFAULT 0,
  history_is_vintage_safe INTEGER NOT NULL DEFAULT 0,
  observed_limit INTEGER, remaining_quota INTEGER,
  probed_at_utc TEXT NOT NULL, response_sha256 TEXT, detail TEXT,
  PRIMARY KEY (provider, endpoint_id));

CREATE TABLE analyst_estimate_snapshots (
  provider TEXT NOT NULL, ticker TEXT NOT NULL,
  fiscal_period_end TEXT NOT NULL, estimate_type TEXT NOT NULL,
  estimate_value REAL, currency TEXT, analyst_count INTEGER,
  provider_published_at_utc TEXT, fetched_at_utc TEXT NOT NULL,
  available_at_utc TEXT NOT NULL, source_uid TEXT,
  payload_sha256 TEXT NOT NULL,
  PRIMARY KEY (
    provider, ticker, fiscal_period_end, estimate_type, fetched_at_utc
  ));
```

## 5. Efficient operating cadence

Slow path:

- synchronize the universe after each sealed portfolio run;
- fetch complete-union, split-consistent OHLCV at EOD;
- snapshot analyst estimates twice daily for holdings/targets and once daily for remaining
  quota-ranked names where endpoint access permits;
- rebuild and seal all expectations states at EOD.

Fast path:

- poll EDGAR current filings every 15 minutes during RTH;
- poll global FMP/IR news hourly where available;
- immediately recompute a name after a material filing, guidance event, analyst action, or confirmed
  peer read-through;
- request on-demand IB market checks for holdings, targets, earnings-window names, and names that
  trip abnormal-move triggers.

An FMP outage lowers `external_provider_coverage`; it does not stop the slow or fast core paths.

## 6. Falling-knife defense and opportunity discovery

The monitor is an early-warning system, not a guarantee that every decline will be detected before
the price falls.

Falling-knife defense is asymmetric:

- credible negative guidance, accounting, financing, regulatory, customer, or filing evidence
  suspends additions immediately;
- abnormal price/volume weakness alone creates review/watch evidence but cannot declare the thesis
  broken;
- negative event plus market confirmation escalates urgency;
- held names always retain risk, review, trim, and exit coverage.

A cheaper price alone is never an opportunity. An add/open alert requires:

- current investability;
- an active valuation-supported band from the sealed levels artifact;
- acceptable liquidity and data quality;
- green/stable expectations state;
- no blocking escalation or earnings/catalyst window;
- stabilization or positive-confirmation evidence.

This creates two ranked outputs:

- `risk_review_queue`: holdings/targets ordered by thesis-break evidence, event credibility, market
  confirmation, and portfolio exposure;
- `opportunity_queue`: investable names ordered by valuation discount, expectations improvement,
  stabilization evidence, liquidity, and target-book relevance.

Before the levels engine exists, the monitor still produces risk alerts and an
`opportunity_observation_queue`, but stamps actionable opportunity status
`deferred_levels_unavailable`. It may not invent a valuation discount or active entry signal.

## 7. Levels-engine contract

The monitor exports sealed `expectations_state.csv` for the same run. The levels engine reads that
artifact, never the live monitor database.

- `watch`, `deteriorating`, or `broken` makes entry/add bands
  `inactive_thesis_suspended`.
- Credible structured company guidance may update valuation inputs through a sector-owned,
  source-linked PIT contract.
- The Phase 1 residual expectations price multiplier is zero.
- The monitor never feeds back into Stage 1, target weights, the optimizer, or the final book.

The detailed valuation and execution design is frozen in:

`portfolio_layer/levels/LEVELS_ENGINE_SPEC.md`

## 8. Revised Phase 1

Build Phase 1 without a mandatory FMP dependency:

1. shared monitor common code and database schema;
2. three-source universe synchronization;
3. SEC and Form 4 adapters;
4. FMP capability probe and optional adapter in parallel;
5. rules-first classification;
6. complete-union EOD market signals;
7. LES/state transitions, risk queue, and opportunity queue;
8. validation, sealing, and orchestration as a soft monitor group.

FMP endpoint availability changes capabilities and coverage, not whether Phase 1 can run.
