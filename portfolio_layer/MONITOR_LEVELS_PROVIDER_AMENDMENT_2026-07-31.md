# Monitor and Levels Provider Amendment

Issued: 2026-07-31

Amends: `MONITOR_LEVELS_IMPLEMENTATION_PLAN.md`

Status: FROZEN FOR IMPLEMENTATION

This amendment records the provider decisions made after the original 2026-07-28 design freeze.
It overrides conflicting provider status, purchase policy, coverage tiers, and immediate-next-step
language in Sections 2, 3, 16, and 17 of the main plan. It does not authorize provider data to
change scores, target books, orders, or broker positions.

## 1. Verified provider state

### FMP

- FMP Premium is active.
- The sealed 50-symbol capability probe at
  `output/provider_capabilities/2026-07-31-fmp-premium-50/` reports `PASS`.
- Of 300 endpoint-symbol checks, 297 were available.
- Verified shapes include analyst estimates, earnings, grade actions, historical grade counts,
  historical ratings, and price-target consensus.
- FMP estimate rows do not establish a complete historical daily-vintage archive. They may be
  retained prospectively only after storage, derived-use, and post-termination rights are recorded.

### Alpha Vantage

- `ALPHAVANTAGE_API_KEY` remains configured for existing free-key consumers.
- `ALPHAVANTAGE_PREMIUM_API_KEY` is separately configured for the expectations monitor.
- A live `EARNINGS_ESTIMATES` probe for IBM succeeded with 41 fiscal-period rows.
- Verified fields include EPS/revenue estimate ranges, analyst counts, prior EPS estimates at
  7/30/60/90 days, and recent revision-up/revision-down counts.
- The row `date` is a fiscal estimate period, not a daily retrieval vintage. Alpha Vantage therefore
  does not replace a prospectively timestamped snapshot store.
- The 75-request-per-minute monthly plan is active for a plan-equivalent comparison with FMP.
- The sealed paid-plan probe tested the same 50 symbols as FMP: Alpha returned non-empty estimates
  for 40/50 (80.0%) with no unresolved provider or quota errors; FMP returned 50/50 (100.0%).
- Alpha failed the pre-registered 90% coverage floor but added explicit 7/30/60/90-day estimate and
  revision-window fields. Its current role is `diagnostics_only`, pending written retention rights.

### Tiingo

- Tiingo free EOD pricing is available for the representative sample.
- Tiingo is a price-validation and recovery candidate. It is not an analyst-estimate source.
- Broad fundamentals and news remain entitlement-limited in the tested account.

### IBKR, Yahoo, SEC, and issuer sources

- IBKR remains authoritative for positions, execution quotes, bid/ask, liquidity, and borrow.
- Yahoo remains a broad EOD OHLCV fallback, subject to existing quality and retry gates.
- SEC filings and issuer releases remain the highest-authority sources for company facts and
  guidance.

## 2. Final provider roles

| Source | Approved role | Prohibited assumption |
|---|---|---|
| SEC/issuer | authoritative events, filings, guidance | cannot supply analyst consensus |
| FMP Premium | primary estimates, revisions, analyst actions, earnings, news discovery | current access does not prove historical daily vintages |
| Alpha Vantage | independent estimates/revisions comparison and optional fallback | do not treat fiscal dates as retrieval vintages |
| Tiingo | EOD price validation/recovery | not a consensus-estimate source |
| IBKR | holdings, execution, liquidity, borrow, priority-name confirmation | not a broad historical fundamentals store |
| Yahoo | broad EOD OHLCV fallback | not authoritative for estimates or corporate events |

The system remains provider-independent. FMP or Alpha Vantage failure must degrade enrichment
coverage explicitly while SEC, universe, market, and holdings monitoring continue.

## 3. Dynamic coverage tiers

Universe sizes are derived from sealed artifacts on every run; numeric counts below are operational
examples, not hard-coded limits.

### Tier 0 - Immediate risk

Includes:

- open broker holdings;
- pending orders;
- current target-book names;
- active exit or risk-review names.

Cadence:

- authoritative events during scheduled polling;
- estimates/revisions at least once after market close;
- priority refresh on material events.

### Tier 1 - Daily investable universe

Includes all currently investable names plus Tier 0. The recent expected scale is approximately
327 names.

Cadence:

- FMP estimates/revisions and relevant analyst actions daily after market close;
- Alpha Vantage only for the pilot or, after approval, as an independent daily comparison;
- EOD market-state calculation after the official session is complete.

### Tier 2 - Weekly research universe

Includes all scored names plus Tiers 0 and 1. The recent expected scale is approximately 1,173
names.

Cadence:

- weekly full-universe estimates/revisions refresh;
- daily event-driven refresh for names promoted into Tier 0 or Tier 1;
- never delay Tier 0/1 processing while Tier 2 is incomplete.

Membership changes are recorded with `effective_from`, `effective_to`, source artifact hashes, and
the run's PIT timestamp.

## 4. Alpha Vantage paid-plan decision

The lowest premium tier has been purchased for one month to run a plan-equivalent evaluation:

- 75 API requests per minute;
- no daily request limit;
- currently advertised at $49.99/month as of this amendment;
- plan price is documentation only and must be rechecked before purchase.

At a conservative 60 requests per minute:

- 327 Tier-1 names require approximately 6 minutes per endpoint;
- 1,173 Tier-2 names require approximately 20 minutes per endpoint;
- multiple endpoints multiply runtime linearly but remain suitable for after-close operation.

Higher Alpha Vantage plans are not justified unless measured runtime prevents the daily service-level
objective. Real-time market data is not a reason to upgrade because IBKR provides the execution
surface.

Subscription status does not authorize retained snapshots by itself. Raw and normalized vendor data
remain non-retained/shadow until storage, internal-derived-use, and post-termination rights are
confirmed in writing.

### Evaluation universe

The final evaluation used the same sealed 50-symbol universe as FMP, spanning:

- current holdings and targets;
- large and small capitalization;
- biotech and event-driven names;
- medical devices;
- semiconductors, hardware, and software;
- defense/machinery/transportation where represented;
- recent IPOs and renamed tickers;
- at least one name with sparse analyst coverage.

The premium probe was throttled to approximately 57 requests per minute, below the subscribed
75-request-per-minute limit. No rate-limit response occurred.

### Evaluation acceptance and result

Alpha Vantage earns an operational role only if all hard gates pass:

1. Key-safe deterministic ingestion and schema validation pass.
2. At least 90% of eligible pilot symbols return structurally valid estimate records.
3. Fiscal period, horizon, currency/basis assumptions, and analyst counts can be normalized without
   silent coercion.
4. Retrieval time is captured locally and no fiscal-period date is mislabeled as a vintage.
5. Stale or missing responses fail closed and cannot create a revision event.
6. Written storage, internal-derived-use, and post-termination rights are recorded.

At least one economic-value condition must also pass:

- Alpha Vantage fills a material FMP coverage gap;
- it supplies a revision field or timestamp unavailable from FMP;
- it provides a useful independent disagreement signal;
- it materially improves outage resilience at acceptable operating cost.

Measured result:

- coverage gate: `FAIL` (40/50 = 80.0%; floor 90.0%);
- unresolved provider errors: `PASS` (zero);
- added revision fields: `PASS`;
- retention-rights gate: `FAIL` (unconfirmed);
- provider role: `diagnostics_only`.

The existing free key remains unchanged for established consumers. The premium key is isolated as
`ALPHAVANTAGE_PREMIUM_API_KEY`. Paying for the monthly plan changes request capacity, not storage
rights. Alpha values must not be retained until written permission covers raw storage, normalized
daily snapshots, internal derived signals, and post-cancellation retention.

## 5. Snapshot and PIT contract

Every retained provider observation must include:

- provider and endpoint;
- normalized ticker and provider symbol;
- `fetched_at_utc`;
- provider publication/update time when supplied;
- `available_at_utc`;
- fiscal period and estimate horizon;
- estimate mean/high/low and analyst count;
- revision-window values and action counts when supplied;
- currency, split basis, and units when known;
- raw-envelope hash and normalized-row hash;
- entitlement/config/source-code hashes;
- coverage, freshness, and normalization status.

Append-only snapshots are first-write-wins for a provider, ticker, endpoint, and retrieval cycle.
Corrections create new rows and preserve superseded observations. Cross-provider values remain
separate; they are not averaged until a separately validated reconciliation model exists.

Historical FMP or Alpha Vantage fields may be used as `historical_research` only when their true
availability semantics are proven. Otherwise, Stage 11 uses only locally captured prospective
snapshots for estimate/revision features.

## 6. Request control and resilience

- Credentials are read from environment variables only.
- Use provider-specific token buckets; default Alpha Vantage ceiling is 60 requests/minute under
  the 75-rpm plan.
- Cache successful immutable envelopes and never refetch an already sealed retrieval cycle.
- Retry transient failures with bounded exponential backoff and jitter.
- Do not retry deterministic authorization, entitlement, invalid-symbol, or schema failures.
- Resume from per-provider cursors after interruption.
- Process Tier 0 before Tier 1 and Tier 1 before Tier 2.
- Record endpoint-level coverage, latency, quota use, failures, and stale ages.
- A provider outage produces an explicit degraded-enrichment state, never fabricated neutral data.

## 7. Updated implementation sequence

### Step 1 - Complete entitlement records

- Obtain and record written FMP retention, derived-use, and post-termination terms.
- Obtain the corresponding Alpha Vantage terms before any retained pilot payload is promoted beyond
  transient capability testing.
- Keep provider data shadow-only until these records pass.

### Step 2 - Extend Increment 0 for Alpha Vantage

- Add Alpha Vantage to `provider_entitlements.yaml`.
- Extend `00_probe_provider_capabilities.py` through the existing provider protocol.
- Add key-safe, redirect-denying, response-size-limited `EARNINGS_ESTIMATES` probing.
- Preserve the current no-raw-retention probe behavior until rights are confirmed.
- Add self-tests for quota, stale payload, malformed schema, and fiscal-date/vintage confusion.

### Step 3 - Run and adjudicate the paid comparison - COMPLETE

- Run the same 50-symbol universe used for the FMP Premium probe.
- Compare Alpha Vantage with FMP field-by-field and symbol-by-symbol.
- Publish a sealed coverage/disagreement report.
- Decide `approved_secondary_estimates_source`, `qualified_pending_rights`, or `diagnostics_only`.

Result: `diagnostics_only`. FMP remains primary; Alpha remains non-retained and cannot feed monitor
states, levels, Stage 11, or portfolio decisions.

### Step 4 - Start Increment 1 in parallel

- Build the provider-independent SQLite foundation, migrations, locking, and universe synchronizer.
- Materialize Tiers 0/1/2 from sealed portfolio artifacts.
- Validate that every holding, target, and Stage-1 name appears exactly once.
- This work does not depend on Alpha Vantage purchase or retention approval.

### Step 5 - Build FMP primary ingestion

- Implement immutable response envelopes, normalized estimates/revisions/actions, quotas, cursors,
  retry/backoff, circuit breakers, and coverage reports.
- Keep estimate snapshots shadow-only until retention rights pass.

### Step 6 - Add Alpha Vantage only after its decision gate

- If approved, use it as an independent comparison/fallback, not as an automatic replacement for
  FMP.
- Store provider-specific rows and disagreement diagnostics.
- Do not average providers or generate production actions from disagreement.
- Apply `provider_reconciliation_v2`: compare only exact ticker/metric/period/end-date matches;
  record the lower comparable estimate only as a gated shadow downside candidate with source lineage.
- Do not assign a central estimate until prospective provider-accuracy gates pass. Single-source
  rows and currency-unverified pairs cannot become downside-guard inputs.
- Label unmatched observations single-source and currency-unverified comparisons explicitly.
- Reconcile differences monthly. Provider-by-sector accuracy requires matured realized outcomes;
  agreement alone is not evidence that either provider is more accurate.

### Step 7 - Continue the original plan

Proceed with authoritative ingestion, classifier benchmarking, market/state engine, evidence
ledgers, shared OHLCV, and the long-only levels engine in the original Increment 2-9 order.

## 8. Immediate definition of done

The provider phase is complete when:

- FMP Premium capability coverage remains sealed and reproducible;
- FMP legal/retention status is explicit;
- Alpha Vantage pilot evidence and purchase decision are sealed;
- the provider-independent universe store is operational;
- no credential or raw prohibited payload appears in source, configuration, logs, or manifests;
- provider absence cannot block authoritative monitoring;
- no provider enrichment affects production portfolio decisions.
