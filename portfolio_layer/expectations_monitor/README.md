# Expectations Monitor

Implementation status: Increment 0, the Increment 1 data foundation, read-only pending-order
capture, and the sealed daily monitor orchestrator are complete. FMP Premium and Alpha Vantage
Premium were tested on the same sealed 50-symbol universe. The sealed
2026-07-24 monitor universe contains 1,147 names across Tiers 0/1/2. Append-only normalized
provider snapshots, exact dependency lineage, and provider/date purge controls are operational.
The first retained 50-symbol cycle stored 2,992 Alpha rows and 498 FMP rows with no raw payloads,
duplicate IDs, credential leakage, or provider/normalization errors. State scoring, transitions,
and valuation levels are not implemented. The output contract is advisory states only; broker
execution is permanently prohibited. Static broker mode is the default and reads the local Activity
Statement chain without connecting to IB.

FMP and Alpha Vantage are never averaged. `41_validate_provider_estimate_semantics.py` validates
each provider independently. `42_reconcile_provider_estimates.py` compares only exact canonical
period matches and records the lower comparable estimate only as a diagnostic downside candidate,
with its provider and both source snapshot IDs. It does not assign a central estimate. Single-source observations remain explicitly
single-source. See `PROVIDER_RECONCILIATION_POLICY.md`.

The retained 2026-07-31-paid-50-pilot-v2 cycle passed semantic validation with 778 active
source rows and zero active failures. Its reconciliation produced 650 canonical active records:
128 exact cross-provider pairs, six disagreement flags, and 522 explicit single-source records.
All 128 lower-estimate candidates remain diagnostic-only because currency is unverified; ten also
fail the two-analyst-per-provider floor. No central estimate or downstream downside guard is active.
The 24 semantic failures in the retained provider history are old Alpha quarterly reference rows;
they remain available for outcome analysis but are excluded from the current forecast window.
Provider-by-sector accuracy is still pending prospective realized outcomes. Historical rows fetched
after their fiscal outcomes cannot be used as point-in-time forecast evidence.

The reconciliation also preserves symmetric provider low/high ranges. It reports provider-range overlap,
the outer envelope, and no-overlap disagreement without averaging. These native ranges remain diagnostic;
future 10%/90% conformal residual boundaries require prospective outcomes before they can support balanced
buy/add/hold/trim/sell monitoring states.

In the sealed pilot, 122 exact pairs have overlapping native ranges and six have no overlap. All remain
diagnostic-only because reporting currency has not been verified.

## Credentials

Provider credentials are read only from:

- `FMP_API_KEY`
- `TIINGO_API_KEY`
- `ALPHAVANTAGE_API_KEY`
- `ALPHAVANTAGE_PREMIUM_API_KEY`

The free Alpha Vantage key remains reserved for existing earnings-calendar and other established
consumers. The expectations-monitor provider probe uses only `ALPHAVANTAGE_PREMIUM_API_KEY`.

Do not place credential values in YAML, source code, command-line arguments, manifests, or reports.
After setting Windows user environment variables, open a new terminal so child processes inherit
them.

## Capability Probe

The committed entitlement file records provider plans, request limits, retention status, and access
assumptions without credentials:

```powershell
python portfolio_layer/expectations_monitor/00_probe_provider_capabilities.py --selftest
python portfolio_layer/expectations_monitor/00_probe_provider_capabilities.py --as-of 2026-07-30
```

The probe:

- sends credentials through provider-required request authentication without logging rendered URLs
  or credentials; Alpha Vantage requires an API query parameter, while FMP and Tiingo use headers;
- uses an allowlist of provider hosts and refuses redirects;
- does not retain raw provider payloads;
- records only HTTP classification, row counts, and returned field names;
- does not retry deterministic authorization, plan, or schema failures;
- enforces the configured free-tier symbol cap.

Artifacts are written under:

`portfolio_layer/output/provider_capabilities/<as-of>/`

## Initial 2026-07-30 Result

Acceptance: `PASS_WITH_GAPS`.

- FMP free access returned analyst estimates, earnings reports, grade actions, historical grade
  counts, and price-target consensus for AAPL, NVDA, and MSFT.
- The same FMP capabilities were plan-restricted for SYK, REGN, and RTX; historical ratings were
  plan-restricted for every tested symbol.
- Tiingo EOD prices were available for all six symbols.
- Tiingo fundamentals were available for AAPL and MSFT only in this probe.
- Tiingo news was plan-restricted for all six symbols.

These observations describe only the tested free accounts and symbols. They do not establish
production coverage, point-in-time history, retention rights, or data quality.

## Adjusted OHLCV

`51_build_monitor_ohlcv.py` builds the shared current monitor/levels adjusted-OHLCV artifact from
the sealed monitor universe. The policy is fixed to Yahoo primary, read-only IBKR confirmation, and
Tiingo recovery. Source observations are retained separately and are never averaged. Tiingo is
fallback-only in the daily configuration; `--tiingo-crosscheck` is reserved for controlled canaries.
`52_validate_monitor_ohlcv.py` independently recomputes the selected source row, validates final
daily-bar timing, coverage, disagreement bounds, input/code hashes, and universe lineage.

The daily orchestrator runs the producer before the independent validator. An OHLCV producer
self-check alone cannot satisfy readiness. IB connectivity is optional when Yahoo coverage is
complete, but every IB attempt is read-only and bounded below 100 instruments. These scripts do
not change Stage 2 covariance inputs, Stage 11 execution evidence, portfolio weights, or broker
orders.

Controlled commands:

```powershell
python expectations_monitor/51_build_monitor_ohlcv.py --as-of YYYY-MM-DD --universe-as-of YYYY-MM-DD
python expectations_monitor/52_validate_monitor_ohlcv.py --as-of YYYY-MM-DD --universe-as-of YYYY-MM-DD
```

## FMP Premium and Alpha Vantage Status

The sealed FMP Premium 50-symbol probe is under:

`portfolio_layer/output/provider_capabilities/2026-07-31-fmp-premium-50/`

Acceptance is `PASS`: 297 of 300 endpoint-symbol checks were available.

The paid Alpha comparison is under:

`portfolio_layer/output/provider_capabilities/2026-07-31-alpha-premium-50/`

The sealed provider decision is under:

`portfolio_layer/output/provider_capabilities/2026-07-31-paid-estimate-comparison/`

Alpha returned non-empty estimates for 40/50 symbols (80.0%) versus FMP's 50/50 (100.0%). There
were no unresolved Alpha provider errors or rate-limit failures. Alpha supplied useful revision
fields, but it failed the pre-registered 90% coverage floor and its retention rights remain
unconfirmed. The resulting provider role is `diagnostics_only`.

Under `../MONITOR_LEVELS_RETENTION_AMENDMENT_2026-07-31.md`, normalized FMP and Alpha estimate
snapshots may be retained provisionally for private local use. Raw payload retention and external
disclosure remain prohibited. Every row must carry retrieval, entitlement, and hash provenance, and
every dependent artifact must be invalidated if its provider snapshots are later purged.

## Promotion Boundary

No provider data may feed monitor states, valuation levels, Stage 1 scores, target books, or broker
actions until:

1. the required coverage passes on the full staged universe;
2. storage and derived-data rights are confirmed in writing;
3. immutable snapshot schemas and PIT timestamps are implemented;
4. provider outage and staleness gates pass;
5. shadow outcomes are recorded and validated.

## Local Foundation Commands

```powershell
# Build the sealed monitoring universe from an accepted portfolio run.
python portfolio_layer/expectations_monitor/39_sync_monitor_universe.py `
  --as-of 2026-07-24

# Append one normalized provider cycle. Raw payloads are discarded.
$asOf = (Get-Date).ToString("yyyy-MM-dd")
python portfolio_layer/expectations_monitor/40_snapshot_provider_estimates.py `
  --provider both `
  --symbols-file portfolio_layer/output/provider_capabilities/2026-07-31-fmp-premium-50/probe_universe.csv `
  --as-of $asOf `
  --retrieval-cycle "$asOf-eod"

# Preview a provider/date purge. Add --execute and --reason only after reviewing the report.
python portfolio_layer/expectations_monitor/02_purge_provider_snapshots.py `
  --provider alpha_vantage --from-date 2026-07-31 --to-date 2026-07-31

# Validate and reconcile a completed two-provider cycle without averaging.
python portfolio_layer/expectations_monitor/41_validate_provider_estimate_semantics.py `
  --retrieval-cycle 2026-07-31-paid-50-pilot-v2 --as-of 2026-07-31
python portfolio_layer/expectations_monitor/42_reconcile_provider_estimates.py `
  --retrieval-cycle 2026-07-31-paid-50-pilot-v2 --as-of 2026-07-31 `
  --universe-as-of 2026-07-24
```

The snapshot command sends only provider-required endpoint, ticker, and authentication fields. It
does not send implementation details, policies, portfolio artifacts, scores, weights, or holdings.

## Tiered Capture, Basis, and Outcomes

The provider evidence foundation is implemented in scripts 43-48:

- `43_run_provider_capture_schedule.py` reads one sealed monitor universe, processes event names
  first, then Tier 0, Tier 1, and scheduled Tier 2 names in deterministic batches no larger than
  50. Alpha and FMP share an exact batch cycle. A failed batch is retried under a new immutable
  attempt ID; a prior PASS is never overwritten.
- `44_sync_estimate_basis_contract.py` captures issuer reporting currency from FMP statements but
  stores it separately from provider estimate semantics. Reporting currency alone cannot authorize
  comparison. EPS and revenue remain fail-closed until provider currency, units, split basis, and
  GAAP-versus-adjusted definitions are verified.
- `45_snapshot_provider_actual_outcomes.py` stores normalized earnings actuals in a globally
  hash-chained v2 ledger. FMP earnings `date` is correctly treated as `report_date`, never silently
  as a fiscal-period end. Exact fiscal-period matches may come only from the local PIT earnings
  calendar; unresolved rows remain ineligible. Release timestamps that lack a timezone are also
  untrusted.
- `46_sync_fiscal_period_resolutions.py` captures exact Alpha `reportedDate` to
  `fiscalDateEnding` mappings and provider-matched reported EPS. The sealed Alpha bulk earnings
  calendar is an authoritative prospective fallback when the per-symbol history endpoint is
  temporarily unavailable. It never infers a quarter from row order or nearest dates.
- `47_link_provider_forecasts_to_outcomes.py` scores only provider-matched quarterly forecasts and
  outcomes. A forecast must have been available on a U.S.-Eastern date strictly before the report
  date. Same-day forecasts are excluded regardless of the reported before/after-market label.
- `48_run_provider_earnings_event_cycle.py` selects only sealed Tier 0/1 names whose exact Alpha
  calendar event is within the configured two-day lookback/lookahead window. It batches exact-period
  and provider-matched actual capture, resumes hash-valid children after a failed parent, retries
  only failed Alpha symbols, and runs the local linker only after every child is complete.
- FMP estimate capture requests annual and quarterly horizons in one atomic run. Explicit `EMPTY`
  responses are retained as coverage gaps rather than transport failures; request errors and
  normalization failures still fail closed. Reconciliation never invents a missing horizon.

No raw provider payload is retained. Provider requests contain only endpoint, ticker, and
authentication. No implementation, policy, score, weight, holding, or order data is sent.

```powershell
# Preview the real sealed universe without calling a provider.
python portfolio_layer/expectations_monitor/43_run_provider_capture_schedule.py `
  --as-of 2026-07-31 --universe-as-of 2026-07-24 --dry-run

# Capture a capped reporting-currency batch.
python portfolio_layer/expectations_monitor/44_sync_estimate_basis_contract.py `
  --as-of 2026-07-31 --symbols AAPL NVDA SYK

# Append actuals; unresolved period/basis/timing rows are retained but cannot score providers.
python portfolio_layer/expectations_monitor/45_snapshot_provider_actual_outcomes.py `
  --as-of 2026-07-31 --symbols AAPL NVDA SYK

# Capture exact fiscal periods plus Alpha reported EPS.
python portfolio_layer/expectations_monitor/46_sync_fiscal_period_resolutions.py `
  --as-of 2026-07-31 --symbols AAPL NVDA SYK

# Build local-only, provider-matched forecast/outcome evidence.
python portfolio_layer/expectations_monitor/47_link_provider_forecasts_to_outcomes.py `
  --as-of 2026-07-31

# Run the sealed earnings-event sequence after the daily estimate capture.
python portfolio_layer/expectations_monitor/48_run_provider_earnings_event_cycle.py `
  --as-of 2026-07-31 --universe-as-of 2026-07-24
```

The accepted 2026-07-31 Tier 0 run processed 62 names in two batches. FMP completed all 124
annual/quarterly endpoint-symbol calls; Alpha returned estimates for 24 names and explicit empty
coverage for 38. The exact-period canary retained 354 mappings and 353 Alpha EPS actuals across
AAPL, NVDA, and SYK. The provider-matched linker produced 377 historical diagnostics but zero
eligible links because every retained forecast was captured after those historical report dates.
That is the required PIT result; prospective captures after 2026-07-31 can mature into evidence.

The first Tier 1 run on 2026-07-31 processed 292 investable names in six batches. Across 876
endpoint-symbol requests, 715 were available, 161 were explicit empty coverage, and zero were hard
errors. All six independent semantic validators and reconciliations passed; action eligibility
remained zero because provider definition/currency semantics are still unverified. The first sealed
event cycle selected 55 Tier 0/1 names, resolved them in two batches, resumed 49 successful Alpha
rows plus both FMP batches after a transient provider-message failure, retried only six Alpha names,
and finished PASS. Its 5,250 historical diagnostics produced zero eligible forecast/outcome links:
no retained forecast predated the old report and no metric basis was eligible at its cutoff. This is
the expected prospective-start result.

One pre-fix Tier 0 session wrote database rows but failed to write child reports because its Windows
paths exceeded the platform limit. It is non-destructively marked `INVALIDATED` and superseded by
the accepted short-path session. Scheduler acceptance now requires child return code zero, database
PASS, a PASS child manifest, and exact output hashes.

## Daily Orchestration and Pending Orders

Scripts 49 and 50 complete the operational shell around the provider foundation:

- `49_snapshot_ib_pending_orders.py` has explicit `static` and `live` modes. Static mode selects the
  latest non-stale `IB_reports/U*.csv` Activity Statement and makes no IB connection. Because an
  Activity Statement contains positions and completed trades but no current-open-order contract,
  it returns `PASS_WITH_DEFERRED` rather than falsely claiming there are no pending orders. Live mode
  connects to the configured real-account TWS port with `readonly=True`, captures open orders, hashes
  the account ID, and refuses historical backfill. `--replay-csv` supports deterministic testing.
- `50_run_expectations_monitor_daily.py` validates the sealed universe and source hashes, runs or
  resumes deterministic provider batches, independently validates and reconciles every completed
  cycle, runs or reuses the earnings-event cycle, and seals all child manifests under one parent.
  Parent idempotency verifies every output and child-manifest hash.
- Provider semantics, pending orders, and authoritative SEC/issuer events are explicit readiness
  dependencies. A deferred dependency produces `PASS_WITH_DEFERRED`, never action authorization.
- Provider requests remain limited to endpoint, ticker, and authentication fields. The orchestrator
  does not send implementation, policy, score, weight, holding, or order data to a provider.
- The only allowed future outputs are `buy_candidate`, `add_candidate`, `hold`, `watch`,
  `deteriorating`, `suspend_adds`, and human-only `exit_review`. A runtime source scan hard-fails if
  executable IB order API methods enter the monitor package.

```powershell
# Preview the daily chain without provider or broker calls.
python portfolio_layer/expectations_monitor/50_run_expectations_monitor_daily.py `
  --as-of 2026-07-31 --tiers tier1 --skip-pending-orders --dry-run

# Run against sealed captures and the default static Activity Statement source.
python portfolio_layer/expectations_monitor/50_run_expectations_monitor_daily.py `
  --as-of 2026-07-31 --tiers tier1

# Validate the read-only IB collector without connecting to IB.
python portfolio_layer/expectations_monitor/49_snapshot_ib_pending_orders.py --selftest
```

The accepted 2026-07-31 parent run processed six Tier 1 cycles, passed all twelve semantic and
reconciliation children, reused the sealed earnings-event cycle, and returned
`PASS_WITH_DEFERRED`. It sealed 14 child manifests with zero independently recomputed hash
failures in the pre-static run; the static-source run adds the sealed broker-source child. State
publication remains disabled while readiness dependencies are incomplete. The current v2 field is
`state_publication_authorized=false`; `broker_execution_prohibited=true` is permanent.
