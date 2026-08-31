# Transportation Generic OOS and Production-Parity Status

> **Superseding correction (2026-08-25):** This July status is historical. The
> shared production-lock mechanics described below are no longer a promotion
> authority for Transportation. Corrected calibration execution is `PASS`,
> predictive acceptance is `FAIL`, and the family remains disabled and zero-cap.
> Only the canonical three-authority future-only route can create new admissible
> evidence. See
> [TRANSPORTATION_V8_CORRECTNESS_AUDIT_2026-08-25.md](TRANSPORTATION_V8_CORRECTNESS_AUDIT_2026-08-25.md).

As of 2026-07-31, transportation now has the historical and governance
structure needed to perform the same class of point-in-time generic-score
evaluation used by the defense and machinery industrial subsectors. No
transportation score has been promoted to production because the untouched
holdout and walk-forward gates did not pass.

## Shared industrials infrastructure used

Transportation remains a family-specific implementation under
`industrials/transportation`, but delegates cross-family mechanics to
`industrials/core`:

- `market_feature_history.py` calls the shared industrials market-feature
  formula implementation and loads each price series once.
- `historical_score_history.py` defines the daily benchmark calendar,
  immutable snapshot checks, and resumable history controls.
- `score_history.py` owns the standard Stage 11 survivorship-corrected
  research sidecar contract.
- `oos_research.py` owns weekly selection, D+1 split/dividend-adjusted
  open-to-open outcomes, terminal membership handling, purged splits,
  score weighting, IC, turnover, and sleeve evaluation.
- `production_lock.py` owns the family-neutral effective-dated immutable
  production-lock registry contract.

Transportation-specific cohort applicability, reporting policies, metric
definitions, aliases, security-continuity decisions, and score components
remain inside the transportation package.

This follows the same ownership boundary used by defense and machinery:
shared configuration, database, policy, reporting, refresh-lock, history,
research, and portfolio contracts live under `industrials/core` or the shared
family-parameterized scripts; only transportation policy and thin orchestration
wrappers live under `industrials/transportation`. The combined industrials and
dedicated-parser regression suite passes, as do transportation Ruff, Pyright,
and compilation gates.

## Historical artifacts completed

The history build used already-loaded daily price data and the latest sealed
month-end PIT financial/specialized snapshot at or before each trading date.
It did not rerun SEC parsing or the dedicated parser for every day.

- Daily shared market features: 1,904 dates, 2019-01-02 through 2026-07-30.
- Daily PIT transportation rank and Stage 11 sidecar history: 1,904 dates.
- Active and inactive membership sources are both represented.
- Independent full-history audit: PASS.
- Membership rows audited:
  - historical membership source: 195,617
  - delisted calibration source: 757

The frozen 2026-07-30 eligibility policy is replayed on historical PIT
features. Feature dates, market dates, filing availability, membership
windows, aliases, and security-continuity boundaries remain historical.

## Generic OOS panel

The weekly cadence is an evaluation cadence, not a portfolio rebalance rule.
The underlying rank history remains daily.

- Weekly snapshots: 382
- Date range with complete 21/63-session IYT outcomes:
  2019-01-04 through 2026-04-24
- Panel rows: 78,568
- Production-universe eligible rows: 35,432
- Return basis: next-session adjusted open to adjusted open, excess to IYT
- Production research universe: operating/core transportation issuers
- Splits:
  - train: 216 weekly dates
  - embargo: 26 weekly dates
  - validation: 63 weekly dates
  - holdout: 77 weekly dates
- Independent panel/hash/PIT validation: PASS

The independent validation caught and corrected a terminal-event defect:
seven delisted securities initially admitted a terminal exit on or before
the modeled D+1 entry. Those 14 horizon observations are now marked
`missing_verified_terminal_outcome`; they are not used as returns.

## Calibration result

The finite candidate registry was frozen before holdout evaluation. Candidate
selection did not access the holdout. `growth_quality` was selected from the
validation split and passed every validation gate.

The selected candidate failed the untouched holdout:

- mean IC: -0.056819
- mean top-sleeve excess return after cost: -0.012352
- top-sleeve excess hit rate: 0.376623
- all four expanding walk-forward blocks failed the joint positive-return
  and non-negative-IC stability rule

The frozen baseline also failed the holdout:

- mean IC: -0.069761
- mean top-sleeve excess return after cost: -0.016141
- top-sleeve excess hit rate: 0.311688

A separate 21-session diagnostic did not provide a defensible alternative:
no preregistered candidate had both non-negative holdout IC and stable
positive excess return.

This is not a missing-history or parser-coverage failure. The panel has full
eligible-row outcome coverage in validation and holdout. The current generic
transportation score lacks stable out-of-sample cross-sectional efficacy over
the sealed holdout.

## Production and portfolio state

The production-readiness audit executes and passes its own integrity checks,
but correctly reports `promotion_readiness=FAIL`. The promoter refuses to
write a production bundle when holdout or walk-forward evidence fails.

Therefore:

- the transportation production-lock registry remains header-only;
- `oos_score_valid_flag` remains zero;
- `portfolio_candidate_gate` remains zero;
- the portfolio-layer source remains optional and fail-closed;
- transportation sector and strategic budgets remain zero.

The rank publisher is now structurally able to consume a valid shared
effective-dated production lock in a future passing cycle. Without such a
lock it preserves the existing shadow contract.

## Remaining governed decisions

1. Do not rerun parsers, rebuild historical features, or relax acceptance
   gates in response to the failed holdout.
2. Complete an outcome-blind model-diagnostic review of component direction,
   component availability/renormalization, cohort comparability, regime
   dependence, and long-only sleeve construction.
3. Pre-register any materially revised scoring hypothesis and candidate
   registry before opening a new untouched evaluation period.
4. Continue the existing month-end specialized-overlay monitor. It is a
   separate evidence stream and currently has zero qualifying monthly signal
   dates; it cannot authorize generic score promotion.
5. Only after a future OOS package passes should an explicit activation
   operation:
   - seal the promotion decision,
   - append the effective-dated production lock,
   - republish the current rank with OOS-valid/candidate flags,
   - switch the portfolio source from optional to required,
   - authorize a reviewed nonzero transportation pilot budget,
   - rerun the portfolio adapter and full portfolio replay.

Production activation is intentionally a distinct, high-impact operation.
It must not be executed while the current readiness audit is failing.

## Release-seal status

The executable implementation gates pass. The prior
`code_aligned_zero_overlay_v2` release-integrity seal does not pass against the
new implementation because its manifest pins the earlier `industrials/config.yaml`
hash and requires every release source to be committed. The current working
tree contains the new transportation/shared-core files plus unrelated user
changes. This is a source-control/release-version boundary, not a parser,
history, calibration, or portfolio-contract defect. A new release must be
sealed from an authorized, scoped commit; the old immutable release must not be
silently rewritten.
The successor contract is `code_aligned_zero_overlay_v3`. Script 22a packages
the already-built shadow artifacts only after every declared source dependency
is tracked, committed, and clean. Its dependency manifest includes the
transportation package and tests, shared `industrials/core` and family scripts,
the independent `dedicated_parser` implementation, and the portfolio-layer
industrial-family adapter. Scripts 23 and 24 accept an explicit release name
and audit the packaged daily-history, generic-OOS, required-metric-repair,
current-rank, portfolio-validation, and governance evidence. Packaging and
acceptance perform no parsing, loading, feature building, calibration,
promotion, portfolio write, or production-config write.
