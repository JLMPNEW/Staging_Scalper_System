# Consumer Defensive and Transportation production-promotion audit — 2026-08-25

## Controlling verdict

Neither model family is authorized for capital production. Both remain disabled,
optional, zero-expected-alpha, and zero-cap in the Portfolio Layer. Their current
shadow packages are useful for integration and prospective observation, but an
execution, parser, publishing, or artifact-integrity `PASS` is not a predictive
acceptance `PASS`.

| Model family | Execution / artifact state | Predictive evidence | Capital-promotion eligibility | Portfolio state |
| --- | --- | --- | --- | --- |
| Consumer Defensive | Historical construction and shadow publishing reproduce successfully | Positive corrected core-baseline diagnostics, but underpowered and not strict OOS; specialized acceptance is zero | **False** | Disabled, not required, expected alpha `0.00`, optimizer cap `0.00`, BL prior `0.00` |
| Transportation | Corrected conflict audit, calibration execution, shadow publication, and adapter checks run successfully | **FAIL**; no ranked group passes every independent block and tankers remain blocked | **False** | Disabled, not required, expected alpha `0.00`, optimizer cap `0.00`, BL prior `0.00` |

This verdict supersedes any legacy wording that treats Stage 8/9/10 checks,
Stage 10B shadow governance, Transportation production-readiness execution, or
old promotion/activation scripts as sufficient capital evidence.

## Consumer Defensive evidence

The corrected V6 audit finds encouraging—but not promotion-grade—core-baseline
results after fixing turnover and cost accounting:

| Horizon | Independent observations | Required | Mean rank IC | Net top-minus-bottom spread | Sign-test p-value |
| --- | ---: | ---: | ---: | ---: | ---: |
| 21 sessions | 3 | 12 | 0.197942 | 0.059437 | 0.125 |
| 63 sessions | 2 | 6 | 0.197055 | 0.125608 | 0.250 |
| 126 sessions | 1 | 4 | 0.176805 | 0.136874 | 0.500 |

This is supportive evidence for continuing a frozen prospective monitor. It is
not enough independent evidence to estimate a stable effect or authorize
capital. The historical holdout was accessed by all 320 candidates although
only two were authorized; 318 candidates and 5,088 holdout-period rows were
therefore exposed. The legacy evidence also has a partial month, stale 13F
inputs, incomplete source-identity and survivorship proof, and a Stage 8/Stage 9
target mismatch. The holdout is burned and cannot be repaired by replay.

Specialized extraction is real measurement work, not wasted work: Stage 10
reports 543/971 measurement-qualified applicable ticker/metric pairs (55.92%).
However, zero of 38 specialized metrics passed every registered factor-validation
gate, so every specialized production weight correctly remains zero.

Controlling artifacts:

- `output/consumer_defensive/validation_v4/2026-08-25/v6/validation_audit_v6.json`
  — SHA-256 `88601954033abdd73455e44e7b27a995f97225b6d33b571535310a69ac3ca265`
- `output/consumer_defensive/validation_v4/2026-08-25/v6/artifact_manifest_v2.json`
  — SHA-256 `515fa9b28d76f3b0e8944d935e9641d1c345e8adefa45fc46c6af3f86a89f366`

The old Stage 10B package is retained only as a safe, zero-cap shadow-governance
record. Its `4/4 PASS` certifies four artifact/governance and fail-closed checks;
it is not a predictive-efficacy result. The package predates the corrected V6
audit and cannot authorize promotion.

## Transportation evidence

The corrected strict identity audit withdraws the earlier claim that 765 of
1,707 accepted-fact conflicts were deterministically resolved. Complete and
equal period/scope identities are required; under that rule, zero conflicts are
deterministically resolvable and all 1,707 remain fail closed. This correction
prevents ambiguous measurements from improving coverage or scores.

The truth-labeled calibration executed successfully, but predictive acceptance
failed:

| Ranked group | Block 1 | Block 2 | Block 3 |
| --- | ---: | ---: | ---: |
| Rail networks | PASS | FAIL | FAIL |
| LTL carriers | FAIL | PASS | FAIL |
| Truckload/intermodal | FAIL | PASS | FAIL |
| Asset-light logistics | FAIL | FAIL | FAIL |
| Oil tankers | BLOCKED | BLOCKED | BLOCKED |

Integrated parcel is an equal-weight eligibility sleeve whose predictive gate
is not applicable; it cannot be relabeled as a predictive pass. The corrected
shadow contains all 35 locked tickers, but only 24 are rank-ready. Every row has
zero OOS, research, Stage 11, survivorship, and portfolio authority, and the
adapter returns zero investable rows.

Controlling artifacts:

- Strict conflict audit — SHA-256
  `da45d138020611c46a5062bdc818cbbb41f3f08c017ce9d1d033048397f5ae9c`
- Conflict-bound score manifest — SHA-256
  `2ad00a390f19e9781968fc708da9ec4a30def7d1fc0331454c20e97134d812be`
- Truth-labeled calibration — SHA-256
  `17ea02e007e5d8f176c1c513b0de1fcb5a1f902d56b14ca54ff4c272cd399a40`
- Exact period-start feasibility — SHA-256
  `5ce42f68761740b0b2a92cdedafa385975907e05c1939beb61ea409d4cb9a638`
- Truth-bound shadow manifest — SHA-256
  `495ed6c84432ae1cb65bd94bc948fcb1e5890f61d41b3e974716ddad6ec6e6df`
- Shadow rank table — SHA-256
  `7106fbd44b0dafc1aba55051216b922a33139da677f517d4bd6b05d4ba69a86f`
- Portfolio adapter validation — SHA-256
  `0389f5469b0ff6a83f60e4c49f0585f97265c609e12e02fc8ff3eb1266a3a40b`

Exact artifact paths and the conflict/calibration methodology are recorded in
`industrials/transportation/TRANSPORTATION_V8_CORRECTNESS_AUDIT_2026-08-25.md`.

## Defects corrected or contained

The review repaired or fail-closed the decision-relevant defects that could
have created false confidence:

- Consumer candidate and baseline samples are compared on the same census;
  unauthorized holdout access is surfaced rather than treated as OOS evidence.
- Consumer 21/63/126-session targets, full-calendar month ends, partial months,
  stale positioning, source identity, survivorship, turnover, entry/re-entry,
  final liquidation, and both long/short sleeves are explicitly audited.
- Transportation facts require complete equal period, segment, denominator,
  unit, definition, and scope identities; missing/conflicting starts cannot be
  inferred, averaged, or overridden at score time.
- Prospective outcomes require exact official-session entry/exit timestamps,
  exact PIT membership, maturity-complete capture/outcome census, finite returns,
  and early terminal proceeds carried as zero-return cash to the common exit.
- Portfolio adapters reject shadow expected alpha above zero, research/OOS/
  survivorship claims from a shadow package, and generic-adapter bypasses.
- Legacy promoter, activation, packaging, acceptance, and pre-canonical capture
  routes are fail closed. Historical artifacts remain diagnostics only.

## Canonical future-evidence hardening

The prospective protocol has been strengthened beyond the historical audit. The
changes improve the validity of evidence collected in the future; they do not
turn any existing historical, revealed, pre-effective, or locally signed
artifact into capital-authorizing evidence.

- Every counted capture must replay the code-frozen scorer from the exact
  point-in-time feature/component snapshot bound to the registered baseline or
  v8 policy. The replay must reproduce each component, score, group rank, tail
  selection, and ticker census. A coherently rewritten score/rank file or an
  adaptive refit after observing prior outcomes fails closed.
- Consumer score replay requires a per-component, independently signed source-
  availability ledger tied to the official signal cutoff. Transportation
  activation freezes the exact historical v8 panel, accepted facts, source-file
  census, and staleness policy; every later scheduled append must preserve that
  prefix and carry independently signed availability for every panel and fact
  input. Self-declared/backdated input times, missing scheduled dates, changed
  staleness, and coherent historical rewrites fail closed.
- Final eligibility is the conjunction of governed lifecycle eligibility and
  deterministic frozen model/data-quality eligibility. An active security may
  be excluded only for an exact code-defined data-quality reason; discretionary
  exclusion is prohibited, and the full candidate/policy census plus every
  exclusion reason remains hash-bound.
- Trust registries, public keys, receipts, captures, registries, outcomes,
  calendars, membership, market bars, asset masters, and corporate actions are
  read as immutable byte snapshots. Hashing, parsing, signature verification,
  and arithmetic use the same bytes so an ABA or split-read file swap cannot
  combine a trusted hash with different parsed content.
- Canonical public APIs reject date truncation and timestamp normalization:
  dates must be exact `YYYY-MM-DD`, timestamps must be exact RFC3339 UTC, and
  offsets, space separators, missing seconds, coercible objects, and appended
  time text are not accepted.
- Capture precedes the next official XNYS entry execution. Outcome evaluation
  occurs only after the exact registered horizon matures, recomputes returns
  from independently attested open-execution total-return sources, and requires
  the complete matured capture-by-ticker-by-horizon census.
- Consumer cohort verdicts and Transportation sleeve verdicts are independent.
  Integrated parcel is explicitly not applicable to predictive rank gates and
  remains an operational monitor; it cannot create a vacuous pass. Passing
  evidence still produces only a zero-cap candidate for a separate independent
  review, never a production/configuration write.

The create-only preflight artifacts materialized on 2026-08-26 both report
`clock_started=false`, count zero current diagnostic artifacts, make no
calendar-date guarantee, and preserve a zero optimizer cap:

- `artifacts/future_only_evidence/2026-08-26/consumer_defensive_preflight_v3.json`
  - SHA-256
  `2d7615b4b9447b2b4e33bfdb8a3a7d7d15a6be921bc1243cc7b779de99ef461e`;
  12/6/4 observations remain for 21/63/126 sessions.
- `artifacts/future_only_evidence/2026-08-26/transportation_preflight_v3.json`
  - SHA-256
  `e0bf694ce6368b9bc42d9bb97184b6d35bcf907408130540d42349e199e21df4`;
  12/4 observations per sleeve remain for 21/63 sessions, and the missed
  2026-08-24 signal remains permanently ineligible for backfill.

Implementation verification date: **2026-08-26**. The source-package builder is
available via `python -m future_only_evidence.source_package_cli --help`. It
creates immutable unsigned snapshots and external signing requests only, runs
full structural score-input validation before any write, and reports
`capture_ready=false` until the configured independent market-data authority
attests the exact bytes. It cannot create timestamps, trusted receipts,
outcomes, passing evaluations, activation candidates, or portfolio writes.

Final focused verification commands and results:

```powershell
python -m pytest -q tests/test_future_only_evidence_canonical_clis.py tests/test_future_only_evidence_activation_candidate.py tests/test_future_only_evidence_prospective_contracts.py tests/test_future_only_evidence_outcome_integrity_v3.py tests/test_future_only_evidence_lifecycle_snapshot.py tests/test_future_only_evidence_canonical_trust.py tests/test_future_only_evidence_score_input_availability.py tests/test_future_only_evidence_transport_score_input_availability.py tests/consumer_defensive/test_future_oos_score_lineage_v2.py tests/consumer_defensive/test_future_oos_canonical_v5.py tests/industrials/test_transportation_future_oos_score_lineage_v1.py tests/industrials/test_transportation_future_oos_canonical_v6.py tests/test_future_only_evidence_source_packages.py tests/test_future_only_evidence_canonical_values.py tests/test_future_only_evidence_capture_integrity.py tests/test_future_only_evidence_interval_integrity.py tests/test_future_only_evidence_protocol.py tests/test_future_only_evidence_protocol_v2.py
# 229 passed in 14.51s

python -m ruff check future_only_evidence/canonical_values.py future_only_evidence/protocol.py future_only_evidence/capture_integrity.py future_only_evidence/interval_integrity.py future_only_evidence/source_packages.py future_only_evidence/source_package_cli.py consumer_defensive/core/future_oos_plan_v5.py consumer_defensive/core/future_oos_capture_v5.py consumer_defensive/core/future_oos_protocol_v5.py industrials/transportation/future_oos_activation_v6.py industrials/transportation/future_oos_capture_v6.py industrials/transportation/future_oos_protocol_v6.py tests/test_future_only_evidence_canonical_values.py tests/test_future_only_evidence_capture_integrity.py tests/test_future_only_evidence_interval_integrity.py tests/test_future_only_evidence_protocol.py tests/test_future_only_evidence_source_packages.py tests/consumer_defensive/test_future_oos_canonical_v5.py tests/industrials/test_transportation_future_oos_canonical_v6.py
# All checks passed
```

## Only admissible path to future promotion

The evidence clock has not started. The canonical trust registry is deliberately
`unconfigured_fail_closed`, so neither a local signature nor a historical replay
can establish a prospective start date.

Promotion requires this sequence:

1. Independently approve and pin three distinct authorities: evidence-content
   sealing, an external append-only timestamp log, and an independent market-data
   export attestation. Configure fixed provider/dataset, asset identity, exchange,
   currency, adjustment, price, benchmark, and timestamp-log policies.
2. Register and externally timestamp the exact frozen policy, candidate/universe
   census, official XNYS calendar, source bytes, thresholds, costs, and domain
   contract before any new target access.
3. Start with the first provable future eligible signal. Transportation's old
   `2026-08-24` planned signal was missed and cannot be backdated.
4. Capture target-blind signals on the frozen schedule. Obtain independently
   attested outcome data only after each horizon matures; omitted matured rows,
   duplicate slots, stale cutoffs, and partial months fail closed.
5. Consumer Defensive must accumulate at least 12/6/4 non-overlapping outcomes
   for 21/63/126 sessions and pass every registered efficacy, sign, breadth, cost,
   and integrity gate. Transportation must accumulate 12 21-session and four
   63-session outcomes per governed sleeve and pass its independently frozen
   subgroup/sleeve gates. These floors imply at least about 504 and 252 market
   sessions, respectively, after a valid start—not fixed calendar promotion dates.
6. A separate independent reviewer must bind a passing evaluation hash into a
   new promotion candidate. Only then may a reviewed change enable a source,
   assign nonzero expected alpha/caps, rehearse a backup-copy migration and
   rollback, and activate capital. The evidence process must not edit production
   configuration itself.

Until that full sequence passes, the truthful production decision for both
families is: **safe zero-cap shadow observation; no capital promotion**.
