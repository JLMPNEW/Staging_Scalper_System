# Transportation share-source implementation

Status date: 2026-07-30

## Outcome

Transportation now uses the shared industrials share-snapshot table and resolver while keeping all transportation-specific conversion and filing decisions inside the transportation package.

Validated results:

- Current active universe: 112/112 shares outstanding and 112/112 public-float observations.
- Historical endpoint universe: 138/139 shares outstanding (99.28%) and 126/139 public-float observations (90.65%).
- The only historical endpoint outstanding-share miss is CGI. Celadon's price history is now in the survivorship contract, but no 2019 share value is fabricated from its last 2017 SEC fact.
- Point-in-time valuation-source readiness: 107/107 required operating issuers ready; zero blockers.

In this transportation universe, `CGI` means Celadon Group (CIK `0000865941`), the trucking issuer that entered Chapter 11 in 2019. It does not mean CGI Inc. and must never be mapped to `GIB`. Norgate `CGIP` prices are admitted only through the reviewed 2019-12-09 economic-terminal cutoff; later sparse OTC prints remain outside investability and are not mislabeled as a provider terminal date.

## Field-specific source order

Shares outstanding and public float are separate concepts and are never silently substituted.

Shares outstanding used for valuation:

1. Interactive Brokers fundamental share fields, when the account has the entitlement.
2. Yahoo Finance `sharesOutstanding`, then market-cap/price or share-history fallbacks.
3. Reviewed primary-filing share observations.
4. SEC CompanyFacts point-in-time share facts.

Public float used for liquidity and positioning:

1. A validated Interactive Brokers public-float fundamental field.
2. Yahoo Finance `floatShares`.
3. SEC `EntityPublicFloat` divided by the unadjusted share price, explicitly marked as a proxy.

Interactive Brokers `shortableShares` is borrow inventory and is never accepted as public float.

## IB entitlement behavior

The live validation connected to IB but received error 10358 for fundamental data. `enable_ib_fundamentals` is therefore disabled in transportation configuration to avoid repeating an entitlement-denied request on every daily refresh. IB remains first in shared source precedence and can be re-enabled after the account entitlement is provisioned. Yahoo and reviewed/SEC filing fallbacks remain active.

## Point-in-time and independence controls

- `fact_share_snapshot` is keyed by ticker, model family, date, and source. A transportation observation cannot serve defense or machinery.
- The shared loader contains no transportation ticker rules.
- Transportation listing conversions are effective-dated in `system_csvs/transportation_share_conversion_overrides.csv`.
- Transportation manual filing observations are immutable reviewed rows in `system_csvs/transportation_reviewed_share_observations.csv`.
- AZUL and LTM conversions begin only at their post-restructuring/relisting membership dates; no pre/post structural histories are joined.
- Reviewed filing observations carry source URLs and explicit proxy flags. The longer carry window exists only for this reviewed source and supports stale-to-exit cases with no later periodic filing.
- Current Yahoo/IB values outrank reviewed filing rows. Reviewed filing rows outrank generic SEC fallback only for shares outstanding; they are not public-float inputs.
- Families without a configured reviewed-observation CSV remain unchanged.

## Daily and historical execution

Daily current refresh runs the transportation wrapper without `--include-historical`; it performs only the bounded current-source pass and writes the Stage 3 report.

The one-time historical materialization is separate:

```powershell
C:\Users\josel\Miniconda3\python.exe industrials\transportation\scripts\03a_sync_transportation_share_snapshots.py --asof 2026-07-30 --history-start 2019-01-02 --include-historical --local-only --allow-partial
```

The valuation-source audit is:

```powershell
C:\Users\josel\Miniconda3\python.exe industrials\transportation\scripts\25e_audit_transportation_pit_valuation_sources.py --asof 2026-07-30
```

Canonical evidence:

- `output/industrials/transportation/stage3/transportation_share_snapshot_coverage.csv`
- `output/industrials/transportation/historical_load/transportation_historical_share_snapshot_coverage.csv`
- `output/industrials/transportation/valuation/transportation_pit_valuation_source_audit.csv`
- `output/industrials/transportation/valuation/transportation_pit_valuation_source_audit.json`

## Acceptance gates

| Gate | Requirement | Result |
| --- | --- | --- |
| Current shares outstanding | 90% minimum | PASS: 100% |
| Current public float | Reported separately | PASS: 100% |
| Historical shares outstanding | 90% minimum | PASS: 99.28% |
| Historical public float | No outstanding-share substitution | PASS: 90.65%; residuals remain unavailable |
| Required valuation sources | Every required issuer ready or explicitly reviewed | PASS: 107/107 |
| Model-family isolation | No cross-family observation serving | PASS |
| Structural breaks | No blind AZUL/LTM history join | PASS |
| IB borrow inventory | Never treated as float | PASS |

## Next authorized stage

The source-readiness gate now authorizes one point-in-time valuation feature rebuild. It does not authorize promotion by itself. The correct next stage is to build historical market capitalization and valuation features once, validate strict component coverage, and only then rerun the bounded OOS calibration sequence.
