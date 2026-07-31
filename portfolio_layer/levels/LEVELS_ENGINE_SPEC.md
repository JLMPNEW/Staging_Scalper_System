# Advisory Valuation and Execution Levels Engine - v1 Specification

> **Authoritative implementation sequence:** `portfolio_layer/MONITOR_LEVELS_IMPLEMENTATION_PLAN.md`
> governs provider selection, realistic observability, evidence ledgers, build increments, and
> definitions of done. This specification governs its domain formulas and contracts.

Status: FROZEN DESIGN - implementation pending.
Frozen: 2026-07-28.
Home: `portfolio_layer/levels/`.

## 1. Objective and boundary

The levels engine publishes auditable valuation ranges, long-entry ceilings, execution bands, trim
zones, and inactive reasons. It is an advisory execution overlay over sealed portfolio artifacts.

It does not:

- feed prices, bands, or states into Stage 1 scores or any optimizer;
- change target weights, final books, orders, exits, or payout decisions;
- forecast one precise future stock price;
- call a market reference "fair value";
- activate entries for names without a defensible valuation anchor;
- reactivate systematic single-name shorting.

The engine follows the governing rule "forecast risk, not point price." It exposes ranges and
conditional action zones. Any later automated use requires a separate, prospective promotion gate.

## 2. Operating principle

**Interpretable structure, empirically earned parameters.**

The initial formulas are transparent scaffolding. Margin-of-safety coefficients, uncertainty
penalties, volatility-band widths, trend shifts, and event rules are research candidates until they
pass purged OOS validation net of realistic execution costs. No parameter is described as optimal
before that evidence exists.

## 3. Two-layer architecture

### 3.1 Valuation layer

The valuation layer estimates an economically defensible distribution without using moving
averages, VWAP, price momentum, or current-price trend:

```
valuation_low
valuation_base
valuation_high
```

Eligible anchor families:

- earnings value: normalized forward EPS x a PIT target P/E;
- cash-flow value: normalized forward FCF / required FCF yield;
- enterprise value: normalized EBITDA x PIT EV/EBITDA less net debt and other senior claims,
  divided by split-consistent diluted shares;
- sector-specific methods explicitly allowed by the sector valuation contract.

Each method emits applicability, estimate, uncertainty, freshness, and reliability. Invalid methods
are excluded rather than forced:

- non-positive or non-meaningful EPS disables earnings-value methods;
- non-positive or structurally unstable FCF disables FCF-yield methods;
- stale debt, cash, share-count, or filing inputs fail the affected method closed;
- unprofitable growth, cyclicals, and development-stage biotech use only methods explicitly approved
  by their sector adapter;
- current price or market technicals can never substitute for a missing valuation anchor.

Applicable estimates are combined with a robust reliability-weighted median. The engine preserves
every component and reports `anchor_disagreement`; it never stores only the blended result.

### 3.2 Execution layer

The execution layer decides whether and where an economically valid valuation can be acted on. It
owns current-price context, volume-weighted daily price references, moving averages, support zones,
ATR/realized volatility, overnight gaps, trend, liquidity, expectations state, and event windows.

The execution layer first generates candidate market-structure zones, then applies the
valuation-derived ceiling:

```
candidate market zone
        -> valuation ceiling and activation gates
        -> active entry band OR explicit no-entry result
```

If no candidate zone exists at or below the long-entry ceiling, no active entry band is published.
The engine does not place an artificial order at a remote valuation number or chase the market above
the valuation ceiling.

## 4. Valuation inputs and PIT contract

Each sector publishes a sealed valuation-input artifact with one row per ticker and at least:

```
as_of_date
available_at_utc
ticker
source_pipeline
company_type
currency
fiscal_period_end
revenue_forward
eps_forward
fcf_forward
ebitda_forward
net_debt
senior_claims
diluted_shares
normalized_cyclical_flag
method_allowlist
input_freshness_json
source_artifact_sha256
valuation_contract_version
```

Sector adapters may add specialist inputs, but shared code never guesses their meaning. Examples
include probability-weighted pipeline value for biotech, backlog/cycle normalization for defense or
machinery, and cohort-specific profitability rules.

Every historical input must reflect what was available at `available_at_utc`. Current peer
membership, revised fundamentals, or present-day share counts may not be backfilled into historical
levels.

Target-multiple candidates are constructed PIT from:

- the company's own rolling history;
- sector-relative history using contemporaneous membership;
- quality-adjusted peers defined by versioned, dated mappings.

The initial 50/30/20 blend is a research candidate, not a production truth. Structural breaks,
insufficient history, and sparse peers reduce reliability or disable the relevant component.

## 5. Consensus and expectations

Consensus is absent in Phase 1. A future consensus anchor requires append-only PIT estimate
snapshots containing fiscal period, estimate value, analyst count, currency, provider publication
time, fetch time, and payload hash.

Current estimates returned by an API today are not historical vintages. Price targets are
diagnostic context and never become the consensus anchor.

Credible company-issued guidance may update forward valuation inputs when it is structured,
source-linked, and PIT. Analyst revisions may update consensus inputs after sufficient prospective
history exists.

The residual expectations price multiplier is fixed at zero in Phase 1:

```
lambda_expectations = 0
```

This prevents guidance or estimate revisions from entering both the forecast inputs and a second
price multiplier. Expectations states affect activation:

- `green`: additions may be considered if every other gate passes;
- `stable`: additions require valuation and stabilization support;
- `watch`, `deteriorating`, `broken`: all entry/add bands inactive;
- a blocking escalation rule suspends additions immediately, independent of the composite score.

## 6. Margin of safety and uncertainty

Intrinsic valuation is not changed by risk. Risk and uncertainty determine the required discount:

```
margin_of_safety =
    base_margin
  + uncertainty_penalty
  + financial_risk_penalty
  + liquidity_penalty
  + event_gap_penalty

long_entry_ceiling = valuation_base * (1 - margin_of_safety)
```

All components are separately stored, non-negative, bounded, and configured by company type. The
initial coefficients are shadow parameters. Anchor disagreement and stale inputs increase the
uncertainty penalty and reduce sizing confidence; they do not move intrinsic value.

## 7. Market structure and volatility

The shared Stage 2 OHLCV extension must provide split-consistent open/high/low/close/volume with
corporate-action validation. A daily volume-weighted price computed from daily bars must be labeled
`volume_weighted_daily_price`, not intraday VWAP.

Candidate execution zones may use:

- 63-day volume-weighted daily price;
- 50-day and 200-day moving averages;
- recent support/resistance pivots;
- ATR20 and ATR60;
- robust 20-day/EWMA realized volatility;
- overnight-gap distribution;
- current bid/ask liquidity snapshot.

The original fixed multiples (`0.4V`, `0.8V`, `1.5V`, `2.0V`) are candidate-grid values only. The
engine records all candidates and the selected configuration. Promotion requires stability away
from grid boundaries and confirmation on untouched data.

Trend can change activation, patience, or the placement of execution zones in volatility units. It
cannot alter `valuation_low/base/high`. A downtrend plus negative expectations evidence suspends
new long entries. A price decline alone creates review evidence, not a forced thesis conclusion.

## 8. Event handling

The engine consumes the sealed earnings calendar, expectations-state artifact, and sector catalyst
contracts for the same run.

- Imminent earnings widen uncertainty or suspend entries according to a configured, versioned rule.
- Binary biotech catalysts default to suspended new entries unless a separately validated event
  policy applies.
- Material guidance cuts, accounting issues, financing distress, or confirmed thesis-break events
  suspend additions immediately.
- Inactive levels remain stored with their reason; they are never silently deleted.

## 9. Universe and actions

The levels universe is the same union defined by the expectations monitor:

```
all sealed Stage 1 scored names
UNION actual broker holdings
UNION current final target names
```

Action policy:

| Name state | Entry/add levels | Risk/review levels | Trim levels |
|---|---|---|---|
| investable + valid valuation | eligible subject to all gates | yes | yes if applicable |
| scored but not investable | inactive | yes | yes if held |
| held but unscored | inactive | yes | yes |
| no valuation anchor | inactive_no_valuation_anchor | market diagnostics only | yes if held, clearly diagnostic |
| expectations watch or worse | inactive_thesis_suspended | yes | yes |

A falling price is never sufficient for an opportunity alert. An actionable add/open candidate
requires all of:

- current `investable_eligible=1`;
- valid and fresh valuation anchor;
- candidate market zone at or below the long-entry ceiling;
- green/stable expectations state and no blocking escalation;
- acceptable spread, ADV, and data quality;
- no blocking earnings/catalyst window;
- stabilization or positive-confirmation evidence defined in the frozen configuration.

This design can identify precursors and react quickly to deterioration. It cannot guarantee that
every falling knife is detected before the price falls.

## 10. Outputs

Per-run artifact:

`output/runs/<asof>/levels/levels.csv`

Minimum fields:

```
as_of_date
available_at_utc
ticker
source_pipeline
universe_tier
is_holding
is_target
investable_eligible
valuation_status
valuation_low
valuation_base
valuation_high
valuation_methods_json
anchor_disagreement
valuation_confidence
market_reference
market_structure_json
base_margin
uncertainty_penalty
financial_risk_penalty
liquidity_penalty
event_gap_penalty
margin_of_safety
long_entry_ceiling
starter_band_low
starter_band_high
add_band_low
add_band_high
trim_band_low
trim_band_high
level_status
inactive_reason
expectations_state
event_state
data_freshness_json
valuation_contract_version
levels_model_version
```

The output directory also contains:

- `levels_components.csv`;
- `levels_meta.json`;
- `levels_manifest.json`;
- `validation/levels_validation.csv`.

All manifests hash configuration, source code, sealed inputs, provider/source artifacts, and output
files. Writes are atomic and deterministic.

## 11. Outcome ledger

Calibration requires an append-only, tamper-evident ledger created with the first shadow run. One row
is written for every emitted band, including inactive bands:

```
level_id
published_as_of
published_at_utc
ticker
band_type
band_low
band_high
level_status
inactive_reason
market_price_at_publish
model_version
config_sha256
input_manifest_sha256
code_sha256
```

Outcome resolution appends, never rewrites:

```
first_touch_date
first_executable_fill_date
entry_price_assumption
max_favorable_excursion
max_adverse_excursion
forward_returns_by_horizon
spread_and_cost_assumptions
expectations_state_changes
event_occurrences
resolution_available_at_utc
```

The ledger uses hash-chained rows, a writer lock, monotone sequence numbers, and first-write-wins
semantics. Rebuilding historical artifacts cannot create prospective evidence.

## 12. Acceptance gates

Hard gates:

1. Sealed inputs are current, hash-valid, and no later than the run as-of.
2. Every open holding appears in `levels.csv`.
3. No active entry/add band exists for an ineligible, unvalued, stale, or thesis-suspended name.
4. Valuation outputs never consume market-reference or trend fields.
5. Execution zones never exceed the long-entry ceiling.
6. Invalid valuation methods are excluded with explicit reasons.
7. OHLCV is split-consistent; future bars and partial current-session bars are rejected.
8. All values are finite, dimensionally consistent, and currency/share-basis aligned.
9. Deterministic rebuild reproduces every component and final level.
10. Outcome-ledger integrity and manifest/code hashes verify.
11. Stage 1, optimizer, final target book, and broker ledger remain byte-unchanged.
12. Provider outages produce explicit coverage flags and cannot fabricate fresh events or estimates.

Synthetic probes include negative EPS, negative FCF, stale debt/share counts, net cash, missing
consensus, conflicting anchors, split events, earnings tomorrow, guidance cuts, held-but-unscored
positions, no valuation anchor, and a falling price without stabilization.

WARN-only diagnostics:

- anchor disagreement and low confidence;
- distance from current market to valuation/entry ceiling;
- inactive opportunity count by reason;
- provider and sector valuation coverage;
- band-touch and post-touch outcome summaries;
- parameter selections at grid boundaries.

## 13. Calibration and promotion

The engine starts advisory/shadow-only. Research evaluates economic decisions, not whether the level
was numerically close to a later stock price.

Primary outcomes:

- forward excess return after an executable band touch;
- maximum adverse excursion before favorable excursion;
- fill probability and time-to-fill;
- replacement opportunity cost when no band is reached;
- net performance after spread, commissions, and turnover;
- false-add rate during deteriorating expectations states.

Use purged walk-forward validation with entry-lag-aware label windows, survivorship-complete data,
code-hashed manifests, multiplicity control, stable non-boundary parameters, and a one-shot untouched
confirmation set. Calibration is performed by sector/company type where sample size permits and
shrunk toward pooled defaults otherwise.

No automated execution or optimizer feedback is authorized by this specification. Such use requires
a new pre-registered campaign that beats the simpler no-level or fixed-policy baseline OOS net of
cost.

## 14. FMP and consensus integration

The levels engine never calls FMP directly. It consumes sealed, normalized monitor artifacts.

FMP may supply:

- analyst action events;
- estimate snapshots;
- press releases/news as secondary evidence;
- diagnostic price-target context.

Only append-only, PIT estimate snapshots can enter the future consensus anchor. Provider coverage,
analyst count, staleness, fiscal-period alignment, currency, and split basis are mandatory. Missing
or low-quality consensus causes weight redistribution among valid valuation anchors; it never causes
use of current market price as fair value.

## 15. Implementation sequence

1. Amend and build the provider-independent expectations monitor.
2. Add shared Stage 2 OHLCV, corporate-action validation, and executable-price conventions.
3. Define and validate sector-owned PIT valuation-input contracts.
4. Implement shared valuation method applicability and robust aggregation.
5. Implement long-only market-structure zones and valuation-ceiling gating.
6. Emit sealed advisory artifacts and start the outcome ledger immediately.
7. Operate shadow-only and audit coverage, false alerts, band touches, and data freshness.
8. Calibrate margins and band parameters only after enough prospective outcomes accrue.
9. Add consensus after sufficient PIT estimate-vintage history exists.
10. Keep short-side levels diagnostic unless a new short campaign independently earns promotion.

## 16. Proposed module layout

```
levels/
  LEVELS_ENGINE_SPEC.md
  levels_common.py
  valuation_contract.py
  valuation_methods.py
  market_structure.py
  outcome_ledger.py
  45_build_valuation_ranges.py
  46_build_execution_levels.py
  47_validate_levels.py
  48_resolve_level_outcomes.py
  providers/                       # sector valuation-contract adapters only
  data/                            # versioned method/company-type overrides
```

The script numbers are provisional and must be checked against the repository before implementation.
