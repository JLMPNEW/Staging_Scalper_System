# Canonical Transportation prospective evidence operations

This package is the supported future-evidence path for Transportation only. The
Consumer Defensive branches were retired on 2026-08-26 and replaced by the
Consumer-owned v2 calibration framework documented in
`consumer_defensive/CONSUMER_DEFENSIVE_V2_IMPLEMENTATION_PATH.md`.

The Transportation path remains intentionally fail closed: canonical trust and
independent-review registries are unconfigured, so the evidence clock has not
started, no current artifact authorizes capital, no portfolio/configuration write
is enabled, and every optimizer cap remains zero.

## Supported commands

```powershell
python -m future_only_evidence.preflight_cli --help
python -m future_only_evidence.source_package_cli --help
python industrials/transportation/scripts/45h_capture_transportation_future_oos.py --help
python industrials/transportation/scripts/45i_evaluate_transportation_future_oos.py --help
python -m future_only_evidence.activation_cli --help
```

The preflight command is read-only apart from one create-only JSON output. The
source-package command creates only unsigned, outcome-blind input packages and
independent-authority signing requests. It never signs evidence and always
returns `capture_ready=false`. Transportation packages freeze the complete v8
historical panel/fact/staleness baseline before activation, then permit only the
exact scheduled cumulative append; the baseline and each append require signed
source-availability records for every panel and accepted-fact input. Missing,
post-cutoff, backdated, reordered, or semantically invalid inputs fail before an
unsigned signing request is emitted.

The capture command creates an immutable signal artifact only after reviewed
registration/activation, exact frozen-score replay, an external timestamp, and
all required source hashes. The evaluator verifies captures/outcomes and emits
only independent sleeve verdicts. It does not activate production.

`activation_cli` can only package the exact hash of a passing Transportation
sleeve evaluation plus a separately signed independent review into another
zero-cap artifact for manual change control.

All older Transportation 45/45a-45g evidence routes are superseded and fail
closed. Historical/revealed diagnostics, local ledgers, pre-effective scores,
and the missed 2026-08-24 slot count as zero prospective observations.

As of 2026-08-26, the Transportation clock has not started, zero observations
count, and the remaining exact floors are 12/4 per sleeve at 21/63 sessions (a
252-session lower bound from the first valid future entry, not a calendar-date
promise).
