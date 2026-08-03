# MacroLayer Audit Remediation — 2026-08-02

## Outcome

The audit was directionally strong. The critical PIT, credential, ingestion, fail-open gate,
state-consistency, allocation, optimizer-integration, and shadow-backtest defects were legitimate
and have been remediated. The code now fails closed at the relevant promotion boundaries.

The existing raw database is **not production-promotable yet**. Hardened QA correctly detected
legacy fabricated/concentrated ALFRED vintages in six registry series. Those rows were created by
the prior realtime-window defect and require a clean source backfill after credential rotation.
They were not deleted without replacement data.

## Confirmed defects fixed

### Raw ingestion and credentials

- ALFRED realtime/vintage range is no longer truncated by incremental observation history.
- Non-vintage availability cannot predate retrieval when explicit release/vintage metadata is absent.
- Non-vintage revisions are append-only and unchanged fetches are idempotent.
- Literal API keys are rejected; FRED/EIA credentials must come from environment variables.
- Soft connector failures and failed QA now produce nonzero process exits.
- EIA zero values, missing totals, pagination, and repeated-page loops are handled correctly.
- Empty fetches preserve prior sync watermarks and cannot create a false first success.
- Multi-source metric policy conflicts fail closed instead of using last-wins behavior.
- OECD sibling series share one deterministic dataset window/cache request.
- ADS all-vintage schema drift and incomplete CFNAI requested windows fail closed.
- Retry-After sleeps are capped by configured retry policy.

### PIT, features, and serving

- Availability filtering and ordering use the same canonical calculation.
- Candidate selection is observation-period-first; late revisions of old periods cannot pin latest values.
- Calendar partial rebuilds preserve dates outside the requested range.
- Serving frequency/ref-area come from the authoritative populated registry row, not lexical MIN values.
- Downstream latest/serving selection is capped at the authoritative calendar end.
- Optional shadow failures do not strand the mandatory serving DAG; strict mode remains available.
- Daily/weekly lags use elapsed time, and monthly/quarterly lags tolerate period-end encodings.
- Standardization replaces same-period revisions rather than counting revision events as new history.
- Feature daily rows preserve PIT carry-forward semantics.
- Composite policies that reference never-materialized features fail before publication.
- Composite validation is fail-closed and analyzes only covered observations by default.

### Probabilities, regimes, and promotion

- NOW probabilities use expanding prior-history mapping rather than same-month tautological fitting.
- Partial current-month labels remain unavailable until month-end.
- Regime initialization honors gates and confirmation; dead incumbents have a floor-breach escape.
- Next-three-month regime probabilities use a 63-step transition horizon.
- Path-dependent v1/v2 smoothing and decision state rebuild from full available history.
- Narrow v2 probability runs rebuild the canonical model-version history.
- Missing H1 baselines fail closed; baseline creation requires an explicit initialization mode and is non-promotable.
- V2/H1 seals include estimation engine, builder, config, and artifact hashes and verify them at promotion.
- Vintage-gap audit status is derived from evidence instead of hard-coded PASS.

### Stages 9–12 and portfolio integration

- Stage 9 context carry-forward is bounded by staleness tolerance and minimum member coverage.
- Stage 10 metadata/ref-area mismatches fail coverage; confidence uses actual finite local components.
- Stage 10 checks reject collapsed score/rank histories and vacuous external ranges.
- Stage 11 uses the configured sector map, safe index alignment, rowwise component renormalization,
  coverage-before-standardization, and no duplicated sector/shock contribution.
- A disabled tactical overlay is neutral even when enabled-mode missing policy is strict.
- Stage 12 allocations use exact feasible bounded normalization and jointly enforce item/group caps.
- Target bands cannot exceed configured caps; infeasible allocations fail rather than renormalizing over limits.
- Canonical optimizer universes cannot silently shrink or emit schema-varying fallbacks.
- Only accepted survivorship inputs are eligible.
- Stage 12D resolves the actual optimizer package/config, honors enabled flags, requires fresh outputs,
  and seals every case with run IDs plus input/output hashes.
- Acceptance, case summary, case manifests, and weights must all match one sealed run before candidate selection.
- IBKR is read-only and sensitive account output is opt-in/redacted.
- Shadow statistics use non-overlapping holding periods, actual elapsed annualization, explicit costs,
  cash-matched benchmarks, reproducible date cutoffs, and conservative invalid/missing exit treatment.

## Findings not treated as mechanical bugs

- Shrinking a negative country score toward zero under low confidence is statistically coherent uncertainty
  shrinkage; reversing that behavior would add unsupported conviction.
- Country-class and fallback penalties are investment-policy choices. They were not silently removed.
- SHOCK sign conventions, exact-date H1 outcome pairing, v2 overlap-adjusted inference, and latest-date
  coverage thresholds are model/governance choices requiring calibrated acceptance criteria.
- Research-family failures remain shadow-only by default and can be made strict explicitly.

## Enhancements disposition

Worth implementing now and completed: provenance seals, deterministic full-history state recomputation,
fail-closed policy/schema validation, feasible hierarchical allocation, realistic shadow costs/benchmarking,
and historical rather than latest-only validation where supportable.

Deferred until a clean PIT backfill and out-of-sample calibration exist: HMM regime replacement, Kalman/dynamic
factor models, surprise/expectations factors, empirically estimated macro/industry betas, Black-Litterman views,
block-bootstrap v2 promotion thresholds, and new shock-sign specifications. Implementing these before repairing
the source history would optimize against contaminated evidence.

## Verification

- Full MacroLayer bytecode compilation: PASS.
- Dedicated macro suites: 65 tests passed after final expansion.
- Complete `tests/portfolio_layer` suite: 110 tests passed.
- H1 promotion self-test: PASS.
- Ruff on all changed remediation Python files (repository import-order convention excluded): PASS.
- `git diff --check`: PASS.
- YAML parse and literal-credential check: PASS.
- Live raw QA run `843a4453ad144fb1803b6bdcca8bb3e4`: correctly FAILED with 6 errors and 8 warnings.

The six legacy error series are `us_anfci`, `us_nfci`, `us_initial_claims`,
`us_initial_claims_4w`, `us_industrial_production`, and `us_nonfarm_payrolls`.

## Required operational follow-up

1. Rotate the exposed FRED and EIA credentials because tracked-history removal does not revoke them.
2. Set `FRED_API_KEY` and `EIA_API_KEY` in the runtime environment.
3. Cleanly replace/backfill the six corrupted ALFRED series, then rerun raw QA until it passes.
4. Run the full serving pipeline and Stage 12D optimizer acceptance in the production data environment.
5. Do not promote v2/H1 or a Stage 12 candidate unless their sealed gates pass on that fresh run.