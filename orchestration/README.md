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
  portfolio runner skips ledger/exits and labels any carried ledger date in the
  final report. On a later night,
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
  100-line market-data batch). A connection failure is recorded as an advisory
  warning and the explicit spread fallback remains available; any partial panel
  must still pass the hard 05d/08 completeness and fallback-fraction gates.

Weekend or holiday publisher folders are retained for audit but are excluded
from the catch-up market calendar.
