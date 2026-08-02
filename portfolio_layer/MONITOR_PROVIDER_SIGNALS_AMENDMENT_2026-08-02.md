# Monitor Provider Signals Amendment - 2026-08-02

Status: active implementation contract. This amendment is additive to
`MONITOR_LEVELS_IMPLEMENTATION_PLAN.md` and does not authorize provider-derived
economic signals for production use.

## Decision

The normalized FMP and Alpha Vantage data are retained provider-by-provider.
They are never averaged. Data-quality and diagnostic features may be produced
immediately, but LES, action-state, optimizer, and price-level effects require
separate prospective evidence and an explicit promotion amendment.

## Implement Now

1. **Provider coverage and staleness readiness.** Measure fresh estimate
   coverage separately by provider and monitor tier. Tier 0/1 fresh coverage
   below 90% is a hard failure in the provider-diagnostics child artifact and
   must raise an operational alert; 90-95% is a warning. Because provider
   economics remain diagnostic-only, that child coverage failure degrades the
   daily parent to `PASS_WITH_DEFERRED` rather than blocking independent
   SEC/market/holdings monitoring. Tier 2 remains isolated and warning-only so
   unavailable lower-priority names do not suppress covered holdings or
   investable names. Missing provider rows never create economic values.
2. **Estimate-revision diagnostics.** Publish provider-separated 30/90-day
   changes for the nearest active annual and quarterly EPS/revenue periods.
   Locally observed PIT history takes precedence once it exists; provider
   embedded lookbacks are labeled fallbacks. Output is diagnostic-only.
3. **Uncertainty and confidence diagnostics.** Publish cross-provider
   dispersion, disagreement flags, analyst counts, and provider count without
   selecting or averaging a central estimate. No levels penalty is authorized.
4. **Earnings-date drift diagnostics.** Publish advanced/delayed/unchanged date
   changes from the existing PIT calendar. A delay has no assumed negative sign
   and creates no LES event until outcome evidence validates that use.
5. **Fiscal-period quality audit.** Surface exact-resolution coverage, eligible
   forecast/outcome links, and fiscal-period mismatch counts. Zero mature links
   is an explicit deferred state, not PASS evidence.

Artifacts are emitted by
`expectations_monitor/49a_build_provider_diagnostics.py` and sealed into the
script-50 daily monitor graph. Source code, config, calendar history, output
hashes, source-snapshot digest, and snapshot dependency lineage are recorded.

## Phase 2 - Build or Activate After Evidence Exists

1. **Cross-provider dispersion penalty in levels.** Activate only after estimate
   currency/definition semantics are verified and realized coverage calibrates
   the penalty. Disagreement remains an uncertainty input, not a directional
   forecast.
2. **Analyst-count/coverage confidence in the consensus anchor.** Activate with
   the consensus anchor only after prospective provider accuracy exists. Counts
   control confidence and size; they do not change the estimate itself.
3. **Adaptive capture scheduling.** Re-tier around earnings only after calendar
   freshness and provider-cycle reliability are measured. Static Tier 0/1 daily
   and Tier 2 weekly cadence remains the operational baseline.
4. **Guidance-versus-consensus severity.** Requires exact fiscal-period,
   currency, units, and definition alignment plus structured numeric guidance.
   Flat guidance impacts remain until those joins validate prospectively.
5. **Earnings calendar cross-provider confirmation.** The current entitlement
   surface has one calendar authority (Alpha bulk, with Yahoo/Gemini recovery),
   not an independent FMP calendar contract. Cross-validation waits for a
   second entitled calendar source.

## Phase 3 - Outcome-Calibrated Economic Signals

1. **Name-specific surprise priors and earnings gap buffers.** Requires linked
   realized forecast outcomes and sealed post-report price reactions across
   enough quarters. A forecast surprise and a market gap are distinct outcomes.
2. **Post-earnings drift LES windows.** Requires prospective tests of surprise
   plus relative-price confirmation, net of repeated-event overlap. No tailwind
   or deterioration points are assigned before those tests pass.
3. **Estimate-revision LES events.** Revision momentum may enter LES only after
   provider-separated, sector-aware OOS tests establish sign, horizon, decay,
   and false-positive behavior. Provider-embedded history cannot alone promote
   the signal.

## Promotion Rules

- Every economic use is promoted independently; diagnostic availability is not
  promotion evidence.
- Provider performance is evaluated separately by sector and metric.
- Missing data is an availability state, never positive or negative evidence.
- Promotion requires PIT-safe prospective outcomes, costs where applicable,
  code/config hashes, and a dated amendment.

## Deferred Implementation Register

Every item below is `PENDING`. The named target module is where the economic
behavior belongs; `49a_build_provider_diagnostics.py` remains the read-only
measurement layer and must not acquire portfolio behavior.

| Enhancement | Target code surface | Data/evidence still required | Activation test | Current blocker |
|---|---|---|---|---|
| Cross-provider dispersion penalty | `levels/60_build_valuation_inputs.py`, `levels/61_build_levels.py`, and `levels/62_validate_levels.py` | Provider/metric currency, units, definition, and fiscal-period parity; sealed realized outcomes and level residuals by sector | Prospective interval-coverage and error calibration must show that dispersion improves uncertainty estimates; the effect may widen bands or lower confidence only and may not select direction | Provider-separated diagnostics exist, but there is no matured linked-outcome history |
| Analyst-count/provider-coverage confidence | `levels/60_build_valuation_inputs.py` and `levels/62_validate_levels.py` | Stable analyst-count semantics by provider and prospective provider error by sector/metric | Counts must predict lower forecast error after sector/provider controls; missing counts must remain missing and may only reduce confidence or size | Counts are captured where supplied, but accuracy history is immature and not uniform across endpoints |
| Adaptive capture scheduling | `expectations_monitor/43_run_provider_capture_schedule.py` | Measured request use, cycle completion, calendar freshness, and Tier 0/1 coverage across normal and pre-earnings periods | Shadow schedule must preserve or improve Tier 0/1 freshness without quota overruns, duplicate captures, or starvation of Tier 2; deterministic priority and failover are required | Static daily Tier 0/1 and weekly Tier 2 policy is reliable; no shadow scheduling trial has run |
| Guidance-versus-consensus severity | `expectations_monitor/53_sync_authoritative_events.py`, `54_classify_monitor_events.py`, and `56_build_expectations_state.py` | Structured numeric guidance with exact fiscal period, currency, units, metric definition, and announcement availability time | Join-quality gates must pass and prospective sector-aware outcomes must validate severity bins before LES points change | Guidance events exist, but the normalized cross-sector numeric-guidance contract and validated joins do not |
| Independent earnings-calendar confirmation | `earnings_dates/`, `expectations_monitor/53_sync_authoritative_events.py`, and `54_classify_monitor_events.py` | A second entitled calendar source with PIT availability timestamps and finality/correction metadata | Agreement, disagreement, and date-change classifications must be reproducible; date changes remain unsigned until prospective outcomes validate an effect | The current calendar is not supported by two independent entitled sources |
| Name/sector surprise priors and earnings gap buffers | `expectations_monitor/58_update_monitor_outcomes.py`, `levels/61_build_levels.py`, and `levels/63_update_level_outcomes.py` | Multiple sealed earnings outcomes and post-report OHLCV reactions per name, with sector-hierarchical fallback for sparse names | A separately frozen specification must set minimum sample sizes and shrinkage; a final untouched confirmation set must improve calibrated gap coverage without increasing missed-risk events | Forecast/outcome links currently have no mature prospective sample |
| Post-earnings drift LES windows | `expectations_monitor/55_build_monitor_market_signals.py`, `56_build_expectations_state.py`, and `58_update_monitor_outcomes.py` | PIT surprise, report timing, benchmark-relative returns, overlap-safe event windows, and transaction-cost assumptions | Purged provider/sector tests with multiplicity control must validate sign, horizon, decay, and false-alert rate; repeated events cannot double count | Outcome history and independent event windows have not accrued |
| Estimate-revision LES events | `expectations_monitor/54_classify_monitor_events.py`, `56_build_expectations_state.py`, and `58_update_monitor_outcomes.py` | Locally observed provider-separated 30/90-day PIT revision history and resolved forward outcomes | Pre-registered provider x sector x metric x horizon tests must pass purged OOS, block-bootstrap/HAC, multiplicity, stability, and false-positive gates; provider-embedded lookbacks are excluded from promotion evidence | Revision diagnostics are available, but local PIT history started only with provider capture and has not matured |

## Pending Operational Work

1. Keep daily provider capture running so local PIT revision histories replace
   provider-embedded fallback history naturally. Historical provider values
   must never be assigned an earlier `available_at_utc` than their actual
   capture.
2. Keep scripts 45-48 running around every earnings event so exact fiscal-period
   links and realized outcomes accumulate. A zero-link state remains
   `DEFERRED_NO_LINKED_OUTCOMES`.
3. Review `provider_coverage_readiness.csv`, revision-source mix, disagreement
   rates, and fiscal-resolution status monthly by provider, sector, and tier.
4. Freeze a separate dated specification before implementing any row above.
   That specification must define the statistical family, sample floor,
   confirmation set, and rollback rule before results are inspected.
5. Preserve the current production behavior until promotion: provider
   diagnostics cannot alter `les_total`, `internal_state`, `action_state`,
   optimizer eligibility, position size, or price bands.
