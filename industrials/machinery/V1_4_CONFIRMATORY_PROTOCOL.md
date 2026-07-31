# Machinery v1.4 Confirmatory Protocol

Status: frozen on 2026-07-29

`machinery_oos_v1.4.0` is a one-spec confirmatory protocol. It freezes the
`equal_components` candidate selected by v1.3 and explicitly labels that
selection as non-independent evidence. It does not retroactively convert the
blocked v1.3 result into a production pass.

## Fixed Contract

- Candidate count: 1
- Optimizer: disabled
- Universe: operating-only machinery
- Benchmark: XLI
- Signal horizons: 21 and 63 trading days
- Return basis: D+1 adjusted-open excess return
- Top sleeve: 20%, with a minimum of 10 names
- Transaction costs: 20 basis points
- Sequential outcome peeking: prohibited

The canonical definition is
`model_protocols/machinery_oos_v1.4.0.json`. Its freeze manifest binds the
definition and the selected weights to the sealed v1.3 registry, static
summary, acceptance, walk-forward summary, and run manifest.

## Evidence Partitions

The original unopened machinery lockbox begins on 2026-01-01. The v1.4
protocol records 2026-01-01 through 2026-07-29 as the pre-freeze partition.
Signal capture begins on 2026-07-30.

The signal collector cannot read or write benchmark returns, security returns,
execution exits, forward dates, or outcome labels. A separate approved
protocol is required before any post-freeze outcome is evaluated. The first
final confirmatory review is not recommended before 2027-07-30 because four
non-overlapping 63-day windows require approximately one trading year.

## Commands

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\27_freeze_machinery_v14_protocol.py
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\28_audit_machinery_v13_defects.py
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\29_capture_machinery_v14_signals.py --asof YYYY-MM-DD
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe industrials\machinery\scripts\30_assess_machinery_defense_compatibility.py
```

Signal capture is intentionally a post-refresh companion command. The active
machinery refresh orchestrator is part of the Stage 12 production source seal;
modifying it solely to attach research capture would invalidate the active
production state.

## Current Results

- Protocol freeze validation: PASS
- v1.3 defect-only audit: PASS, zero defects
- Model or gate tuning performed by audit: no
- Lockbox outcomes accessed: no
- Defense artifacts modified: no
- Defense direct-replication readiness: blocked

Defense is not directly comparable today. Its panel has only a 63-day
adjusted-close label, no matching transaction-cost contract, no capex-cycle
pillar, and semantic rather than exact mappings for the cycle and backlog
pillars. The compatibility result is supporting research only and cannot
satisfy a machinery promotion gate.
