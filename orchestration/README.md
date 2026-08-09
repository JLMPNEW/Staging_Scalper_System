# Master orchestration

`run_all.py` is the only scheduled owner of the cross-sector and portfolio DAG.
It serializes repositories that share databases, limits concurrent network lanes,
and runs `portfolio_layer` only after every required sector passes its health gate.

## Nightly operation

The scheduled entry is `run_nightly.py`. It:

1. validates the independent FMP/Alpha provider store;
2. scans the configured IB statement directory and reconciles any newly arrived
   statement into an existing dated portfolio run;
3. runs `run_all.py --catch-up` for the latest completed market session;
4. writes a durable console log and `nightly_manifest.json` under
   `orchestration/nightly_runs/<timestamp>/`;
5. returns non-zero unless reconciliation and the new master manifest both seal
   `PASS`.

Install or inspect the Windows task:

```powershell
powershell -File orchestration/manage_windows_task.ps1 -Action Preview
powershell -File orchestration/manage_windows_task.ps1 -Action Install
```

The default task runs Monday-Friday at 23:00 local machine time. It is
start-when-available, prevents overlapping instances, retries twice, and has an
18-hour ceiling. API keys and other credentials are inherited from the task
user's environment; they are never placed in task arguments or manifests.

## Data-source boundaries

- FMP and Alpha estimate/revision network calls belong exclusively to
  `portfolio_layer/provider_ingestion`.
- Master catch-up marks older portfolio dates `--historical-catchup`, preventing
  current-only event endpoints from being assigned to historical runs.
- IB statements remain date-specific. If no statement ends on the run date, the
  portfolio runner uses only a bounded, hash-verified prior ledger, defers
  ledger/exits/payout, and seals the portfolio run as `PASS_WITH_DEFERRED`.
  Monitoring, optimization, final weights, and final reporting continue and
  label the carried ledger date and age. On a later night,
  `reconcile_late_ib_statements.py` detects the newly downloaded statement and
  reruns only `ledger -> exits -> payout -> final -> final_report` for that
  existing date. It never rebuilds scores, prices, risk, optimization, macro, or
  monitoring. Its metadata is written to
  `late_statement_orchestration_meta.json`, preserving the original full-run
  `orchestration_meta.json`.
- A statement date without an existing target book is recorded but not treated
  as a reconciliation candidate. An already accepted complete broker chain is
  idempotently skipped. A malformed file matching `statement_glob` or a failed
  downstream rebuild stops the nightly run before the current-date master.
- The portfolio risk group attempts IB historical liquidity after building the
  exact-date risk universe. Requests are sequential (never a simultaneous
  100-line market-data batch). On a connection failure, the configured policy
  may replay only the newest portfolio-database sample partition within the
  liquidity staleness bound. The source partition and connection error are
  sealed; any stale row, quote defect, incomplete universe, or excess fallback
  still fails the hard 05d/08 gates.

Weekend or holiday publisher folders are retained for audit but are excluded
from the catch-up market calendar.

The Windows task may run the heterogeneous sector master with the base Conda
interpreter. The portfolio runner independently normalizes itself, before lock
acquisition or artifact writes, to `orchestration.python_executable` from
`portfolio_layer/config.yaml`. A missing configured interpreter fails before
the run starts.

At startup, the nightly entry and Stage 12 runner fill only a fixed allowlist of
missing process variables from the local Windows user environment. Existing
process values always win; values are never printed or written to manifests.
This prevents a long-lived Task Scheduler/IDE parent from missing newly added
API-key or database-path variables.
