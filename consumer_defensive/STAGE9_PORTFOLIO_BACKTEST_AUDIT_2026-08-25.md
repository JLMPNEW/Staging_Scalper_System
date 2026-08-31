# Consumer Defensive Stage 9 Portfolio Backtest Audit

## Decision

Stage 9 is complete through isolated acceptance. Run
`cds9_63065740a60179d1a1abc968` passes all 31 independent checks and is
permitted only as a Stage 10 reporting input. It does not authorize a model,
weight, OOS-state, portfolio-gate, or production change. The frozen Stage 7
core baseline remains the scoring source.

## Frozen Inputs

- as-of date: `2026-08-14`;
- Stage 6C run: `3`, panel SHA-256
  `d2c7155be91cf21c2826e911e083e662bf203119ee087baf12f754ac2d2adcf0`;
- Stage 7 contract:
  `d5184d007b89f3be62c61277cd4ddcb864f15ff0ccd09d9234de31922cf909c8`;
- Stage 8 run: `cds8_2a94264294f4b58b1444fb2d`;
- Stage 8 candidate registry:
  `ab9ce2b8c647a9c30f37d146d6d2d71f6f0e503b846d14c51491b944f13d5ad9`;
- Stage 8 split:
  `d71a3548e6f5a36d5f4ac8c45af91815225afad817dbaf77bf280f055b2e3c9e`;
- Stage 9 policy:
  `24f2847a60873b30daf33c8d29798781ba96ef474ac13ba1c7e41dc003ac159f`.

The Stage 9 policy is a separate file. This preserves the accepted Stage 8
`config.yaml` hash instead of retroactively changing its methodology lockbox.

## Backtest Contract

All 320 Stage 8 candidates were registered before label evaluation. Each is
evaluated in four specifications:

1. long-only top quintile, equal weight;
2. long-only top quintile, score weight;
3. dollar-neutral long-short top/bottom quintile, equal weight; and
4. dollar-neutral long-short top/bottom quintile, score weight.

Each specification reports total and XLP-relative returns. The test uses frozen
21-session Stage 6C labels. An earliest-start greedy schedule selects 40
non-overlapping windows from 56 calibration slots; 16 overlapping slots remain
explicit cash observations in annualization. The selected split census is 22
train, five first embargo, three validation, six second embargo, and four final
holdout windows.

Costs and risk controls are explicit:

- 20 basis points one way on gross traded notional, including initial entry,
  drift-adjusted rebalances, gap liquidation/re-entry, and final liquidation;
- observed annual borrow fees for short positions plus a separate 5% annual
  stress rate where a fee is unavailable;
- 63-session ADV capacity at 10% participation and a 5% stress case, five exit
  days, and a $100 million reference NAV;
- volatility, maximum drawdown, turnover, capacity, liquidation days, cohort
  concentration, single-name concentration, and terminal-value attribution.

## Accepted Evidence

| Evidence | Result |
| --- | ---: |
| Candidate count | 320 |
| Portfolio specifications | 4 |
| Return bases | 2 |
| Summary rows | 2,560 |
| Period rows | 46,280 |
| Holding rows | 377,106 |
| Calendar / selected windows | 56 / 40 |
| Terminal-return panel rows consumed | 4 |
| Independent validation | 31/31 PASS |
| Generator artifact exact replay | 8/8 hashes unchanged |
| Consumer Defensive + shared factor-validation regressions | 494 passed, 5 skipped |
| Ruff / Python compile / diff integrity | PASS |
| Database writes | 0 |

Contract SHA-256:
`63065740a60179d1a1abc96806f5e82198efa9221161aa32f640b34b4e11ce1f`.
Manifest self-hash:
`03346ffceb33b9f1c7b974229cad4ec1f5638945422d476f8cbe8aca3b1df183`.

The rehearsal database SHA-256 was
`41f67310dca004bc100cd8016ec187ac58c269f6b56a424846eccf6fc04efdf7`
both before and after generation. The scripts connect with SQLite `mode=ro`
and `query_only=ON`; production was not opened or modified.

## Capacity Caveat

The $100 million reference NAV is intentionally conservative and is not
supported consistently by the historical cohort portfolios. For the Stage 7
equal-weight long-only baseline, observed reference-NAV capacity-pass fractions
range from 5.0% to 54.3% by cohort and are 30.0% at sector scope; the 5%
participation stress fractions range from 0.0% to 41.2% and are 10.0% at sector
scope. This is a reportable liquidity constraint, not a reason to fabricate
volume or approve a smaller production portfolio. Stage 10 must expose the
capacity fields and preserve the report-only label.

## Failure History And Fixes

- v1 failed closed at 22/23 checks because the Stage 8 panel source hash was
  looked up at the wrong manifest level and serialized as blank. The builder
  now requires a valid 64-character SHA-256 for every tieout row, and the
  validator independently reconstructs the complete source tieout.
- v2 exposed a validator-only terminal-marker naming mismatch. The validator
  now uses the canonical `_terminal_return_used_flag` and independently binds
  terminal consumption.
- v3 adds methodology-file immutability, run-ID derivation, decision self-hash,
  decision/manifest contract binding, complete artifact-hash census, exact
  Stage 6C/7/8 bindings, and source-tieout reconstruction. All pass.
- Stage 6C validation previously attempted schema bootstrap on a read-only
  connection. It now performs schema, migration and foreign-key checks without
  writes when `query_only=ON`, with a dedicated regression test.

The failed v1 and superseded v2 directories remain immutable audit evidence;
only v3 is accepted.

## Next Gate

Stage 10 may now publish deterministic current and dated reports using the
retained Stage 7 baseline and links to this Stage 9 evidence. Stage 10 must not
reinterpret the deep replay as contemporaneous OOS evidence or enable any
portfolio gate. Stage 10B governance, Stage 11 Portfolio Layer integration,
Stage 12 orchestration, clean-room acceptance, and production migration remain
downstream gates.
