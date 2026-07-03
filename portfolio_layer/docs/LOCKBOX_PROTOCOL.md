# Stage 11 Lockbox Protocol

**Declared:** 2026-06-27
**Status:** SEALED (never opened)
**Amendment policy:** append-only. Nothing above the Amendment Log may be edited after declaration;
corrections and decisions are appended as dated entries. Stage 11 scripts record this file's sha256 in
their manifests so any silent edit breaks provenance.

---

## 1. Purpose

Every backtest, calibration fit, or walk-forward comparison that is inspected and then acted on
(tuning a slope, widening a band, dropping a feature) leaks information from that history into the
system's design. After enough iterations the system is implicitly fitted to the data it was judged on,
and a good result on that data is weak evidence.

The lockbox is the defense: a slice of history that is **declared before any historical replay or
calibration result is inspected** and **never touched during development**. The finished system is run
on it exactly once. Because nothing was ever fitted to it, that single result is honest out-of-sample
evidence and is the sole basis for promoting shadow-only stages to production.

This document is the declaration. It exists BEFORE `research/65_pit_snapshot_store`,
`research/66_define_calibration_targets`, or any walk-forward has produced an inspectable result.
No forward-return join, payoff slope, or ablation statistic had been computed on any date in the
sealed window as of the declaration date.

## 2. Window declaration

All dates are snapshot (as-of) dates on the US master trading calendar.

| window | range | use |
|---|---|---|
| **Development window** | 2024-01-02 through 2025-12-31 | all iteration: calibration fits, purged CV, ablation walk-forwards, tuning |
| **Lockbox (sealed)** | 2026-01-01 through the Open Event | untouched until the one-time Open Event |

The lockbox has no fixed right edge: live-accumulated snapshots join the sealed window as they are
created, so the lockbox grows (and the test strengthens) the longer development takes.

## 3. What "sealed" prohibits — and what stays allowed

**Prohibited on lockbox dates until the Open Event** (by any person or script):

- Computing or inspecting forward-return labels, score-vs-realized-return joins, payoff slopes,
  IC/rank-IC statistics, or calibration diagnostics for snapshots dated in the lockbox.
- Running any backtest, replay, or walk-forward whose evaluation window overlaps the lockbox.
- Aggregate P&L attribution of scores/overlays over lockbox dates.

**Explicitly allowed** (these are live operations, not backtesting):

- Daily live operation of Stages 1–9 on current dates: score collection, risk panel, shadow books,
  ledger updates, exit recommendations.
- Archiving PIT snapshots of lockbox dates into the snapshot store (capture is mandatory; inspection
  of their *outcomes* is what is sealed).
- Data-quality validation of lockbox-dated artifacts (schema, hashes, coverage) that does not reveal
  score-vs-return relationships.

## 4. Purge and embargo rules

Forward-return labels look ahead up to 252 trading days, so late development-window snapshots have
label windows that extend into the lockbox. To prevent that leakage:

- **Training purge:** any model or calibration whose result will be evaluated at the Open Event may
  train only on snapshots whose **entire label window ends on or before 2025-12-31**. (For the 252d
  horizon that means training snapshots up to ~2025-01; for 21d/63d nearly the full dev window is
  usable. This is a further reason short horizons are calibrated first.)
- **Development-window CV:** all cross-validation inside the dev window uses purged, embargoed splits
  (embargo ≥ the label horizon) — no fold may train on labels overlapping its own test fold.
- **252d limitation (acknowledged):** a lockbox opened in late 2026 cannot fully verify 252d labels
  out-of-sample. 252d-horizon calibration therefore cannot be promoted at the first Open Event;
  it stays shadow regardless of dev-window results.

## 5. Registered comparison arms

The Open Event evaluates exactly the registered arms, walk-forward, net of the Stage 4 cost model:

1. `aqr_only` — Stage 3/4 baseline (the bar to beat)
2. `+rotation` — Stage 5 overlay
3. `+macro_bl` — Stage 6/7 fusion
4. `+sleeves` — Stage 8 risk-budget proposal
5. `+exits` — Stage 9 exit engine

Arms may be **added** to this registry (appended below with a date) any time before the Open Event,
provided they were developed exclusively on the development window. Arms may not be added at or after
the Open Event.

- `+payout` (Stage 10): **EXCLUDED by default** — the book is treated as accumulation-only. If a
  payout/withdrawal capability is ever wanted, Stage 10 must be built, dev-window tested, and
  registered here BEFORE the Open Event; adding it afterward requires a new lockbox cycle.
- `+forecast` (ML) and `+hedging`: not registered; per STAGE_GATES they may only be registered after
  the PIT calibration harness exists and they beat the simpler stack in dev-window tests.

## 6. Promotion criteria at the Open Event

Per STAGE_GATES Stage 11: an arm is promoted from shadow-only to production only if, **on the lockbox
window**, it beats the `aqr_only` baseline on **net-of-cost out-of-sample information ratio** (Calmar
and max drawdown reported alongside; SPY/sector-ETF beta and risk-parity comparisons reported as
context, not gates). If the full stack does not beat the simpler configuration, the simpler
configuration is promoted instead. Calibrated alpha slopes feed back into Stage 1/Stage 7 only if
their lockbox OOS diagnostics pass.

## 7. One-open policy

- The lockbox is opened **once**, by an explicit dated entry in the Amendment Log ("Open Event"),
  after the dev-window harness (65, 66, 15b, 16) is complete and the registered arms are final.
- The results are what they are. If they are disappointing, the system may of course be revised — but
  the opened window is **spent**: it becomes development data, and a **new lockbox** (accruing from
  the open date forward, minimum 6 months of live-accumulated snapshots) must be declared in the
  Amendment Log before any future promotion decision.
- There is no partial peek. Any inspection of lockbox outcomes, however small, constitutes the Open
  Event.

## 8. Enforcement

- `research/65_*`, `research/66_*`, and `backtest/16_*` must refuse to compute labels, joins, or
  evaluation results for snapshot dates inside the sealed window unless invoked with an explicit
  `--lockbox-open` flag, and must record this file's sha256 plus the flag state in their sealed
  manifests. Use of the flag before an Open Event entry exists in the Amendment Log is a protocol
  violation and invalidates the run.
- `backtest/17_publish_lockbox_ledger.py` writes the append-only OOS ledger at the Open Event; a
  tamper/lookahead probe over its hashes fails the build if violated.

---

## Amendment Log (append-only)

- **2026-06-27 — Declaration.** Protocol created and sealed. Dev window 2024-01-02..2025-12-31;
  lockbox 2026-01-01..Open Event. Payout arm excluded by default pending an explicit registration.
  No historical replay, forward-return join, or calibration result had been inspected as of this date.
- **2026-06-27 — Config mirror.** Window dates mirrored into `config.yaml` `stage11_lockbox:` for
  script enforcement. The protocol document remains canonical; scripts must verify config matches and
  refuse on divergence. `dev_window_start` may move earlier freely (backfilled history);
  `dev_window_end`/`sealed_start` change only via a dated entry here.
- **2026-06-27 — Payout intent recorded; Stage 10 deferred to last.** The user intends an eventual
  payout capability of approximately $15,000 (cadence TBD). Decision: Stage 10 is the LAST stage to be
  implemented; the `+payout` arm remains UNREGISTERED for the first Open Event. When built, the payout
  layer will be validated against a subsequent lockbox cycle (live-accrued data after the first open)
  before any production use. Rationale: payout is a constraint overlay, not an alpha source, so
  deferring its OOS validation to the next cycle does not compromise the first promotion decision.