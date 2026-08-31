# Consumer Defensive and Transportation production-promotion audit - 2026-08-26

## Controlling result

The implementation audit is complete, but neither model family is currently
eligible for capital production. Both remain disabled, optional, zero expected
alpha, zero optimizer cap, and zero Black-Litterman prior in
`portfolio_layer/config.yaml`.

This is not a parser-execution failure. The code is ready for a governed
future-only evidence run. The remaining blockers are statistical and external:

1. Consumer Defensive has supportive historical diagnostics, but its completed
   holdout is burned and contains only 3/2/1 independent 21/63/126-session
   observations versus the frozen 12/6/4 minimums.
2. Transportation's corrected historical evidence is inconsistent across
   chronological blocks; no predictive sleeve passes its complete frozen gate.
3. The independent trust and review registries remain
   `unconfigured_fail_closed`. No valid prospective evidence clock has started.

| Family | Software / artifact status | Supportive evidence | Promotion-grade evidence | Capital state |
| --- | --- | --- | --- | --- |
| Consumer Defensive | Canonical v5 capture/evaluation and signed component-source lineage pass their implementation tests | Positive historical core-baseline IC and spreads; 55.92% specialized measurement coverage | No: burned holdout, 3/2/1 independent observations, zero accepted specialized factors | Disabled; cap 0.00 |
| Transportation | Canonical v6 capture/evaluation and full signed score-input ledger pass their implementation tests | Some positive group/block diagnostics | No: no ranked sleeve passes all independent blocks; tanker blocked; 1,707 fact conflicts remain fail-closed | Disabled; cap 0.00 |

An execution, artifact-integrity, parser, or shadow-publication `PASS` must not
be represented as a predictive-acceptance `PASS`.

## Bugs and inconsistencies fixed in this audit

### Shared evidence protocol

- Replaced permissive timestamp coercion with exact RFC3339 UTC parsing on the
  active Consumer v5 and Transportation v6 capture/evaluation paths.
- Replaced date truncation with exact `YYYY-MM-DD` validation in the shared
  canonical protocol, capture-timing, and interval-timing validators.
- Added adversarial tests for noncanonical offsets, missing seconds, timestamp
  strings supplied where dates are required, invalid dates, and object-to-string
  coercion.

### Consumer Defensive

- Bound every score-driving Stage 6 component to an independently signed
  point-in-time availability record rather than trusting a locally generated
  aggregate timestamp.
- Bound ticker, component identifier/name, status, source table/row/field,
  component as-of date, exact value hash, source observation, provider/dataset,
  locator, source-record hash, availability time, signal cutoff, and attestation
  signature.
- Added exact structural validation for the atomic feature snapshot before an
  unsigned signing request can be emitted.
- Preserved the rule that unsigned packages are never capture-ready and cannot
  authorize activation.

### Transportation

- Replaced self-declared score-input timing with a signed ledger covering every
  cumulative scoring-panel row and every accepted fact. The ledger binds the
  full row, metric values/statuses, positioning score, eligibility flags,
  calibration cohort, source-score hash, value/unit/period/source identity, and
  ordered identity/content/value censuses.
- Added the omitted ticker to the source-observation mapping hash. Without this,
  the source-package builder and signed validator could disagree.
- Refactored the replay-input builder to match the expanded nine-role canonical
  source contract. It now emits structural panel/fact inputs and an unsigned
  availability signing request, and defers canonical score replay until the
  independent attestation exists.
- Added missing CLI inputs/outputs for baseline and prospective availability
  artifacts.
- Added exact semantic validation before writes and create-only preflight so an
  existing target cannot leave a partial package.
- Closed an append-only hole: newly appended panel rows *and* accepted facts
  must now have independently attested availability strictly after the frozen
  baseline or predecessor cutoff. A backdated fact can no longer enter a later
  cumulative replay, including through cross-kind source-record reuse.

## Consumer Defensive supportive evidence

The immutable Stage 8 panel has 9,036 rows over 86 dates from 2019-01-31 through
2026-02-11. The registered holdout listed six dates, but 2026-02-11 was a partial
month and was correctly excluded. Five completed dates remain, and they were
exposed to 320 candidates even though only two were authorized. The holdout is
therefore retrospective diagnostic evidence only.

| Horizon | Raw completed dates | Independent dates | Required | Independent mean IC | Independent net top-minus-bottom | Sign-test p-value |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 21 sessions | 5 | 3 | 12 | 0.197942 | 0.059437 | 0.125 |
| 63 sessions | 5 | 2 | 6 | 0.197055 | 0.125608 | 0.250 |
| 126 sessions | 5 | 1 | 4 | 0.176805 | 0.136874 | 0.500 |

The raw five-date IC signs were all positive, and the Stage 9 baseline
long-short holdout had four positive periods out of four with a 12.043%
compounded return. Those observations are encouraging, but overlapping raw
dates and an exposed holdout cannot be counted as independent prospective
evidence.

Specialized extraction produced 543 measurement-qualified observations out of
971 applicable ticker/metric pairs (55.92%). The factor-validation campaign
tested 174 registered cells: 84 lacked testable evidence and 90 were testable.
Exactly one cell passed false-discovery-rate control: 63-session
`alcohol_depletion_growth_pct` against SPY-beta residual returns, with mean IC
-0.609766 and p/q 0.011727. It was opposite the preregistered
`higher_is_better` direction, so rejection was correct.

Direction handling was independently reproduced. Raw values enter the kernel,
raw Spearman IC is calculated, `higher_is_better` requires positive IC,
`lower_is_better` requires negative IC, and an accepted lower-is-better factor
is inverted exactly once when converted to a Stage 8 percentile. A synthetic
lower-is-better factor with mean IC -0.960606 passed direction and FDR. Zero
specialized weights are therefore not caused by a direction bug.

Controlling historical artifacts:

- `output/consumer_defensive/validation_v4/2026-08-25/v6/validation_audit_v6.json`
  - SHA-256 `88601954033abdd73455e44e7b27a995f97225b6d33b571535310a69ac3ca265`
- `output/consumer_defensive/validation_v4/2026-08-25/v6/artifact_manifest_v2.json`
  - SHA-256 `515fa9b28d76f3b0e8944d935e9641d1c345e8adefa45fc46c6af3f86a89f366`
- Immutable Stage 8 panel
  - SHA-256 `ff95903a842052bf196058c6413cab3d98a3ea188609b3168b7383a0cd545e20`

## Transportation supportive evidence

The corrected calibration executes successfully but predictive acceptance
fails. The frozen score directions and weights were independently checked:
recipe, pack, component, and group weights tie to 1.0; every direction is +1 or
-1; negative-direction metrics are inverted exactly once; operating-ratio YoY
improvement and maximum drawdown use the correct sign conventions.

| Ranked group | Block 1 | Block 2 | Block 3 | Complete gate |
| --- | --- | --- | --- | --- |
| Rail networks | PASS | FAIL | FAIL | FAIL |
| LTL carriers | FAIL | PASS | FAIL | FAIL |
| Truckload/intermodal | FAIL | PASS | FAIL | FAIL |
| Asset-light logistics | FAIL | FAIL | FAIL | FAIL |
| Oil tankers | BLOCKED | BLOCKED | BLOCKED | BLOCKED |

Examples of supportive but insufficient results include:

- Truckload/intermodal, 63 sessions, all independent periods: mean IC 0.135411,
  mean top-minus-cohort net 0.011418, but top-minus-bottom was -0.008279.
- Truckload/intermodal block 2: mean IC 0.235895, top-minus-cohort net 0.074575,
  and top-minus-bottom 0.107925; blocks 1 and 3 failed.
- Rail block 1 at 63 sessions was positive, but later chronological blocks
  reversed.
- LTL top excess was positive, but IC and rank-spread diagnostics were negative.

Integrated parcel is deliberately an equal-weight monitoring group and is not
a predictive-gate pass. The shadow has all 35 locked tickers, 24 rank-ready
tickers, and zero capital authority. The strict conflict audit contains 11,040
rows and 1,707 unresolved conflicts; none is deterministically resolvable under
complete equal period/scope identity.

Controlling artifacts:

- Strict conflict audit SHA-256
  `da45d138020611c46a5062bdc818cbbb41f3f08c017ce9d1d033048397f5ae9c`
- Conflict-bound score manifest SHA-256
  `2ad00a390f19e9781968fc708da9ec4a30def7d1fc0331454c20e97134d812be`
- Truth-labeled calibration SHA-256
  `17ea02e007e5d8f176c1c513b0de1fcb5a1f902d56b14ca54ff4c272cd399a40`
- Frozen v8 score policy SHA-256
  `7254b662380f81c4ea3897cc30707ffe6283f54d463f5646255f5556eac2e5fd`
- Truth-bound shadow manifest SHA-256
  `495ed6c84432ae1cb65bd94bc948fcb1e5890f61d41b3e974716ddad6ec6e6df`

## Verification

The final implementation was exercised through independent overlapping suites
rather than reporting a misleading summed test count:

- 288 canonical protocol, source-lineage, capture/evaluation, preflight,
  legacy-lockdown, and portfolio-adapter tests passed.
- 45 shared/legacy protocol and capture-route tests passed.
- 108 Consumer factor/calibration/backtest and Transportation scoring/conflict
  regression tests passed.
- The red-team integration checkpoint passed 220 tests.
- The source-package implementation checkpoint passed 229 tests.
- Factor-direction and Transportation score arithmetic passed 73 focused tests.
- Ruff, Python compilation, and all canonical/source-package CLI help smoke
  tests passed.

No production configuration or capital authority was changed.

## Remaining external work and admissible promotion path

The canonical trust registry and independent-review registry are intentionally
unconfigured. Code cannot manufacture the independent keys, attestations,
future passage of time, market outcomes, or review decision needed for valid
promotion.

The remaining path is:

1. Independently approve and pin separate evidence-seal, timestamp-log, and
   market-data-export Ed25519 authorities, plus a separate promotion reviewer.
2. Register and externally timestamp the exact frozen policy, candidate census,
   calendar, source bytes, cutoffs, costs, thresholds, and source-query policy
   before the first eligible signal.
3. Start the first provable future capture. Missed or pre-effective signals may
   not be backfilled.
4. Capture every scheduled signal and obtain independently attested outcomes
   only after each horizon matures.
5. Consumer cohorts must independently reach 12/6/4 nonoverlapping
   21/63/126-session observations and pass every frozen gate. Transportation
   sleeves must independently reach 12 21-session and four 63-session outcomes
   and pass every ranked-group, aggregate, coverage, cost, and integrity gate.
6. A separate reviewer may create a zero-cap manual activation candidate for
   each passing cohort/sleeve. Only a later separation-of-duties change may
   assign nonzero expected alpha/caps and enable portfolio allocation.

Until those conditions are observed, the defensible state is **production-grade
shadow software, no capital promotion**. Enabling either sector now would not be
"supportive evidence"; it would be an unsupported production override.
