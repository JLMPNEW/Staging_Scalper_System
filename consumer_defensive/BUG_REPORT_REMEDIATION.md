# Consumer Defensive bug-report remediation

Record type: historical point-in-time remediation ledger. The current authoritative contracts are `README.md`, `STAGE_GATES.md`, `IMPLEMENTATION.md`, and the latest validated replay artifacts. Earlier configured-database counts or pass statements below do not establish acceptance of later migration, scope, lifecycle, immutable-seal, path, or chronological-watermark controls.

Audit date: 2026-08-11  
Attachment reviewed: `pasted-text.txt` (16 numbered findings plus governance/gate highlights)

## Disposition

| Finding | Disposition | Remediation |
|---|---|---|
| F1 ROIC used net debt | Confirmed | Financial v2 defines invested capital as debt plus equity less cash and refuses non-positive or incomplete denominators. |
| F2 absent debt became zero | Confirmed | Debt is now nullable; no debt facts means unknown leverage and ROIC, not apparent net cash. |
| F3 debt concepts treated as alternatives | Confirmed | Current maturities and short-term borrowings are separate additive components; alternatives remain within each component. |
| F4 annual and quarterly facts collapsed | Confirmed | The canonical key and migration now include component and period start, preserving different durations at the same period end. |
| F5 non-contiguous quarters summed as TTM | Confirmed | Four-quarter sums require quarterly adjacency. Otherwise TTM uses annual plus current interim less comparable prior-year interim, then falls back to the latest annual fact. |
| F6 delisted eligibility flags disagreed | Confirmed | The reviewed terminal ledger is authoritative during candidate loading and reconciliation synchronizes membership flags. Validation rejects disagreement. |
| F7 delisted names never received market features | Confirmed | Feature membership is now point-in-time recognized-index membership plus major-exchange status, not today's active set. Validation uses the same selector. |
| F8 63-day volatility used 62 returns | Confirmed | The calculation now consumes exactly 63 returns when 64 prices are available. |
| F9 downside volatility used standard deviation | Confirmed | It now computes annualized downside deviation: root mean squared negative return relative to a zero target. |
| F10 residual momentum was row-aligned | Confirmed | Ticker and benchmark endpoint prices must now share the exact start and end dates. Missing benchmark endpoints produce `NULL`, not a mismatched residual. |
| F11 pre-history filter compared only years | Confirmed | Delisted scope and exact last-trade dates now come from the terminal ledger; pre-history comparison is date-to-date. |
| F12 delisted seed lacked authoritative terminal fields | Confirmed design defect | The seed is discovery metadata only. The reviewed terminal ledger now controls scope, dates, values, successor terms, and eligibility; DF resolves to provider exit year 2021. |
| F13 bad explicit provider symbol fell through | Confirmed | Explicit mappings fail closed when absent or ambiguous and never enter fuzzy matching. |
| F14 validator ignored custom terminal policy | Confirmed | Stage 3 validation accepts the loaded policy and the CLI exposes and passes `--terminal-policy`. |
| F15 successor loaders used different PIT predicates | Confirmed | Yahoo and Norgate successor requests share one economic-event/reference-date as-of predicate. |
| F16 unguarded `lastrowid` conversions | Confirmed | All reported insert sites use one guarded `require_lastrowid` invariant. |

## Governance and gate highlights

- Missing SEC acceptance timestamps were a real as-of defect. Such filings are now excluded, and both filing and raw-fact acceptance completeness are hard validation checks.
- Missing FX validation was real. Stage 4 now requires the current canonical definition, required-currency coverage, and zero unconverted canonical monetary facts. The production FX history was refreshed to 2010 and the gate reports zero misses.
- Duplicate active-universe definitions were a maintainability defect. Current-universe callers now share the taxonomy-scoped selector; historical feature selection has a separate, explicitly PIT selector.
- Dead duplicate path entries were removed from `config.yaml`, including the nonexistent historical-membership CSV path. Authoritative universe and terminal paths remain in their owning policies.
- Safety controls that already describe executable invariants are now validated fail-closed: PIT membership, terminal reconciliation, no raw-unadjusted return fallback, membership requirement, coherent review thresholds, and paired OOS lock dates.
- Liquidity thresholds remain universe-review thresholds rather than automatic daily exclusions. Making them a daily hard filter would silently shrink calibration cohorts and would conflict with the adopted flexible-cohort policy.
- Empty OOS lock dates, disabled adapters, and zero portfolio weight are intentional pre-promotion state, not current execution bugs. Their implementation belongs to the later parser/factor-validation/Portfolio Layer stages.
- The disclosure census redundant-I/O claim was stale against the audited code: documents are parsed once into `document_texts` and reused across metrics. No change was required.
- The Norgate preflight now derives dates, paths, indices, and watchlists from the universe policy and uses the production symbol resolver. Provider-wide diagnostic helpers remain local because they are audit-only operations.
- The rejected `tz_localize(None)` hypothesis remains rejected; no change was made.

## Verification evidence

- `python -m ruff check consumer_defensive/core tests/consumer_defensive`: PASS.
- `python -m pytest tests/consumer_defensive -q`: 30 passed.
- Configured Stage 3 validation at 2026-08-10: PASS; 108 expected PIT features, 108 written, all full-history.
- Configured terminal validation: PASS with the one reviewed WBA contingent-value exclusion; 10 of 11 terminal events calibration-eligible.
- Configured Stage 4 validation at 2026-08-10: PASS; 230,709 v2 canonical facts, 0 FX misses, 119 feature rows.
- Historical market proof at 2021-05-27: 103 PIT features, including CORE, K, SAFM, SPTN, TWNK, and VGR, which are delisted today.

All legitimate defects concretely described in the attachment are fixed. The items classified as deferred policy state or a rejected hypothesis are not runtime bugs and were not misrepresented as implemented production stages.
