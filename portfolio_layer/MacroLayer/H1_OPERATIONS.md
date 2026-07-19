# H1 Operations Runbook

This runbook governs operation of `macro_regime_h1_hybrid_v1`. The frozen statistical contract
remains `H1_CANDIDATE_SPEC.md`. Production remains `macro.regime_source: v1` until sealed H1
evidence is `PROMOTABLE`.

## Daily sequence

The MacroLayer serving pipeline runs these H1 steps in order:

1. Build the exact-date H1 hybrid and append new live probabilities.
2. Validate every H1 database row against its frozen components.
3. Build the H1 regime decision.
4. Append newly resolved outcomes and evaluate promotion evidence.
5. Run `validate_macro_h1_operations.py`.

The operational validator independently verifies both ledger chains, the baseline and evidence
chain heads, the complete promotion seal, evidence/decision date parity, A1.7, and the production
source. `NOT_PROMOTABLE` is healthy while production remains on V1.

Outputs:

- `output/h1_operations/latest_status.json`
- `output/h1_operations/history/*.json`
- `output/h1_chain_checkpoints/*.json`

For stronger disaster recovery, mirror `output/h1_chain_checkpoints/` and the two ledgers to a
separate machine or write-once storage after each successful daily run.

## First post-cutoff canary

On the first run carrying an H1 decision after `2026-07-19`:

1. Confirm one covered row for that date appears in `prospective_ledger.csv` with capture lag no
   greater than seven calendar days.
2. Confirm `outcomes_ledger.csv` changes only when a label becomes newly available.
3. Re-run the H1 chain for the same date. Both ledger append counts must be zero and chain heads
   must remain unchanged.
4. Confirm the operational report is `PASS`, candidate evidence remains `NOT_PROMOTABLE`, and
   production remains V1.

The canary can be made explicit with:

```powershell
python portfolio_layer/MacroLayer/validate_macro_h1_operations.py `
  --end-date YYYY-MM-DD --require-post-cutoff-capture
```

## Monitoring and reviews

- Daily: chain integrity, source drift, coverage, decision parity, A1.7, and production guard.
- Monthly: checkpoint restoration test and ledger row/count reconciliation.
- Quarterly: informational evidence review without changing the frozen gates.
- Expected first statistically eligible informational review: approximately late 2027.
- Earliest realistic final review with eight independent PI_LEAD outcomes: approximately late
  2028.

Any H1 source, frozen config, ledger schema, or promotion-rule change requires a new documented
campaign amendment or model identity before additional evidence is admitted.

## Promotion and rollback

Promotion is a configuration change only after the sealed rail reports `PROMOTABLE`:

```yaml
macro:
  regime_source: "h1"
```

Before that change, rerun the full portfolio pipeline, Stage 16d, the H1 operational validator,
and an independent promotion audit. Keep the previous V1 run artifacts. Rollback is the reverse
configuration change to `regime_source: "v1"`, followed by a full rebuild and validation.

## Separate research

Growth-lead redesign belongs to a separately versioned V3 campaign with a new pre-registration
and evaluation window. V3 research must not alter H1 builders, config blocks, ledgers, evidence,
or review thresholds.
