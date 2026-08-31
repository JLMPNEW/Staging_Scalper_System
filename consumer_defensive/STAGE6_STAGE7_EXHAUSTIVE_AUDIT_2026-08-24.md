# Consumer Defensive Stage 6/7 Exhaustive Audit

Audit date: `2026-08-24`

Scope: Stage 6B accepted-observation handoff, Stage 6A measurement overlay,
Stage 6C point-in-time panel, the Consumer Defensive shared
`factor_validation` adapter, and Stage 7 shadow scoring. Production databases,
production scores, Portfolio Layer inputs, and investable gates were out of
scope and were not changed.

## Verdict

The legacy Stage 6C panel and factor campaign are retained as immutable
historical evidence, but they are superseded for future research by corrected
Stage 6C run 3 and campaign
`cdfv_20260814_d2c7155be91c_2498172c7161_a6495192b5`.

The corrected implementation is suitable for continued shadow research. It is
not approved for production promotion or portfolio use. Stage 7 v3 remains
`shadow_monitor`; every specialized weight, portfolio candidate gate, and OOS
validity flag remains zero.

## Audit Order And Results

| Order | Area | Result |
| --- | --- | --- |
| 1 | Immutable Stage 6B run and observation manifest | Fixed exact-run binding in both the Stage 6A overlay and Stage 6C panel. |
| 2 | Point-in-time observation selection | Fixed period/acceptance ordering and observations accepted before their period end. |
| 3 | Panel schema, hashes, exports, and lineage | Added exact manifest hashes, registry binding, source selection/lineage checks, and fail-closed export. |
| 4 | Factor scope routing and campaign identity | Removed redundant/impossible scopes, added governed subtype fallback, safe hashed scope IDs, and methodology-bound campaign IDs. |
| 5 | Evidence/report validation | Added exact exported-manifest checks, report cell identities, exact package-state counts, and malformed-report handling. |
| 6 | Stage 7 contract and scoring | Added exact Stage 6B run lineage, corrected policy/verdict semantics, and versioned the immutable source/model to v2. |
| 7 | Independent scoring and ranking tie-outs | Added independent weighted-score, data-quality, score-order, ticker tie-break, percentile, and unranked-field checks. |
| 8 | Clean isolated replay | Corrected Stage 6C, factor campaign, and Stage 7 v3 all passed; production remained unchanged. |

## Bugs And Inconsistencies Corrected

### High severity

1. Stage 6C selected consolidated scope before the latest eligible reporting
   period. The legacy audit found 7,444 affected panel selections across 220
   ticker/metric pairs. Selection now orders by period end, accepted-at time,
   scope priority, confidence, and observation hash.
2. The Stage 6C observation cursor permanently discarded an observation when
   it had been accepted but its period end was still in the future. That
   observation could never become eligible on a later evaluation date. The
   panel now retains accepted observations and applies both point-in-time
   conditions at each evaluation date.
3. Stage 6C and the Stage 6A overlay could read accepted facts that were not in
   the selected Stage 6B run. Both consumers now load only the exact
   `observation_sha256s` manifest of the latest completed as-of run and fail if
   the manifest and stored facts differ.
4. The Stage 6A overlay enforced accepted-at time but not
   `period_end <= as_of`. Future-period observations reported early could enter
   current scoring inputs. The overlay now enforces both constraints.
5. Stage 7 trusted only a Boolean overlay marker. It now requires every input
   row to reference the exact latest completed Stage 6B run ID.
6. Stage 6C and campaign provenance hashed the entire shared YAML, including
   Stage 7's downstream campaign reference. Updating Stage 7 therefore changed
   the upstream campaign identity and made an exact self-consistent replay
   impossible. Stage 6C now hashes a semantic panel-only contract, and factor
   validation writes and seals a factor-only config snapshot. Downstream config
   edits no longer alter upstream identities.

### Medium severity

1. Stage 6C feature-manifest rows were exported without a semantic registry
   tie-out, and the adapter did not prove that panel/manifest CSVs were exact
   database exports. Row hashes, registry fields, creation timestamps, counts,
   and exact database equality are now checked before campaign registration.
2. Invalid Stage 6C runs could be exported. Report generation now fails closed
   unless every Stage 6C validation check passes.
3. Campaign IDs omitted adapter/methodology provenance, permitting identity
   collisions after code changes. New IDs include a methodology hash covering
   the adapter and observed code/config/source provenance.
4. The legacy router registered redundant sector scopes and structurally
   impossible cohort scopes. The corrected router uses sector scopes only for
   multi-cohort factors, cohort scopes only when their registered minimum can
   be met, and explicit subtype scopes with a floor of three only when the
   cohort is structurally impossible.
5. Full cohort/subtype scope names exceeded the shared registry's 64-character
   ID limit. Subtype IDs are now deterministic 20-hex hashes; full cohort and
   subtype names remain in report metadata and are used for row selection.
6. Report validation compared only aggregate counts and could throw on malformed
   count or horizon values. It now compares exact cell IDs and identities,
   exact verified package-state counts, and returns a controlled failure for
   malformed payloads.
7. Stage 7 said missing values made a “zero contribution,” while it actually
   applies the frozen neutral score of 50 at the component's unchanged weight.
   Stage 7 now records
   `neutral_score_contribution_no_weight_redistribution`. Stage 6A retains its
   separate zero-weight specialized-metric wording.
8. Stage 7's legacy factor verdict overstated the evidence by implying every
   metric had been adequately tested. Stage 7 v3 references the corrected
   campaign and records that eight metrics were testable, one cell passed FDR
   in the wrong pre-registered direction, and zero cells were directionally
   accepted.
9. Stage 7 rank validation checked only contiguous ranks. It now independently
   reconstructs weighted scores, data-quality confidence, sector/cohort score
   order, the ticker-ascending ordinal tie-break, percentiles, and null rank
   fields for review-required rows.

## Corrected Measured Evidence

### Stage 6C run 3

- 81,221 immutable panel rows
- 28,487 valid numeric rows
- 86 monthly evaluation dates
- 38 registered metrics
- 18/18 validation checks passed
- panel SHA-256:
  `d2c7155be91cf21c2826e911e083e662bf203119ee087baf12f754ac2d2adcf0`
- exact source Stage 6B run: 37

The legacy panel had 30,309 numeric rows. The corrected run removed 1,822
numeric rows that did not satisfy the corrected sealed-source and point-in-time
selection contract; it did not reduce the expected panel skeleton.

Historical metrics with at least one numeric value by cohort are:

| Cohort | Metrics | Numeric panel rows |
| --- | ---: | ---: |
| Beverages | 9 | 5,449 |
| Distribution and retail | 12 | 5,456 |
| Household, personal care, and tobacco | 11 | 5,900 |
| Packaged foods and agriculture | 13 | 11,682 |

### Corrected shared factor campaign

- 174 governed cells across 66 FDR families and 11 registered scopes
- 90 evidence-eligible/testable cells
- 8 testable metrics: alcohol depletion growth, case-volume growth,
  comparable-sales growth, fixed-charge coverage, gross-margin change,
  net debt/EBITDA, organic-revenue growth, and physical-volume growth
- 84 cells remained ineligible because they lacked both the required IC dates
  and independent windows
- 1 FDR-significant cell: alcohol-depletion growth, alcohol subtype,
  63-session SPY-beta-residual target, `p=q=0.0117267`
- that cell's mean IC was `-0.6098` versus the pre-registered
  `higher_is_better` direction, so the direction gate correctly rejected it
- 0 directionally accepted cells and 0 activated specialized weights

The inverse alcohol-depletion result is a new research hypothesis. It must be
registered and tested on new chronological evidence; its direction must not be
flipped after observing this campaign.

### Stage 7 v3 isolated replay

- 108/108 deterministic shadow outputs
- 94 rank-ready and 14 review-required (`87.04%`, floor `85%`)
- 15/15 validation checks passed
- 38 specialized weights equal zero
- contract SHA-256:
  `d5184d007b89f3be62c61277cd4ddcb864f15ff0ccd09d9234de31922cf909c8`
- baseline-input manifest SHA-256:
  `ad90697b81c020c3666d47b04aa2ece231a2d8b7793dc00d23e27dd907f2500a`
- output manifest SHA-256:
  `abcca120e948d45a440b5f421809f3fb98b656484a4439d8b493e8a852fe93e8`

### Automated verification

- 391 Consumer Defensive tests passed; 5 platform-specific tests skipped
- 94 shared factor-validation kernel/artifact/FDR/safety tests passed
- all 16 focused Stage 6B/6C/factor/Stage 7 regressions passed
- Ruff passed every changed Python file
- the final campaign ledger and all 174 evidence packages verified with zero
  errors

## Controls Audited Without A Confirmed Defect

- The shared BH-FDR implementation behaved as registered; FDR was not the
  cause of legacy low testability.
- No mixed units or mixed definition versions were observed within a metric in
  the corrected panel.
- Missing data remains explicit; unavailable metrics are not fabricated,
  zero-filled, or used to redistribute weight.
- Market-share and other selectively disclosed metrics remain excluded from
  validation under the coverage-bias policy.
- Production writes, promotion, Portfolio Layer writes, and investable output
  remained disabled throughout the audit.

## Residual Risks And Next Gate

The shared independent-window inference uses the existing governed kernel and
was not changed in this sector audit. A separate cross-sector methodology
review may compare its approximation with block-aggregated inference, but that
would require a new shared-kernel contract and must not be introduced through a
sector adapter.

Stage 8 should begin from Stage 7 v3 only after the corrected artifacts and this
audit are reviewed. Its first expected candidate remains the core-only baseline.
The inverse alcohol-depletion hypothesis may be registered for future evidence,
but it is not eligible for a current nonzero weight.
