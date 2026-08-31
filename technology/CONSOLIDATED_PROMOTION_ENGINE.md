# Technology Consolidated Promotion Engine

The technology promotion engine is shared infrastructure for semiconductors,
software infrastructure, and technology hardware. Calibration, weights, panels,
and promotion decisions remain family-specific.

## Decision hierarchy

1. Validate the sealed Stage 8 and walk-forward artifacts against the current
   configuration and signal panel.
2. Compare the candidate with the incumbent on the untouched Stage 8 holdout
   using the net-of-cost, equal-weight, long-only top-quintile portfolio.
3. Evaluate 21-, 63-, and 126-session economics. The longer horizons compound
   non-overlapping 21-session portfolio returns; they do not reuse overlapping
   forward labels.
4. Score economic advantage, risk efficiency, predictive evidence, and
   deployability. Statistical evidence changes confidence; a t-statistic below
   2.0 is not by itself a veto.
5. Apply only hard safety constraints: governed inputs, no post-lock research
   override, enough matched holdout periods, absolute drawdown/expected-shortfall
   limits, turnover/cohort caps, cost limits, and reference-notional liquidity.
6. Emit one of `full_promotion`, `limited_promotion`, `shadow_challenger`, or
   `retain_incumbent`.

The runner never modifies production weights. A full or limited recommendation
requires an explicit approval/receipt step before activation. Limited promotion
is capped at the policy exposure fraction while evidence accumulates.

## Commands

Evaluate existing governed artifacts:

```powershell
python technology/scripts/22_run_technology_consolidated_promotion.py
python technology/scripts/22_validate_technology_consolidated_promotion.py
```

Rebuild diagnostics, Stage 8, walk-forward calibration, and Stage 9 backtests
sequentially for every technology family before evaluation:

```powershell
python technology/scripts/22_run_technology_consolidated_promotion.py --run-calibration
```

Outputs are written under
`output/technology_reports/consolidated_promotion`, with immutable copies under
its `runs` directory.
