# Biotech Calibration, Validation, Promotion, and Portfolio Mandate

Status: Implemented cohort framework v3; production activation requires fresh evidence and explicit approval
Document version: 1.3
Decision date: 2026-08-28
Implementation date: 2026-09-01
Applies to: `biotech_index` research, calibration, production scoring, reporting, and `portfolio_layer` integration

## 1. Purpose

This document is the implementation contract for evaluating, calibrating, promoting, and operating the biotech ranking model.

The framework must accomplish all of the following:

1. Preserve a mandatory biotech allocation in the portfolio.
2. Keep hard investability and data-quality controls intact.
3. Determine the number of actively selected names from validated score reliability instead of a permanently fixed Top N.
4. Evaluate challengers relative to the incumbent production policy and XBI.
5. Use genuinely unseen, purged walk-forward results as the primary promotion evidence.
6. Use profit factor and tail-risk metrics together with lower confidence bounds (LCBs).
7. Prevent validation/test reuse, multiple-testing leakage, survivorship bias, and point-in-time leakage.
8. Keep historical strict-OOS records immutable after a production policy is locked.

This document supersedes the single chronological 70/30 train/test interpretation as the final promotion methodology. The framework is implemented by scripts 60, 61, and 64 and the shared modules listed in Section 15. A production policy changes only after a fresh run authorizes it and an explicit, effective-dated activation contract is hash-pinned in config.

## 2. Core Decisions

### 2.1 Biotech is a mandatory portfolio sleeve

The portfolio must not eliminate biotech solely because no active stock-selection policy has a positive absolute LCB.

The system must separate:

- Sector allocation: the biotech sleeve risk/capital budget assigned by `portfolio_layer`.
- Ticker investability: whether a security is legally, operationally, financially, and data-wise eligible.
- Active selection: whether an eligible security has sufficient calibrated expected alpha and score reliability.
- Residual sleeve exposure: benchmark exposure used when active evidence is insufficient.

If active selection cannot fill the sleeve, the unfilled amount must be allocated to XBI or another explicitly approved biotech benchmark. It must not be filled by overriding hard ticker vetoes.

### 2.2 Active name count is adaptive

The number of individual holdings must not be permanently fixed at 6, 8, 10, or 20.

The live count must be the number of hard-eligible names that clear a frozen, OOS-validated score-reliability threshold, subject to:

- a configured maximum number of active names;
- per-name and per-cohort concentration limits;
- liquidity and capacity limits;
- marginal expected-alpha requirements; and
- the biotech sleeve's active-risk budget.

A fixed Top N remains a calibration diagnostic and a maximum-capacity control. It is not the sole production selection rule.

### 2.3 Promotion is relative, not status-quo biased

The incumbent must not be retained merely because every challenger fails an absolute return hurdle that the incumbent also fails.

The primary promotion question is:

> Does the challenger improve the biotech sleeve relative to the production policy on the same OOS dates, tickers, costs, and constraints?

Positive absolute performance remains desirable and is reported, but a negative absolute LCB is not an automatic rejection when biotech exposure is mandatory.

### 2.4 Outer test data is never optimization data

Training estimates parameters. Validation selects policies and thresholds. The outer test evaluates a frozen decision.

Any period inspected to choose a model, policy, threshold, factor, blend, Top N, or Optuna search space is validation data from that point forward. It must not be labeled untouched test data.

The existing repeatedly inspected 2024-2026 holdout must be treated as validation/research evidence, not as a new untouched final test.

### 2.5 Calibration and promotion are independent by cohort

The five official biotech cohorts are separate calibration and promotion sleeves. A strong or weak result in one cohort cannot select, suppress, or promote a policy in another cohort.

| Component | Required scope |
|---|---|
| Fundamental factor weights | Cohort-specific |
| Score threshold | Cohort-specific |
| Number of selected names | Cohort-specific and adaptive |
| Reliability/exposure weight | Cohort-specific |
| Challenger selection | Cohort-specific |
| Promotion decision | Cohort-specific |
| Structural data-quality rules | Global rules are permitted |
| Final portfolio risk controls | Global, after all cohort sleeves are combined |

For every fold, each cohort receives the same frozen calendar boundaries but independently fits its candidate weights, reliability curve, threshold, maximum breadth, and active/XBI allocation. A cohort with no qualifying challenger retains its production incumbent; it does not fall back to another cohort's winner and does not force the entire biotech sleeve into XBI.

Only challengers passing that cohort's statistical, profitability, tail-risk, breadth, and live-portability gates may enter Optuna or promotion review. Optuna is run independently only for those surviving cohorts. The five resulting sleeves are combined exactly once for net-of-cost profitability, covariance, concentration, and other final portfolio risk controls.

The live contract contains all five cohort records but activates only independently authorized cohorts. Non-authorized cohorts continue using the incumbent production formula and gate. An activated cohort applies its own formula, threshold, adaptive maximum names, and reliability weight only to tickers assigned to that cohort.

## 3. Required Data Contract

### 3.1 Historical panel

Calibration must use the existing point-in-time, survivorship-corrected historical panel beginning on 2019-01-04, including delisted companies only while they were alive.

Required properties:

- Universe membership is determined as of each snapshot date.
- All features use information known by the snapshot date.
- SEC facts use filing/acceptance availability, not later restatements.
- Form 4 uses issuer CIK and filing date.
- 13F uses manager filing availability and historical CUSIP mapping.
- CTGov and FDA events use observed/announced dates.
- Prices use the correct point-in-time security identity and source.
- Delisted terminal events use the approved recovery contract.
- Current-universe replay rows cannot enter the survivorship-corrected calibration panel.

### 3.2 Existing eligibility concepts remain separate

- `portfolio_candidate_gate`: live stock-level production eligibility.
- `calibration_eligible_flag`: broad research-calibration eligibility.
- `stage11_calibration_input_eligible_flag`: strict Stage 11 input eligibility.
- `survivorship_corrected_panel_flag`: confirms PIT universe/dead-name support.
- `oos_score_valid_flag`: confirms the score was genuinely produced under the production policy effective on that date.
- `calibration_sample_role`: `pre_lock_research`, `strict_oos`, or `excluded`.

Research rows may be calibration-eligible even when they were not live portfolio candidates. They must not be misrepresented as strict production OOS observations.

### 3.3 Score preservation

Hard or soft portfolio exclusions must not overwrite the raw/native score with zero.

The system must preserve:

- native score value;
- production rank score before allocation exclusion;
- allocation eligibility;
- allocation/exclusion reason;
- structural veto state; and
- policy-adjusted selection state.

This prevents a portfolio decision from corrupting score IC and calibration analysis.

## 4. Evaluation Architecture

### 4.1 Nested purged expanding walk-forward design

Each return horizon must be evaluated independently. For each outer fold:

1. Use all eligible history before the fold's validation period as training data.
2. Require at least three years of training history.
3. Apply the horizon-specific purge/embargo.
4. Calibrate weights and candidate structures using training data only.
5. Select policies, thresholds, blends, and Optuna parameters using validation data only.
6. Freeze the complete candidate policy contract.
7. Apply the horizon-specific purge/embargo before the outer test.
8. Evaluate the frozen policy on the untouched outer-test window.
9. Store the fold result before proceeding to the next fold.
10. Expand the training history and repeat chronologically.

Outer-test windows should not overlap by default. If overlapping folds are enabled for diagnostics, they must be labeled as correlated evidence and must use date-blocked inference.

### 4.2 Default windows

| Forward-return horizon | Validation window | Outer-test window | Minimum calendar embargo | Default fold step |
|---|---:|---:|---:|---:|
| 20 trading bars | 6 months | 6 months | 40 days | 6 months |
| 60 trading bars | 12 months | 12 months | 100 days | 12 months |
| 120 trading bars | 18 months | 18 months | 185 days | 18 months |

The implementation must calculate the minimum calendar embargo from the forward horizon and trading calendar. Configured values may be larger but never smaller than the calculated minimum.

### 4.3 Completion-date discipline

Fold membership must be based on when the forward-return target is complete, not merely the signal date.

For every observation:

- signal date must fall within the designated signal window;
- entry must use the configured next-bar convention;
- exit/target completion must not cross a forbidden fold boundary;
- benchmark and ticker targets must use matching entry/exit conventions; and
- no target may be formed from unavailable future prices.

### 4.4 Recent monitoring windows

Trailing 30-, 60-, and 90-day diagnostics are required for regime monitoring, but they are not sufficient promotion evidence.

They may be used to:

- identify recent degradation or improvement;
- adjust a pre-approved active-risk blend within fixed bounds;
- trigger review; and
- detect data or score drift.

They must not be used to select a permanent policy or repeatedly retune thresholds.

## 5. Candidate Research and Calibration

### 5.1 Pre-registration

Candidate structures must be declared before an outer test is opened. The declaration must include:

- candidate name and immutable ID;
- factor set;
- weight bounds;
- cohort routing;
- hard and soft veto behavior;
- ranking and selection order;
- threshold family;
- Top N/max-name bounds;
- benchmark;
- cost assumptions;
- return horizons; and
- optimization objective and constraints.

Changing any of these creates a new candidate ID and invalidates resume caches for that candidate.

### 5.2 Initial candidate families

The first implementation must compare at least:

1. Current production scoring and selection policy.
2. Current best challenger.
3. Production/challenger score or allocation blends.
4. Validated cohort-specific policies.
5. Conditional borrow/short-interest overlays.
6. Catalyst variants.
7. Institutional-crowding variants.
8. XBI benchmark exposure.
9. Same-cohort equal-weight benchmark portfolios.

The search space must not add unvalidated factors simply to increase the chance of finding a backtest winner.

### 5.3 IC and monotonicity authorization

Factors entering a production candidate must first be classified by the feature monitor:

- `promote_candidate`;
- `cohort_specific_only`;
- `diagnostic_only`;
- `invert_or_redesign`; or
- `insufficient_data`.

Only the first two classes may enter production candidate optimization. Cohort-specific factors may operate only in authorized cohorts. Diagnostic and insufficient-data factors remain report-only.

### 5.4 Benchmarks and return bases

Every candidate must be evaluated on:

- absolute net return;
- XBI-relative net alpha;
- incumbent-relative paired net return; and
- same-cohort equal-weight relative return where applicable.

The primary allocation objective is net XBI-relative and incumbent-relative performance. Absolute returns remain a risk and investor-outcome diagnostic.

### 5.5 Costs and execution

All calibration and OOS returns must include:

- next-bar entry;
- configured round-trip transaction costs;
- liquidity eligibility as of the signal date;
- turnover generated by policy changes;
- corporate-action handling; and
- realistic missing-price behavior.

Capacity and slippage sensitivity must be reported before promotion even if they are not fully included in the first optimization objective.

## 6. Score Reliability and Adaptive Selection

### 6.1 Reliability target

Calibration must estimate, by score/rank and where support permits by cohort:

```text
score or rank percentile
    -> expected net XBI-relative return
    -> expected incumbent-relative return
    -> return confidence interval / LCB
    -> profit factor
    -> loss20 and loss40 probability
    -> observation and date support
```

Raw scores must not be treated as calibrated probabilities or expected returns.

### 6.2 Cross-fitted calibration

Score-to-return calibration must be fitted only inside training/validation data using one or more of:

- cross-fitted score buckets;
- monotonic/isotonic regression;
- regularized monotonic splines; or
- hierarchical cohort shrinkage toward the all-biotech relationship.

Cohort-specific thresholds require sufficient dates, tickers, wins, and losses. When support is insufficient, the cohort estimate must shrink to or fall back to the global biotech threshold.

### 6.3 Threshold candidates

Validation must compare:

- raw-score thresholds;
- within-cohort rank percentiles;
- global rank percentiles;
- Top 3/5/10/15/20 portfolios;
- marginal inclusion of the next-ranked name;
- minimum calibrated-alpha LCB;
- minimum score-confidence/data-quality level; and
- active-risk blends with XBI.

Thresholds are selected on validation and evaluated unchanged on the outer test.

### 6.4 Live adaptive selection algorithm

For each live as-of date:

1. Apply hard ticker eligibility.
2. Calculate the current production score without zeroing excluded rows.
3. Resolve the ticker's authorized cohort calibrator.
4. Map score/rank to calibrated expected alpha, LCB, and reliability.
5. Keep names that clear the frozen threshold.
6. Rank survivors by calibrated expected net alpha, using production score as the documented tie-breaker.
7. Apply per-name, per-cohort, liquidity, and total-name caps.
8. Stop adding names when marginal calibrated expected alpha fails the inclusion threshold.
9. Allocate the active stock-selection budget across selected names.
10. Allocate the residual biotech sleeve to XBI.

Hard structural vetoes must never be relaxed to meet a minimum number of stocks.

### 6.5 Active share of the biotech sleeve

The fraction of the sleeve allocated to active names must also be reliability-dependent:

| Aggregate reliability | Active stock-selection range | XBI residual range |
|---|---:|---:|
| High | 80%-100% | 0%-20% |
| Medium | 40%-70% | 30%-60% |
| Low | 0%-30% | 70%-100% |

Exact breakpoints must be selected in validation and frozen in the promoted policy contract. These ranges define candidate bounds, not automatically approved production values.

## 7. Performance Metrics

### 7.1 Co-primary promotion metrics

Promotion must use two co-primary metrics:

1. Paired OOS incremental return LCB versus production.
2. OOS profit factor, both absolute and relative to production.

LCB alone is insufficient because it does not describe the balance of total gains and losses. Profit factor alone is insufficient because it can be dominated by a few biotech takeouts or multibaggers.

### 7.2 Required return metrics

For train, validation, every outer fold, aggregate OOS, cohort, and regime, report:

- arithmetic mean and median return;
- XBI-relative mean and median return;
- incumbent-relative paired mean and median return;
- LCB and upper confidence bound;
- hit rate;
- profit factor;
- Sortino-like ratio;
- Omega when configured;
- Spearman rank IC;
- score-bucket monotonicity;
- top-minus-bottom quintile spread; and
- active-date coverage.

### 7.3 Tail and concentration metrics

Report and constrain:

- 20% loss rate;
- 40% loss rate;
- worst-decile return;
- configured CVaR/expected shortfall;
- maximum drawdown of the portfolio replay;
- largest winner contribution;
- top-three winner contribution;
- ticker concentration;
- cohort concentration; and
- effective number of independent names/dates where measurable.

### 7.4 Robust profit factor

Biotech profit factor must be reported as:

- raw PF;
- capped/winsorized-return PF;
- PF excluding the largest winner;
- PF excluding the three largest winners;
- cohort-level PF; and
- PF with explicit win/loss counts.

PF is invalid for promotion when the configured minimum number of losing observations is not met. Infinite or capped PF from an all-win small sample cannot authorize promotion.

### 7.5 Operational metrics

Report:

- average and minimum selected names per active date;
- percentage of dates with active stock selection;
- XBI residual weight;
- turnover;
- estimated transaction costs;
- liquidity/capacity usage;
- missing-score and missing-price rates; and
- policy fallback frequency.

## 8. Promotion Governance

### 8.1 Evidence hierarchy

Evidence has the following order:

1. Aggregate untouched outer-test performance.
2. Consistency across outer folds, cohorts, and regimes.
3. Validation performance used for selection.
4. Training performance as a sanity and mechanism check.
5. Recent 30/60/90-day live diagnostics.

Training performance must not outweigh genuinely untouched OOS performance. Outer-test data, however, cannot be used to change the candidate being evaluated in that fold.

### 8.2 Full-promotion standard

A challenger qualifies for full promotion only when all of the following hold:

1. PIT, survivorship, identity, price, and execution QA pass.
2. Aggregate paired OOS challenger-minus-production LCB is greater than zero.
3. Aggregate OOS PF is better than production PF.
4. Aggregate OOS PF is preferably at least 1.0.
5. The challenger beats production in at least 60% of eligible outer folds.
6. Loss20, loss40, CVaR, and drawdown do not deteriorate beyond configured tolerances.
7. Improvement survives largest-winner and top-three-winner removal tests.
8. Candidate breadth, active-date coverage, liquidity, capacity, and turnover are operationally acceptable.
9. No economically important cohort suffers material no-harm failure without an explicit cohort exclusion/routing rule.
10. The candidate was frozen before each outer test and was not selected from outer-test results.

The exact no-harm tolerances must be configuration values and must be documented in the promotion artifact.

### 8.3 Provisional/blended promotion

A challenger may receive provisional promotion when it materially improves recent and aggregate OOS results but has incomplete historical stability.

Requirements:

- paired OOS LCB improves;
- PF improves materially;
- tail risk is controlled;
- evidence is not winner-concentrated; and
- full-promotion failures are stability/sample-size issues rather than leakage, data, or severe-loss failures.

The candidate must then be deployed as:

- a bounded production/challenger blend;
- a cohort-specific policy; or
- a capped portion of the biotech active-risk budget.

The policy must include predeclared 60- and 90-day review triggers and rollback conditions.

### 8.4 No qualifying active policy

If no challenger or incumbent demonstrates reliable active alpha:

- biotech remains allocated;
- XBI becomes the dominant sleeve exposure;
- only names clearing the reliability threshold receive active positions;
- hard vetoes remain enforced; and
- research continues without representing weak evidence as validated alpha.

This is not a biotech allocation gate. It is a reduction in active stock-selection risk.

### 8.5 Incumbent treatment

The incumbent has no automatic grandfathering right. If it materially underperforms a robust challenger, it can be replaced even when both absolute LCBs are negative, provided the relative promotion and no-harm requirements pass.

If neither incumbent nor challenger is reliable, active exposure is reduced in favor of XBI rather than forcing the worse policy.

## 9. Optuna Contract

### 9.1 Scope

Optuna may tune only:

- factors authorized by IC/monotonicity validation;
- candidate structures that survived initial calibration;
- bounded weights and thresholds with economic justification; and
- policy parameters declared before the outer test.

Optuna must not discover unrestricted formula structure from scratch.

### 9.2 Nested operation

For each outer fold:

1. Build the fold using deterministic dates and embargoes.
2. Give Optuna training and validation data only.
3. Optimize the validation objective subject to hard constraints.
4. Freeze the winning trial and policy hash.
5. Close the Optuna study for that fold.
6. Evaluate the winner once on the outer test.

Outer-test metrics must not be visible to the sampler, pruning logic, resume logic, or candidate selection code.

### 9.3 Objective

The default Optuna objective should maximize paired validation improvement versus production, led by incremental LCB, while constraining:

- PF;
- loss20/loss40;
- CVaR;
- top-three gain contribution;
- turnover/cost;
- date coverage;
- average selected names;
- cohort no-harm; and
- liquidity/capacity.

Metric caps must prevent infinite PF, one-winner portfolios, or tiny samples from dominating the objective.

### 9.4 Cache and reproducibility

Resume caches must include hashes of:

- code version;
- config;
- data snapshot contract;
- candidate definition;
- fold definition;
- feature authorization manifest;
- cost assumptions; and
- benchmark/return objective.

Any mismatch invalidates the cache. Seeds, sampler settings, trial state, and package versions must be recorded.

## 10. Production Deployment

### 10.1 Policy lock

A promoted policy must have:

- immutable policy ID and version;
- effective trading date;
- scoring contract version;
- cohort routes;
- reliability thresholds;
- blend/active-risk bounds;
- max active names and concentration rules;
- benchmark fallback;
- evidence artifact paths and hashes;
- validation end date;
- outer-test evidence summary; and
- rollback triggers.

The first trading day after code/config lock is the new strict-OOS start date.

### 10.2 Historical immutability

Do not rewrite prior strict-OOS files to pretend the new policy was live historically.

- Existing strict-OOS production files retain their original model/policy version.
- Research recomputations are written to a separate modeling output root.
- Pre-lock historical scores may be recomputed for research only and labeled `pre_lock_research`.
- A promoted policy applies to production outputs only from its effective date forward.

### 10.3 Live output behavior

The production `biotech_daily_scores.csv` must continue to expose the existing standardized contract. At minimum:

- raw/native score remains populated;
- `portfolio_candidate_gate` reflects hard eligibility plus the promoted active-selection contract;
- `portfolio_candidate_reason` states the final decision reason;
- excluded-but-scored rows retain their native score; and
- score/model/policy versions identify the effective production contract.

Detailed reliability curves and threshold diagnostics belong in calibration artifacts. The main daily CSV should not be expanded with redundant research-only columns unless `portfolio_layer` requires them.

### 10.4 Portfolio-layer integration

`portfolio_layer` must receive:

- selected active biotech names and scores;
- the active stock-selection portion of the biotech sleeve;
- the residual XBI portion;
- per-name capacity constraints; and
- the effective biotech policy version.

The allocations must sum to the biotech sleeve budget. A stock-selection shortfall must increase XBI residual exposure, not create an unintended sector underweight.

## 11. Monitoring and Review

### 11.1 Daily

- Data freshness and provider completion.
- PIT provenance and score validity.
- Hard-veto and eligibility consistency.
- Selected-name count and XBI residual.
- Liquidity/capacity and missing data.
- Policy/model version consistency across biotech reports and `portfolio_layer`.

### 11.2 Weekly

- Score and rank drift.
- Cohort and ticker concentration.
- Turnover and transaction-cost estimates.
- Selection-threshold proximity.
- Factor/feature coverage drift.
- Form 4 reconciliation and source QA.

### 11.3 Trailing 30/60/90 days

- Paired live performance versus production predecessor and XBI.
- PF and robust PF variants.
- LCB/mean/hit-rate diagnostics with small-sample warnings.
- Loss rates, drawdown, and CVaR.
- Breadth and winner concentration.
- Regime and cohort attribution.

These windows may trigger review or a pre-approved blend adjustment. They do not independently authorize a new permanent policy.

### 11.4 Scheduled calibration

- Quarterly: rerun walk-forward candidate calibration and reliability monitoring.
- Annually: full methodology, factor, cohort, cost, and capacity review.
- Event-driven: rerun after material score-formula, feature-definition, universe, survivorship, price, or corporate-action corrections.

Routine daily data refreshes do not require full recalibration.

## 12. Required Artifacts

Each complete calibration run must write a unique, immutable output directory containing at least:

- `walk_forward_run_manifest.json`
- `walk_forward_fold_manifest.csv`
- `walk_forward_candidate_metrics.csv`
- `walk_forward_paired_policy_comparisons.csv`
- `walk_forward_cohort_metrics.csv`
- `walk_forward_regime_metrics.csv`
- `walk_forward_profit_factor_robustness.csv`
- `walk_forward_tail_risk_metrics.csv`
- `score_reliability_curves.csv`
- `score_reliability_thresholds.csv`
- `adaptive_selection_replay.csv`
- `adaptive_sleeve_allocation_replay.csv`
- `optuna_fold_trials.csv` when Optuna runs
- `promotion_decision.json`
- `promotion_decision.md`
- `production_policy_contract_candidate.json`

Every artifact must carry or reference code, config, data, candidate, fold, and policy hashes.

## 13. Implementation Map

### 13.1 Calibration core

Create reusable modules rather than duplicating split and metric logic across scripts:

- `biotech_index/core/calibration_splits.py`
  - deterministic nested walk-forward folds;
  - completion-date boundaries;
  - horizon-specific embargoes;
  - leakage assertions.
- `biotech_index/core/calibration_metrics.py`
  - paired LCB/bootstrap metrics;
  - raw and robust PF;
  - tail, concentration, breadth, cost, and stability metrics.
- `biotech_index/core/score_reliability.py`
  - cross-fitted score buckets;
  - monotonic calibration;
  - hierarchical cohort fallback;
  - frozen threshold serialization.
- `biotech_index/core/promotion_policy.py`
  - relative full/provisional promotion decisions;
  - no-harm constraints;
  - decision reasons and policy contract generation.

### 13.2 Scripts

Recommended script ownership:

- Extend/refactor `28_calibrate_biotech_opportunity.py` to consume the shared fold and metric modules.
- Update `46_optuna_biotech_candidate_optimizer.py` for nested fold isolation.
- Update `47_analyze_biotech_policy_failure_modes.py` to compare paired incumbent/challenger OOS results.
- Update `58_diagnose_biotech_oos_calibration.py` so negative absolute LCB does not automatically block a relatively superior policy.
- Extend `59_calibrate_biotech_cohort_regime_edges.py` to emit authorized cohort threshold candidates.
- Add a walk-forward orchestration script after the shared modules exist.
- Update `11_score_biotech_index.py` to apply the frozen reliability/selection policy.
- Update `12_publish_biotech_reports.py` to publish final adaptive eligibility and policy evidence.
- Update `24_run_biotech_refresh_pipeline.py` to validate the promoted policy contract and portfolio-layer handoff.

Exact new script numbers must be chosen after checking for concurrent additions. Do not renumber existing scripts.

### 13.3 Configuration

Add a new versioned configuration block rather than silently changing `calibration.tier1.train_fraction` semantics. The target block must include:

- minimum training years;
- per-horizon validation/test/step windows;
- calculated and configured embargoes;
- benchmark and cost contract;
- paired metric settings;
- PF minimum win/loss counts and caps;
- fold-consistency requirement;
- no-harm tolerances;
- reliability calibration method and support requirements;
- active-name maximums;
- active/XBI allocation bounds;
- provisional-promotion rules; and
- live monitoring/rollback triggers.

The current 70/30 split remains available only as a legacy diagnostic after migration.

## 14. Test Requirements

### 14.1 Unit tests

- Fold dates are deterministic.
- Train, validation, purge, and outer-test sets are disjoint.
- Return completion cannot cross fold boundaries.
- Embargo is never below the horizon-derived minimum.
- Optuna receives no outer-test rows or metrics.
- A changed candidate/config/data hash invalidates caches.
- Legitimate zero scores are not treated as missing.
- Exclusions do not overwrite native scores.
- PF requires configured win/loss support.
- Robust PF removes the correct winner contributions.
- Paired metrics use identical dates/observations.
- Cohort calibrators fall back safely when support is insufficient.
- Hard vetoes cannot be relaxed by adaptive breadth logic.
- Active stock plus XBI residual equals the biotech sleeve.

### 14.2 Integration tests

- Historical panel uses PIT and survivorship-correct rows.
- Delisted aliases and reused tickers use the correct price series.
- Walk-forward outputs reproduce with the same seed and hashes.
- A deliberately leaky feature causes the QA gate to fail.
- A test-selected candidate cannot be promoted as untouched OOS.
- A relatively superior challenger can pass when both absolute LCBs are negative, provided no-harm requirements pass.
- A one-winner PF spike cannot pass robust promotion checks.
- Insufficient active names increase XBI residual rather than weakening hard vetoes.
- Production and `portfolio_layer` outputs use the same policy version and selected names.

### 14.3 Static and repository checks

After Python changes:

- run Ruff on every changed Python file;
- run Pyright on every changed Python file/module scope;
- run the biotech regression suite;
- run `git diff --check`; and
- run a deterministic smoke calibration with at least two folds.

## 15. Implementation Map and Execution Order

Implemented components:

- `core/calibration_splits.py`: purged expanding walk-forward folds and target-completion leakage checks.
- `core/calibration_metrics.py`: paired LCB, absolute/relative PF, robust PF, tail, concentration, and bootstrap metrics.
- `core/score_reliability.py`: score-reliability thresholds, adaptive name count, and active/XBI sleeve accounting.
- `core/promotion_policy.py`: relative full/provisional promotion, co-primary LCB/PF gates, cohort/horizon no-harm, and fail-closed deployment readiness.
- `core/cohort_calibration.py` and `core/cohort_portfolio.py`: official cohort scope, independent decisions, fold alignment, and one post-cohort portfolio combination.
- `core/promotion_contract.py`: immutable hash/effective-date validation, global-v1 compatibility, cohort-v2 formula validation, and monitoring/rollback validation.
- `scripts/60_run_biotech_walk_forward_calibration.py`: one isolated cohort calibration, validation-only Optuna, untouched outer-test evaluation, and frozen fold contract.
- `scripts/64_run_biotech_cohort_walk_forward_calibration.py`: five independent cohort runs, survivor-only Optuna, contract aggregation, and one final global profitability/risk replay.
- `scripts/61_activate_biotech_promotion_contract.py`: explicit non-retroactive global-v1 or cohort-v2 activation and activation receipt.
- `scripts/11_score_biotech_index.py` and `scripts/12_publish_biotech_reports.py`: effective-dated cohort formulas, adaptive selection fields, and report persistence.
- `portfolio_layer`: canonical cohort reliability fields plus per-cohort active/XBI transformations before global optimizer controls.

Unsupported research selection policies remain non-activatable until their exact live scorer implementation has parity tests. A successful statistical backtest alone cannot bypass this deployment gate.

Operational sequence:

1. Freeze and hash the incumbent production policy, current cohort map, and effective-dated cohort migration file.
2. Run static checks and regression tests before any expensive recomputation.
3. Rebuild the daily PIT/survivorship-correct score panel once, using effective-dated cohorts and excluding removed names after their valid membership end dates.
4. Regenerate every dated `biotech_daily_scores.csv` required by `portfolio_layer`, then pass historical panel QA.
5. Build a fresh observation/forward-return cache with no resume reuse; its signature must include score config plus both cohort files.
6. Run deterministic nested walk-forward calibration independently for each of the five cohorts. Each cohort fits its own weights, reliability threshold, adaptive breadth, name cap, active/XBI exposure, challenger, and promotion decision.
7. Run Optuna only within deterministic cohort survivors, using train/validation data only; never expose outer-test rows to search.
8. Retain the production incumbent independently in every non-surviving cohort. Do not substitute another cohort's winner and do not remove the biotech mandate.
9. Combine the five frozen cohort sleeves once and apply net-of-cost profitability, covariance, concentration, and global portfolio risk controls.
10. Produce per-cohort and combined decision artifacts. Activate only independently authorized cohorts through an explicit effective-dated, hash-pinned contract.
11. Run the daily biotech pipeline and `portfolio_layer` consistency checks against the activated contract.
12. Set the first post-lock trading date as strict OOS and begin 30/60/90-day monitoring without rewriting prior strict-OOS records.

Historical daily CSVs do not need regeneration merely to implement the calibration harness. Regenerate research scores only when required to evaluate a changed score formula or feature definition. Never overwrite historical strict-OOS production records with a later policy.

## 16. Acceptance Criteria

Implementation is complete only when:

1. Nested walk-forward folds pass leakage QA for every horizon.
2. The existing production policy is represented in every fold.
3. Candidate selection and Optuna never access outer-test metrics.
4. Paired production/challenger LCB and PF comparisons are available.
5. Robust PF and winner-concentration diagnostics are available.
6. Score-reliability thresholds are fitted without outer-test access.
7. Adaptive name count and XBI residual replays are reproducible.
8. Hard ticker vetoes remain unchanged by the biotech mandate.
9. A formal decision artifact explains full promotion, provisional promotion, incumbent retention, or benchmark-dominant fallback.
10. A promoted policy has a version, effective date, evidence hashes, and rollback contract.
11. `biotech_daily_scores.csv` and `portfolio_layer` agree on active names and policy version.
12. The biotech sleeve remains fully allocated through active names plus benchmark residual.
13. Ruff, Pyright, regression tests, integration tests, and `git diff --check` pass.

## 17. Final Operating Principle

The system must not manufacture confidence, protect an inferior incumbent, or eliminate the biotech mandate.

It must allocate the biotech sleeve through the strongest evidence available:

- hard-eligible, reliably ranked stocks when active alpha is supported;
- a controlled incumbent/challenger blend when evidence is promising but incomplete; and
- XBI residual exposure when active evidence is weak.

All promotion decisions must be based primarily on frozen, paired, genuinely OOS evidence, with LCB and profit factor as co-primary metrics and tail risk, concentration, breadth, costs, and cohort stability as mandatory controls.
