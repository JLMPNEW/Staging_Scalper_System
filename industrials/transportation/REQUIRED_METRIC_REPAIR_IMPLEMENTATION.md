# Transportation Required-Metric Repair Implementation

Status: implemented and validated in staging
Point-in-time date: `2026-07-30`
Production promotion: not authorized by this batch

## Outcome

The bounded required-metric repair scope contains 32 ticker/metric pairs across
19 tickers. The final acceptance audit reports:

| Outcome | Pairs |
|---|---:|
| Derived values | 24 |
| Reviewed `NOT_APPLICABLE` | 3 |
| Documented deferred | 5 |
| Unexpected unresolved | 0 |
| Total | 32 |

The 27 resolved pairs consist of 24 point-in-time values and three explicit
applicability decisions. The five unresolved pairs are exactly the predeclared
deferrals for CIIT, CTNT, and RUBI.

Repo-wide required-metric coverage is 937 of 969 applicable active-universe
pairs, or 96.70%. The remaining repo-wide gaps are not parser failures and are
outside the unexpected residual count for this repair scope.

## Efficient execution contract

The implementation preserves the one-search/one-parse design:

1. The bounded SEC/archive load completed once for the 18 financial tickers.
2. Dedicated-parser run 68 completed all 178 accession work items with zero
   failures.
3. All semantic decisions were made from that sealed run and hash-locked local
   filing documents.
4. No SEC retrieval or dedicated-parser rerun was used for the reviewed
   operand, feature, scoring, panel, or portfolio gates.
5. The final current PIT refresh rebuilt only `2026-07-30` from already-loaded
   raw history.
6. Historical calibration inputs and the frozen walk-forward calibration were
   not rebuilt because the specialized calibration-candidate set did not
   change.

## Implemented controls

### Reviewed operand policy

`review_policies/transportation_required_metric_operand_repairs.json` is the
fail-closed source of truth. It contains:

- 16 reviewed financial facts;
- 2 structural applicability overrides;
- document SHA-256 checks;
- parser evidence-key and normalized-fact fingerprint binding to run 68;
- exact arithmetic checks for reviewed sums and operating-income bridges;
- explicit exclusion of overlapping or rounded alternatives;
- zero permission for automatic extension promotion, network access, or
  reparsing.

The reviewed facts include:

- FY2025 cash capex for ASR, CMDB, CMRE, DAC, ECO, EDRY, ESEA, GATX, HSHP,
  MRTN, PAC, and SB;
- FY2025 ASR revenue and operating cash flow;
- FY2025 ASC and PBI operating-income component bridges.

The reviewed structural decisions classify AER and R operating margin as
`NOT_APPLICABLE`. Their leasing-centered presentation does not provide a
comparable reported consolidated operating-income subtotal.

### Aligned annual formulas

`reviewed_annual_metrics.py` prevents a reviewed annual operand from being
mixed with a different TTM or fiscal window. It is limited to pairs in
`transportation_required_metric_repair_scope.csv`.

For a scoped pair, the resolver:

- starts from an accepted reviewed annual anchor;
- requires matching period start, period end, and currency for every duration
  operand;
- verifies that the reviewed anchor was materialized in the canonical table;
- computes capex/revenue, FCF/revenue, or operating income/revenue only from
  that exact annual window;
- treats explicit zero capex as reported evidence, not imputation;
- returns fail-closed `NOT_DISCLOSED` when an aligned dependency is absent;
- classifies cash runway as `NOT_APPLICABLE` and capital-raise dependence as
  zero only when the same annual window proves nonpositive cash burn.

The shared TTM definition was not weakened. Annual reviewed values are exposed
with `reviewed_aligned_annual_formula` provenance rather than mislabeled as
TTM.

### Family-scoped source integration

Transportation now declares its two supplemental disclosure sources under the
transportation financial configuration:

- `dedicated_parser_transportation_required_metric_repair_v1`
- `transportation_reviewed_required_metric_operand_v1`

The shared financial builder consumes an explicitly configured family source
list without requiring the family to use the defense/machinery availability
path. This keeps the shared infrastructure common while transportation retains
its dedicated availability builder.

### Acceptance audit

`09zd_audit_transportation_required_metric_repair_outcomes.py` is the
read-only, repeatable 32-pair gate. It fails if:

- the scope no longer contains exactly 32 unique pairs;
- an availability row is missing;
- any unresolved pair is not documented in the reviewed policy.

Its current manifest reports 27 resolved pairs, 5 documented deferrals, and
zero unexpected unresolved pairs.

## Pair dispositions

### Resolved values

- Operating margin: ASC and PBI.
- Capex-to-revenue and FCF margin: ASR, CMDB, CMRE, DAC, ECO, ESEA, HSHP,
  MRTN, PAC, and SB.
- FCF margin: GATX.
- Capital-raise dependence: EDRY, derived as `0.0` from a non-burning aligned
  FY2025 window.

### Resolved not applicable

- AER operating margin: structural leasing presentation.
- R operating margin: structural leasing presentation.
- EDRY cash runway: the aligned FY2025 window is not cash-burning, so runway
  is not economically meaningful.

### Documented deferrals

- CIIT capital-raise dependence and cash runway: no complete same-window capex
  series exists; a one-day productive-asset fact is not annualized.
- CTNT capital-raise dependence and cash runway: OCF, capex, cash, and issuance
  observations do not share a validated window.
- RUBI maximum drawdown: 249 valid adjusted-price bars are available, below
  the 252-bar rule. Financial parsing cannot repair market history.

## Downstream gates

All of the following passed at `2026-07-30`:

- reviewed operand plan and execute gates;
- financial feature materialization;
- specialized-metric availability build and validator;
- 32-pair repair outcome audit;
- exact-date PIT snapshot refresh;
- scoring build and independent scoring validation;
- shadow rank publication and independent rank validation;
- current-only complete-panel build;
- outcome-blind monitoring-source export;
- zero-overlay monitoring audit;
- shared `portfolio_layer` adapter validation;
- 381 transportation and portfolio-adapter regression tests;
- Ruff checks for all modified transportation/shared-builder files.

The scoring result is 83 rank-ready names and 29 blocked names. The refreshed
shadow rank still has zero OOS-valid and zero portfolio-candidate rows. That is
the existing frozen zero-overlay calibration and production-governance
decision, not a failure of the required-metric repair. This batch does not
authorize production promotion or a new calibration.

## Repeatable commands

```powershell
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09zc_load_transportation_reviewed_required_metric_operands.py --asof 2026-07-30 --plan-only
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\09zd_audit_transportation_required_metric_repair_outcomes.py --asof 2026-07-30
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\08a_validate_transportation_specialized_metrics.py --asof 2026-07-30
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\06a_validate_transportation_scoring_features.py --asof 2026-07-30
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\18_validate_transportation_shadow_rank_table.py --asof 2026-07-30
C:\Users\josel\miniconda3\python.exe industrials\transportation\scripts\20_validate_transportation_portfolio_adapter_shadow.py --asof 2026-07-30
```
