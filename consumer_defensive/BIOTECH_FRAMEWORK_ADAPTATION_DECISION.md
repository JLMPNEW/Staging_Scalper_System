# Consumer Defensive Decision On The Biotech Calibration Framework

Decision date: 2026-08-26  
Status: approved design recommendation; Consumer production evidence remains pending

## Decision

Consumer Defensive should adopt the Biotech framework's leakage controls,
paired challenger evaluation, reliability calibration, robust profit-factor
tests, tail-risk controls, provisional cohort promotion, immutable policy locks,
and monitoring discipline.

It should not copy the Biotech framework wholesale. The Biotech document is an
approved target design whose implementation is still pending, not an already
validated production precedent. Consumer Defensive also has a different
allocation mandate, four independently calibrated cohorts, XLP rather than XBI,
and a burned retrospective holdout. Those differences require the adaptations
below.

## Component Decision Matrix

| Biotech component | Consumer decision | Consumer adaptation |
| --- | --- | --- |
| Nested purged expanding walk-forward | Adopt | Run independently by cohort on PIT membership; use 21/63/126-session completion-date purges and embargoes. |
| Outer test never used for candidate selection | Adopt | Outer folds are research evidence only; no candidate, threshold, or weight may change after its outer fold is observed. |
| Paired challenger versus incumbent | Adopt | Pair on identical ticker/date/cohort observations and compare against the frozen Stage 7 baseline and XLP. |
| LCB and profit factor as co-primary metrics | Adopt | Require paired incremental-return LCB, absolute and relative PF, minimum win/loss support, robust PF, and winner-concentration diagnostics. |
| Tail/no-harm controls | Adopt | Retain loss20/loss40, CVaR, drawdown, turnover, liquidity, concentration, and regime/cohort stability gates. |
| Cross-fitted score reliability | Adopt | Fit reliability curves inside training/validation folds and publish cohort-specific threshold candidates. |
| Adaptive name count | Adapt | Determine breadth separately inside each promoted cohort, subject to cohort cap, name cap, liquidity, and marginal-alpha floors. Do not use disclosure completeness as a selection bonus. |
| Cohort-specific provisional promotion | Adopt | A qualifying cohort can receive a small signed alpha/capital cap while other cohorts remain zero-weight shadow lanes. |
| Fold-local bounded Optuna | Defer, then adopt | Enable only after the deterministic candidate registry and fold splitter pass leakage tests; search only pre-authorized factors and bounded parameters. |
| Mandatory sector sleeve | Reject for Consumer | Consumer Defensive remains an optional portfolio sleeve. Weak absolute evidence keeps its capital allocation at zero. |
| Benchmark residual fills unused sleeve | Reject by default | Do not force an XLP residual merely because active Consumer names do not qualify. Portfolio Layer may hold XLP only under a separately approved strategic allocation policy. |
| Relative promotion when both policies have negative absolute evidence | Restrict | It may replace an already active incumbent if no-harm and rollback tests pass, but it cannot authorize Consumer's initial capital activation. Initial cohort activation still requires positive absolute evidence. |
| Retrospective outer-test result as production authority | Reject | Historical walk-forward evidence prioritizes research candidates. The canonical three-authority prospective protocol remains the capital-promotion authority. |

## Relationship To The Canonical Future-Only Protocol

The adapted walk-forward framework and the existing canonical v5 protocol solve
different problems:

1. Nested walk-forward research chooses and stress-tests a frozen cohort policy
   without repeatedly reusing one holdout.
2. Canonical future-only capture proves how that frozen policy behaves after an
   external timestamp, under independent market-data and evidence authorities.
3. Independent change control converts a passing cohort verdict into a bounded,
   effective-dated production lock.
4. Stage 12 and Portfolio Layer revalidate that lock and enforce its exact alpha
   and cap.

Historical replay cannot replace step 2 because the existing Consumer history
has already been used during development. Conversely, the prospective protocol
should not be used as an open-ended model-search loop.

## Consumer-Specific Target Architecture

The research implementation should be versioned separately from the canonical
v5 evidence contract and contain:

- deterministic PIT folds by cohort and 21/63/126-session horizon;
- completion-date purge and horizon-derived embargo;
- a preregistered incumbent, challengers, blends, and threshold grid;
- fold-local factor authorization and bounded optimization;
- paired incumbent/challenger/XLP return panels after identical costs;
- aggregate and fold-level LCB, PF, robust PF, tail, concentration, breadth,
  turnover, and coverage outputs;
- cross-fitted score-reliability curves and adaptive breadth candidates;
- a research verdict of reject, retain incumbent, nominate challenger, or
  nominate provisional blend for each cohort; and
- immutable hashes binding data, folds, candidates, code, configuration, and
  results.

No research verdict may directly edit Stage 7, Portfolio Layer configuration,
or a production activation registry.

## Recommended Implementation Order

1. Freeze and hash the current Stage 7 baseline and current research artifacts.
2. Implement deterministic cohort/horizon fold generation and leakage tests.
3. Implement paired return, LCB, PF, robust-PF, tail, and concentration metrics.
4. Represent the incumbent in every fold and preregister bounded challengers.
5. Implement cross-fitted reliability curves and adaptive cohort breadth.
6. Add fold-local bounded optimization only after steps 2-5 pass.
7. Publish aggregate outer-fold evidence and cohort research verdicts.
8. Keep the winning candidate frozen and continue canonical prospective capture.
9. After prospective maturity, require independent review and a signed bounded
   activation lock before any cohort alpha or optimizer cap becomes positive.

## Current Production Readiness

The Consumer implementation now has the necessary software boundaries for
selective promotion: cohort identity survives into Portfolio Layer, calibration
and percentiles are cohort-specific, per-cohort caps are enforced, Stage 12
publishes a separately validated operational file, historical sidecars remain
calibration-only, and deployment can be rehearsed against a SQLite backup.

Consumer Defensive is not yet capital-promoted. The checked-in alpha and cap for
every cohort remain zero. External trust roots, prospective registration,
matured outcomes, independent review, a signed activation registry, and a
successful production-backup rehearsal remain real evidence/operations gates.
