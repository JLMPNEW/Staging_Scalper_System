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

## Financial-lineage hybrid rollout

`orchestration/financial_lineage_policy.yaml` is the single source of truth for
financial-filing lineage enforcement. Local publishers, local validators, the
portfolio adapter, and the global orchestrator all call
`orchestration_contracts.financial_lineage`; sector registry/config files must
not redefine the same policy.

The production flow is fixed:

1. the sector performs its normal incremental filing and fact ingestion;
2. the shared bounded-recovery stage classifies the active universe, then reparses
   only `CANONICALIZATION_GAP` accessions and same-period companions;
3. the sector rebuilds financial features and the local publisher classifies every
   row as `INCORPORATED`, `CANONICALIZATION_GAP`,
   `NO_MATERIAL_FILING_IDENTIFIED`, or `PARSING_GAP`;
4. the local publisher writes the lineage fields and a policy-evaluated manifest;
5. the local validator applies the shared evaluator before sealing `PASS`;
6. the global orchestrator applies that same evaluator to the published CSV;
7. the portfolio adapter accepts only rows that satisfy the shared contract.

Defense and machinery are the pilot families. Their production and historical
policies are `strict_universe`; research is `candidate_only`. Other families
remain `disabled` until their local publisher emits the shared fields and their
shadow census has zero unexplained or unsafe observations. To onboard another
family, first implement its lineage producer and parity tests, then enable
`candidate_only` shadow validation, clear the review queue, and only then change
its production policy to `strict_universe`. This staged switch is the hybrid
rollout: the contract is global immediately, while enforcement is activated one
validated sector at a time.

A strict failure is diagnostic and fail-closed: the attempted artifacts and
classification evidence remain available, but neither the local last-success
manifest nor the global/portfolio production state may advance. Historical
lineage uses the same policy against each point-in-time date; adopting the
contract does not itself require a full historical rebuild.

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
