# Biotech Net Profitability Replay and Promotion Operations

## Purpose

The walk-forward calibration ranks candidate scoring and selection policies without exposing the outer-test
fold to optimization. Final promotion is decided by a separate daily portfolio replay that measures actual
net wealth after execution lag, transaction costs, market impact, capacity limits, delistings, and terminal
recoveries.

The paired forward-return bootstrap remains a confidence diagnostic. It is required for full promotion, but
it is not an absolute veto on a challenger that produces better net wealth, profit factor, drawdown, CVaR,
and fold consistency. Such a challenger may receive only a small provisional allocation.

## Sequence

1. Run `60_run_biotech_walk_forward_calibration.py` on a fresh observation cache.
2. Train and validation choose the challenger structure. Outer-test data is used once for evaluation.
3. The runner invokes `62_compare_biotech_portfolio_profitability.py` automatically.
4. Script 62 replays challenger and production holdings one trading day after each frozen signal.
5. Script 62 independently reconstructs the result from frozen CSV inputs without database access.
6. A final profitability-governed candidate contract is written. It is never activated implicitly.
7. Script 61 validates the contract hash, scoring parity, replay verification, effective date, monitoring
   controls, and explicit approver before producing an immutable active contract.
8. Script 63 evaluates 30-, 60-, and 90-day live champion/challenger returns and recommends hold, scale,
   or rollback to the XBI residual sleeve.

## Net Replay Contract

The daily replay uses:

- one-bar execution lag;
- historical close prices from the configured source-priority contract;
- Norgate delisted-price overlays and explicit terminal recoveries;
- a conservative one-way base cost plus square-root market impact;
- a maximum percentage of historical average daily dollar volume;
- partial fills rather than assumed unlimited capacity;
- XBI for unallocated residual capital;
- arithmetic terminal losses and recoveries, avoiding invalid log returns at zero;
- fold-end liquidation so each untouched fold has explicit implementation costs.

Frozen normalized inputs are:

- `portfolio_replay_targets.csv`
- `portfolio_replay_price_inputs.csv`
- `portfolio_replay_terminal_events.csv`
- `portfolio_replay_folds.csv`

The independent verifier must reproduce every published comparison field within `1e-6` and records that it
did not access the database.

## Promotion Decision

The profitability score combines relative CAGR, Calmar ratio, profit factor, maximum drawdown, daily 5%
CVaR, and turnover. Hard controls require adequate paired daily support, multiple folds, fold consistency,
candidate profit factor of at least 1.0, and no material drawdown or CVaR deterioration.

Full promotion additionally requires the configured composite score, a high deflated-Sharpe probability,
and a positive paired daily block-bootstrap lower bound. Provisional promotion is permitted when terminal
wealth and the balanced scorecard are better but statistical confidence is incomplete. The default
provisional active-stock cap is 25%; the remaining sleeve stays in XBI.

No contract can activate unless its candidate formula and selection policy have exact live-scorer parity.
If the latest fold is a benchmark fallback or the scorer cannot reproduce the candidate, the profitable
research result remains shadow-only.

## Multiple Testing and Reproducibility

The effective trial count includes deterministic candidate/threshold combinations and Optuna trials. It is
used by the deflated-Sharpe calculation. Optuna cannot observe outer-test rows. Every source, normalized
input, result, and final contract is SHA-256 recorded in the profitability and walk-forward manifests.

## Live Monitoring Input

Script 63 consumes a CSV with:

- `date`
- `contract_id`
- `candidate_net_return`
- `incumbent_net_return`

The contract identifier must match on every row. A mismatch, material drawdown deterioration, or material
CVaR deterioration generates a rollback recommendation. Insufficient live support holds the provisional
weight. Scaling occurs one stage at a time only after the configured monitoring window passes.

## Principal Outputs

- `portfolio_profitability_comparison.csv` and `.json`
- `portfolio_profitability_daily.csv`
- `portfolio_profitability_trades.csv`
- `portfolio_profitability_fold_comparisons.csv`
- `profitability_promotion_decision.json` and `.md`
- `portfolio_profitability_verification.json`
- `portfolio_profitability_manifest.json`
- `production_policy_contract_profitability_candidate.json`
- immutable script-63 monitoring artifacts

The original horizon-return promotion decision remains in the final contract as statistical audit evidence;
the profitability decision is the production promotion authority.
