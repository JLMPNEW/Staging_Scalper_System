# Provider Estimate Reconciliation Policy

Policy version: `provider_reconciliation_v2`

## Non-negotiable rules

1. FMP and Alpha Vantage observations remain separate provider records.
2. Provider values are never averaged, blended, overwritten, or backfilled across providers.
3. A cross-provider comparison requires the same ticker, metric, canonical period, and fiscal-period end.
4. Currency mismatches are not comparable. Missing currency is disclosed as unverified.
5. For a comparable pair, the lower current estimate is recorded only as a conservative downside candidate.
   It is not the central forecast and cannot create a positive opportunity signal.
6. A single-source value is labeled single-source and produces no conservative candidate.
7. Revision fields remain provider-specific. Alpha revision fields are not imputed into FMP records.
8. All reconciliation outputs remain shadow-only and cannot alter scores, weights, levels, or broker actions.
9. The current selection excludes fiscal periods ending more than 90 days before the reconciliation date.
   Older rows remain retained as schema and outcome references, but a row fetched after its fiscal outcome
   cannot be treated as a historical point-in-time forecast.
10. A downside candidate is eligible only when both providers are quality-valid, currency is verified,
    each provider has at least two analysts, and provider fetch times are within six hours.
11. No central estimate is assigned until prospective provider-accuracy evidence satisfies this policy.

## Intended use

- FMP and Alpha observations remain parallel evidence channels.
- Alpha revision fields remain an early-warning channel; they are never copied into FMP.
- An eligible lower estimate may only escalate risk, suspend an addition, or widen a downside scenario in
  shadow mode. It cannot force a sale or replace the central valuation anchor.
- Positive opportunity or add decisions require independently supported central estimates and the existing
  portfolio gates. A conservative downside candidate cannot create upside evidence.

## Symmetric boundaries

Provider low/high fields are preserved as analyst-dispersion ranges, not treated as calibrated probability
intervals. When both providers supply complete valid ranges, the reconciliation records:

- the agreement range: `max(provider lows)` to `min(provider highs)`;
- the outer envelope: `min(provider lows)` to `max(provider highs)`;
- an explicit no-overlap disagreement state when the ranges do not intersect.

Neither range is actionable until currency, analyst breadth, fetch skew, and range-overlap gates pass. After
prospective outcomes accrue, rolling conformal residual quantiles at 10% and 90% replace native analyst ranges
as the calibrated uncertainty interval. Promotion requires at least 60 matured observations across six fiscal
quarters and observed interval coverage consistent with the 80% target.

The eventual monitor applies the boundaries symmetrically:

- buy/add evidence requires adequate upside from the calibrated lower valuation boundary and no deterioration;
- hold/watch applies when price and calibrated value ranges overlap or evidence conflicts;
- trim/sell evidence requires the calibrated upper valuation boundary to fall below price together with
  confirmed estimate or thesis deterioration.

The monitor publishes evidence and state; portfolio and broker gates remain authoritative for any action.

## Monthly review

The monthly reconciliation report groups disagreement by ticker, source pipeline, sector, metric, and fiscal
period. It records relative differences, the diagnostic downside candidate, and unverified currency. Large
differences are investigation candidates, not automatic trading signals.

Provider accuracy cannot be inferred from provider agreement. Only prospectively captured, first-write
snapshots may enter accuracy evaluation. Each provider is scored separately by sector, metric, and forecast
horizon using paired ticker-period observations. Revenue is evaluated first. EPS joins require an explicit
actual-definition contract so GAAP and adjusted EPS are not mixed.

Monthly reports may begin at 20 matured observations. Cross-company accuracy uses normalized absolute error
and signed scaled bias; raw MAE is diagnostic only because company scale would otherwise dominate the result.
A provider-sector-metric-horizon preference cannot change before 40 paired observations spanning at least
four fiscal quarters, a pre-registered paired 95% confidence test, and at least 5% relative normalized-error
improvement. Preference changes are reviewed quarterly, must remain in force for at least two quarters, and
use hysteresis rather than monthly switching. Until those gates pass, central estimate status remains
`unassigned_pending_prospective_accuracy`.

## Commands

```powershell
python portfolio_layer/expectations_monitor/41_validate_provider_estimate_semantics.py `
  --retrieval-cycle <cycle> --as-of <yyyy-mm-dd>

python portfolio_layer/expectations_monitor/42_reconcile_provider_estimates.py `
  --retrieval-cycle <cycle> --as-of <yyyy-mm-dd> --universe-as-of <portfolio-run-date>
```
