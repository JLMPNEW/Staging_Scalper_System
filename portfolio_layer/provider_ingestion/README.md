# Independent Provider Ingestion

This package is the only network owner for current FMP and Alpha Vantage estimate
snapshots. The portfolio pipeline consumes the store but does not call current
provider endpoints while rebuilding historical dates.

## Contracts

- Database: `portfolio_layer/db/provider_observations.sqlite`
- Raw payload retention: prohibited; payloads exist only in request memory.
- Secrets: environment variables only (`FMP_API_KEY`,
  `ALPHAVANTAGE_PREMIUM_API_KEY`).
- Provider requests contain only authentication, endpoint parameters, and ticker.
  Holdings, weights, actions, scores, and internal policy are never sent.
- Every request records start and response-received timestamps. Availability begins
  at response receipt, never request start or a caller-supplied portfolio date.
- Every successful check creates an observation. A full estimate version is created
  only when normalized content differs from the prior version.
- Changes are interval-censored between the prior and current observations. The
  system never claims to know the provider's unreported intraday revision time.
- Current-snapshot endpoints reject `--portfolio-as-of` unless it equals the actual
  local capture date. Missed tasks run current-only; they are never replayed as past
  observations.
- `XNYS` sessions, holidays, early closes, weekends, and DST determine actionability.
  Same-session use requires both the response and the completed capture cycle before
  `decision_cutoff_local`. Otherwise the observation becomes effective next session.
- Capture runs form an append-order SHA-256 chain. Artifacts record exact observation
  dependencies. New runs also seal the canonical request-report schema and row order;
  a missing or corrupted request CSV and a missing manifest can be reconstructed
  byte-for-byte from accepted database evidence without repeating provider calls.
- A new `scheduled-*` capture is accepted only while a matching date/phase dispatch
  row is `STARTED`. Manually launched captures cannot impersonate scheduled continuity.
- Scheduled capture reads only its provider-owned universe registry. Portfolio and
  monitor artifacts can refresh that registry through a sealed handoff but cannot
  prevent an otherwise due capture from running.

## Commands

```powershell
python portfolio_layer/provider_ingestion/capture.py --phase sunday_baseline
python portfolio_layer/provider_ingestion/capture.py --phase premarket
python portfolio_layer/provider_ingestion/capture.py --phase priority_refresh
python portfolio_layer/provider_ingestion/capture.py --phase postclose
python portfolio_layer/provider_ingestion/run_due.py
python portfolio_layer/provider_ingestion/validate.py
python portfolio_layer/provider_ingestion/publish.py --as-of 2026-07-31
python portfolio_layer/provider_ingestion/recover.py --through 2026-08-05
python portfolio_layer/provider_ingestion/recover.py --through 2026-08-05 --execute
```

`run_due.py --now-utc ...` is also dry-run-only: a simulated scheduler clock can
never launch or label a real provider capture.

Use `--dry-run` on `capture.py` to verify the selected universe without network
requests. `--symbols` and `--limit` provide controlled probes. Requests are processed
in configured groups of at most 50.

## Delayed Portfolio Runs

Provider capture and portfolio computation have separate clocks. If the portfolio
pipeline has not run for several sessions, `recover.py` first verifies the provider
store, reports every elapsed scheduled slot with no accepted capture, and identifies
missing accepted portfolio sessions on the XNYS calendar. Without `--execute` it
only writes a sealed plan. With `--execute` it invokes the normal single-date
portfolio orchestrator for missing sessions oldest-first and stops on the first
failure. It never calls a current provider endpoint for a historical date.
Past-date portfolio commands are stamped `--historical-catchup`; this also suppresses
the monitor's still-separate current FMP actual-outcome event cycle. Script 50 independently
rejects a still-open/future XNYS session and requires the event cycle to be skipped whenever
the requested date precedes the latest completed XNYS session. The normal 23:00 CT nightly
run remains live-current after midnight ET because it targets that latest completed session.

The recovery artifact records provider outages as explicit coverage gaps. Gaps are
not backdated or silently repaired. The normal point-in-time monitor query can then
reconstruct each missing portfolio date from observations genuinely available and
effective on that date. If provider ingestion itself was offline, the missing
interval remains disclosed.

Provider capture owns an append-only universe registry in
provider_observations.sqlite. A hash-valid monitor-universe artifact is an optional
handoff that can append a newer registry version; it is never a runtime dependency.
If portfolio or monitor processing fails, scheduled capture continues from the last
accepted registry. The registry source date remains recorded for lineage, but it does
not make current provider observations stale and cannot downgrade an otherwise clean
capture. A newly accepted handoff updates membership on the next scheduled cycle.

Recommended Windows Task Scheduler jobs (America/New_York): Sunday 18:00 baseline;
business-day 07:30 premarket; 08:45 priority refresh; 18:00 post-close. The robust
deployment is one Task Scheduler job invoking `run_due.py` at the YAML-configured `scheduler_poll_minutes` cadence (currently 10 minutes). It reads
the ET schedule and never replays a missed historical provider slot. Intraday is
disabled by default. XNYS holidays are not scheduled. Each invocation exits, and the
capture lock prevents overlapping network cycles.

Recovery windows are phase-specific: Sunday baseline and post-close remain actionable
for four hours, premarket for 105 minutes, and priority refresh for 60 minutes. The
dispatcher processes the oldest overdue incomplete phase first, allows at most two
attempts, and terminates a child that exceeds 100 minutes. A machine or parent-process
crash leaves a durable `STARTED` dispatch row; after 110 minutes it is sealed as
`INTERRUPTED` before the next retry. Manual captures are audit evidence but never
satisfy scheduled continuity.

The Sunday baseline captures Tier 0 and Tier 1 so Monday readiness does not depend on
a capture that occurs only after Monday's close. Premarket and priority refreshes are
Tier 0; postclose captures Tier 0/1 daily and adds Tier 2 on the final XNYS session of
the week, including holiday-shortened weeks.

Preview or install the single recurring Windows task with:

```powershell
powershell -File portfolio_layer/provider_ingestion/manage_windows_task.ps1
powershell -File portfolio_layer/provider_ingestion/manage_windows_task.ps1 -Action Install
```

Installation is an explicit operator action; repository tests do not modify Windows
Task Scheduler. The installed task wakes the computer, starts when available, runs on
battery, derives its restart count from `max_scheduled_attempts` (currently one retry
after the initial run), ignores overlapping starts, and derives its execution limit from
`capture_timeout_minutes` plus a 20-minute sealing margin. It uses the interactive user principal;
therefore captures while the user is fully logged out require a separately approved
credentialed service account or scheduled-task principal.

Every scheduler attempt has a hash-sealed child log and database record. Capture
acceptance is provider-wide (clean-request and available-request floors), not a
single successful row. Every entry point validates the complete policy version, schedule,
provider set, recovery contract, actionability semantics, timeout ordering, and thresholds. The durable summary log is serialized and rotated. Run
`validate.py --require-continuity` for a fail-closed operational check; the default
validator preserves historical gaps as `PASS_WITH_WARNINGS` so known misses cannot
be mistaken for reconstructed observations.

## Portfolio Integration

`expectations_monitor/50_run_expectations_monitor_daily.py` automatically skips its
legacy estimate capture when `provider_ingestion.network_owner` is
`independent_service`. `49a_build_provider_diagnostics.py` attaches the independent
store, enforces both availability and `effective_trading_date`, and writes lineage
back to the provider store. The monitor remains advisory and does not place orders.

The independent store currently owns estimate and revision snapshots. Existing
actual-outcome and fiscal-period resolution ledgers remain in the expectations
monitor database; their schema is not being silently reinterpreted. Moving those
historical-outcome capabilities is a separate migration and is not required for
current estimate-revision timing.

## Legacy Migration

`migrate_legacy.py` is dry-run by default. `--execute` preserves legacy observed
timestamps and does not invent provider vintages. Compact or dashed dates embedded in
legacy cycle names are compared with actual capture dates in the authoritative
`legacy_migration_annotations` table. The migration is idempotent.
