# Transportation Surface-Freight Score Redesign ? 2026-08-01

## Decision

The implementation defects identified by the 2026-07-31 audit have been
remediated. A smaller, economically coherent transportation research cohort and
a metric-level score redesign have also been implemented. The redesigned model
is **not authorized for production promotion** because no candidate passes the
complete validation gate and both validation and holdout have now been exposed
during research iteration.

This is a model-evidence failure, not a parser, price arithmetic, cohort breadth,
or portfolio-adapter failure.

## Pipeline hardening completed

1. Exact-zero candidate metrics are no longer treated as missing by the
   `value or -999.0` sorting pattern. The shared `finite_or_default` helper
   preserves `0.0` and substitutes the default only for non-finite values.
2. The generic OOS builder now materializes the exact bounded price observations
   used by the panel. The panel is calculated from the rehydrated frozen slice,
   not from a separate in-memory representation.
3. The price-slice path, SHA-256, row count, start date, end date, and signal
   lookback are pinned in the panel manifest.
4. The permanent validator independently reconstructs security returns, IYT
   returns, excess returns, and scheduled session horizons from the frozen slice.

The isolated redesign panel contains 198,464 frozen price observations. All
78,318 available panel outcomes reconstructed successfully with zero missing
price observations and a maximum absolute error of
`5.115907697472721e-13`, versus the `1e-9` acceptance tolerance.

## Outcome-blind research cohort

The new cohort is `north_american_surface_freight_and_logistics`. Membership is
defined before outcome inspection using operating status, portfolio role,
economic peer group, and common freight-cycle exposure. Historical evaluation
retains then-live delisted issuers; the current serving list contains active
issuers only.

The cohort has 33 current active names. Five names are excluded structurally:

- `FSTR`, `GBX`, `RAIL`, and `TRN`: rail-equipment or infrastructure
  manufacturers rather than freight operators.
- `ZTO`: a China parcel network with materially different macroeconomic,
  regulatory, and currency drivers.

Metrics are normalized inside two business-model comparison groups before the
resulting scores are compared across the shared cohort:

- Asset-light logistics: contract warehousing, freight brokerage, integrated
  freight/logistics, and intermodal.
- Asset-based freight: trucking, rail operators, auto haul, fleet leasing, and
  surface-equipment leasing.

This prevents asset-turnover, capex, and valuation ranks from directly comparing
brokers with railroads or equipment lessors.

## Current research-ranked names

Twenty-four names have complete current redesigned scores:

`EXPD, RLGT, FDX, GXO, UPS, FWRD, CHRW, LSTR, HUBG, SNDR, JBHT, KNX, TFII,
ARCB, UNP, XPO, CNI, CVLG, ODFL, SAIA, NSC, CP, CSX, WERN`.

Nine cohort members remain research-score ineligible at the current snapshot:

- Not rank-ready: `FDXF, PAL, PAMT, ULH`.
- Incomplete selected metrics: `GATX, HTLD, MRTN, R, RXO`.

These are research ranks, not production recommendations or portfolio-approved
names.

## Train-only metric redesign

The train split selected five metrics that met unchanged coverage, history,
positive-IC, and subperiod-stability requirements:

- `capex_to_revenue`
- `fcf_yield`
- `asset_turnover`
- `ev_operating_income`
- `revenue_growth`

Two bounded mean-reversion variants were also tested because
`relative_strength_3m` and `ret_12m_ex_1m` had persistent negative train IC at
the 63-session horizon. Those variants failed validation and are not selected.
Fundamental metric directions were not inverted to manufacture a passing score.

## Results

| Candidate | Validation net excess | Validation IC | Hit rate | Top-minus-bottom spread | Positive spread rate | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `train_ic_equal` | +0.0645% | +0.0002 | 47.62% | +2.20% | 58.73% | FAIL ? hit rate |
| `train_ic_proportional` | ?1.1314% | +0.0370 | 33.33% | +3.05% | 74.60% | FAIL ? return and hit rate |
| `train_ic_component_balanced` | ?0.9623% | +0.0085 | 38.10% | +1.66% | 61.90% | FAIL ? return and hit rate |
| Fundamental plus mean reversion, equal | ?3.7653% | ?0.0395 | 25.40% | ?1.88% | 41.27% | FAIL |
| Fundamental plus mean reversion, bounded | ?3.7658% | ?0.0415 | 25.40% | ?2.05% | 41.27% | FAIL |

The proportional score does distinguish higher- from lower-ranked names, but the
entire top sleeve still underperforms IYT. The equal-weight score has a slightly
positive top-sleeve return, but its IC is economically indistinguishable from
zero and its hit rate misses the 50% gate. Neither is a complete investable
model.

## Governance and acceptance gates

The following remain mandatory:

1. Cohort membership must remain outcome-blind and contain at least 20 active
   names.
2. Historical evaluation must remain point-in-time and survivorship-corrected.
3. Metric candidates must be derived from train evidence only and frozen before
   candidate selection.
4. A candidate must pass positive IC, positive net excess, at least 50% hit rate,
   drawdown, turnover, coverage, and minimum-history gates.
5. At least 50% of expanding walk-forward blocks must pass positive-return and
   nonnegative-IC gates.
6. Validation and holdout used during this redesign are research-contaminated
   and cannot authorize promotion.
7. The promoter, production lock, activation, and portfolio adapter must continue
   to fail closed until genuinely untouched evidence passes.

## Correct next evidence sequence

1. Freeze this cohort and candidate-generation contract as a shadow research
   specification. Do not change it in response to future observed outcomes.
2. Materialize daily outcome-blind scores for signals after the 2026-07-30 data
   cutoff.
3. Wait for complete 63-session forward outcomes for those new signals. A
   shorter 21-session diagnostic may be monitored but cannot replace the
   promotion horizon.
4. Evaluate the frozen specification once on the new untouched window.
5. Promote only if the complete validation, holdout-equivalent, walk-forward,
   readiness, production-lock, and portfolio-layer gates pass.

Selecting permanent universe members from already-revealed winners, relaxing the
hit-rate gate, or repeatedly changing weights against the same validation and
holdout periods would create an overfit pass and is explicitly prohibited.
