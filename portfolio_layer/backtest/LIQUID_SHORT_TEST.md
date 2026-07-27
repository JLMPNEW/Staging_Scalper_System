# Pre-registration: liquid-tier tactical short test (one-shot)

Registered 2026-07-26, BEFORE the run. Nothing below may be changed after the calibration starts.

## Background (the sealed result this test responds to)

The full-universe tactical short book was calibrated and sealed at
`output/tactical_short_calibration/2026-07-05-v3` on 2026-07-26:

| metric | sealed full-universe value |
| --- | --- |
| OOS selection alpha (arith. ann.) | **-5.695%** |
| OOS active t (HAC, lag 5) | **-2.243** |
| OOS profit factor | 0.749 |
| OOS trades / outer folds | 924 / 5 |
| positive sectors | 3 of 6 |
| stress net ann | -10.80% |
| fold consistency | 0.40 |
| per-fold selection alpha | -4.50%, -4.50%, -4.74%, -8.42%, -6.24% (all five negative) |
| promotion status | NOT_PROMOTABLE |

Every outer fold lost money. The book's entry universe reached down to a $5 adjusted open and a
$250k trailing 20-session median dollar volume, where the modelled half spread falls into the 25bps,
75bps and 300bps fallback tiers and borrow is frequently a conservative fallback.

## Hypothesis

**H1.** The full-universe short book was killed by transaction and borrow costs, not by an absent
signal. Restricting entries to the top-liquidity tier — where the price-tiered fallback half spread
resolves to 10bps (>= $20) or 25bps ($10-$20) rather than 75-300bps — leaves enough gross edge for
the ranking to produce positive, statistically significant out-of-sample selection alpha.

**H0 (the null this test can accept).** The short-side ranking carries no exploitable information at
any cost level; the liquid tier is negative, or indistinguishable from zero, out of sample.

## What changes, and what explicitly does not

The variant is `--liquid-tier` on `backtest/16e_tactical_short_replay.py` and
`backtest/16f_calibrate_tactical_short.py`, reading the new `tactical_short_liquid` block in
`portfolio_layer/config.yaml`. The flag is **fail-closed and off by default**: without it the block
is not read, and 16e refuses to start if the block would *loosen* either floor.

**Changed (exactly two gates):**

| gate | full universe | liquid tier |
| --- | --- | --- |
| `min_short_entry_price` (D+1 adjusted open) | 5.0 | **10.0** |
| `min_median_dollar_volume_20d` (trailing 20-session median of `adj_close * volume`) | 250,000 | **5,000,000** |

**Deliberately unchanged** (identical to the sealed run, same code path, same config values):

- price-tiered half-spread fallbacks (`20/10bps`, `5/25bps`, `1/75bps`, `0/300bps`) and the stress
  multipliers;
- fail-closed borrow: `allow_unknown_availability: false`, `missing_borrow_fee_annual: 0.25`,
  7-day staleness caps, shortable-share sizing check;
- the `minimum_objective_windows: 2` objective-window floor;
- the content-hashed candidate cache (the flag and the liquid block enter the cache key, so a
  liquid candidate can never be served a full-universe artifact);
- the feasible candidate grid: holds `[3, 5, 7, 10, 15]`, targets `[0.005, 0.01, 0.02, 0.03]`,
  stops `[0.05, 0.08]`, with the `60bps x 1.5 = 0.009` round-trip feasibility floor. The floor is
  **kept at the full-universe value on purpose**: at a $10 price floor the true round trip is
  cheaper, so retaining 60bps is conservative and keeps the searched box identical to the sealed
  run (30 feasible candidates, 10 rejected);
- no-tie selection (an objective tie selects nothing and the fold contributes no OOS evidence);
- grid-boundary flagging blocks promotion;
- 5 outer folds / 3 inner folds, purge = max hold + 2, expanding blocks;
- all `tactical_short.promotion` gates, including `min_trades: 500`, `min_active_t: 2.0`,
  `min_profit_factor: 1.10`, `min_positive_sectors: 4`, `min_stress_net_ann: 0.0` and the borrow /
  availability / OHLCV coverage floors.

**Known semantics, stated up front so it is not read as a result.** The tail selection
(`tail_fraction: 0.10` of each pipeline's ranked names) still runs on the full scored panel; the two
liquid gates are applied afterwards as entry admission. The tested universe is therefore
"the full universe's bottom decile INTERSECT the liquid tier", not "the liquid universe's own bottom
decile". This is the same gate-based construction the corrected engine already uses for its other
hygiene screens, and it is what keeps every other component identical to the sealed run.

## Artifacts (new suffixed roots; nothing existing is deleted or overwritten)

- `portfolio_layer/output/tactical_short_liquid_calibration/2026-07-05-liquid/`
- `portfolio_layer/output/tactical_short_liquid/2026-07-05-liquid/`

The sealed `tactical_short/2026-07-05-v3` and `tactical_short_calibration/2026-07-05-v3` are read
only for comparison and are never rewritten.

## Decision rule (fixed in advance)

Verdict is taken from `tactical_short_parameters.json` written by 16f, on the aggregated
out-of-sample evidence from the 5 outer folds:

- **PROMOTABLE** — `promotion_status == "PROMOTABLE"`, i.e. every gate above passes: OOS selection
  alpha > 0, active t >= 2.0, profit factor >= 1.10, >= 4 positive sectors, stress net ann > 0,
  >= 500 OOS trades, fold consistency >= 0.50 on >= 5 folds, no grid-boundary selection, no
  objective ties, coverage floors met.
- **NO_SELECTION** — fewer than 5 outer folds produce OOS evidence, or the liquid floors admit too
  few names for the engine to trade. This is a valid answer: it means the liquid tier is not
  testable with this universe, and it closes the program on coverage grounds.
- **NOT_PROMOTABLE** — anything else.

**Underpowered case, resolved in advance.** If the ONLY failing gate is `min_trades` (< 500 OOS
trades) while OOS selection alpha is positive AND active t >= 2.0, the result is reported as
UNDERPOWERED-POSITIVE and the program stays open for one power extension. In every other
combination — including a trade shortfall alongside a negative or insignificant alpha — the verdict
is NOT_PROMOTABLE.

## One-shot rule

This is the **final** test of the systematic single-name short program.

If the verdict is **NOT_PROMOTABLE** or **NO_SELECTION**, the short program is **closed
permanently**. The coverage is then complete: the full universe was tested and failed
(-5.7%/yr, t -2.24, 5/5 folds negative), and the most favourable cost environment available in this
data — the top-liquidity tier at 10-25bps — was tested and also failed. No further short variants
will be proposed or run: no alternative universes, no alternative cost assumptions, no alternative
grids, no re-selection of the tail, no sector subsets. The sealed artifacts stand as the record.

If the verdict is **PROMOTABLE**, the liquid-tier parameters go to the normal Stage 11 promotion
review; they are not live-promotable on the strength of this run alone.

## Amendment 1 — 2026-07-26T21:20Z (infrastructure only, no statistical change)

Attempt 1 (launched 18:52Z) died at `outer3 candidate 18/30` with
`Command ... timed out after 900.0 seconds`. The cause is machine load, not the model: unrelated
jobs were saturating the disk, the 3.8 GB market-positioning snapshot alone took 62 minutes, and one
16e candidate replay crossed the shared `candidate_timeout_seconds: 900` ceiling.

`tactical_short_calibration` was **not** modified. Instead `tactical_short_liquid` gained three
wall-clock keys — `candidate_timeout_seconds: 7200`, `candidate_max_attempts: 3`,
`candidate_retry_delay_seconds: 5` — and 16f now applies liquid-block overrides through an explicit
allowlist (`LIQUID_INFRASTRUCTURE_OVERRIDE_KEYS`). The allowlist contains only those three keys; a
liquid block naming any statistical key (grid, folds, objective, penalties, tolerances, cost floor)
is a hard startup failure, and this is asserted in `16f --selftest`. No number in the hypothesis,
the gates, the grid, the fold layout or the decision rule above changed, and no result had been
observed for any outer fold when the amendment was made (attempt 1 produced no OOS evidence at all
— it died inside the first fold's inner-selection loop).

Because the config and 16f hashes both changed, the candidate cache key changed and attempt 2
recomputes from scratch. Attempt 1's partial work directory is superseded, not reused.
