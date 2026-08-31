# Consumer Defensive Stage 10 Publishing Audit

## Decision

Stage 10 is complete through isolated acceptance. Final run
`cds10_729cbfd933b3c0ddc912b999` passes all 17 Stage 10 checks after independently
re-running all 31 Stage 9 checks. It is permitted only as a Stage 10B
cross-sector/governance comparison input. It does not authorize a model-weight,
OOS-state, portfolio-gate, Portfolio Layer, or production change.

The accepted dated package is:

`C:\Users\josel\Documents\STAGING\ConsumerDefensiveRollout\20260824_exhaustive_audit\stage10\2026-08-14\v3`

The validation-gated stable copy is:

`C:\Users\josel\Documents\STAGING\ConsumerDefensiveRollout\20260824_exhaustive_audit\stage10\latest`

## Frozen Inputs

- as-of date: `2026-08-14`;
- read-only rehearsal database SHA-256:
  `41F67310DCA004BC100CD8016EC187AC58C269F6B56A424846ECCF6FC04EFDF7`;
- Stage 7 source: `consumer_defensive_stage7_baseline_v3`;
- Stage 7 contract:
  `d5184d007b89f3be62c61277cd4ddcb864f15ff0ccd09d9234de31922cf909c8`;
- Stage 9 run: `cds9_63065740a60179d1a1abc968`;
- Stage 9 manifest:
  `03346ffceb33b9f1c7b974229cad4ec1f5638945422d476f8cbe8aca3b1df183`.

Stage 10 uses a separate publishing policy. It does not change the frozen main
configuration used by Stage 8 and Stage 9.

## Accepted Contract

- Stage 10 contract:
  `729cbfd933b3c0ddc912b999bd9e5b210e0ec545685e7bb4f970c0cfa764d8cb`;
- artifact manifest:
  `1ef5e07f8f33b775574c2a356cddd907dbecae6ebd10c5edc8572d026abc709d`;
- source access: SQLite URI `mode=ro`, `query_only=ON`;
- database writes: `0`;
- production promotion: disabled;
- Portfolio Layer writes: disabled;
- portfolio candidate gate: `0` for every security;
- OOS-valid flag: `0` for every security;
- readiness: `research_only_not_investable`.

## Artifact Census

| Artifact | Rows |
| --- | ---: |
| Final-rank table | 108 |
| Company scorecard components | 5,940 |
| Core scorecard components | 1,836 |
| Specialized scorecard components | 4,104 |
| Cohort/sector summaries | 5 |
| Specialized cohort/sector coverage rows | 190 |
| Per-ticker specialized coverage rows | 108 |
| Review queue | 14 |
| Risk flags | 43 |
| Frozen Stage 9 baseline views | 40 |
| Source-ledger rows | 7 |

The HTML dashboard, JSON payload, contract, manifest, nine CSV reports, and
validation report are deterministic and self-contained. The `latest` directory
is updated only after a passing validation and is byte-identical to the accepted
dated version.

## Score And Coverage Results

| Scope | Tickers | Rank ready | Review | Qualified SEC pairs | Applicable pairs | Measurement coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Consumer Defensive | 108 | 94 | 14 | 543 | 971 | 55.92% |
| Beverages | 22 | 19 | 3 | 111 | 191 | 58.12% |
| Distribution & retail | 22 | 19 | 3 | 113 | 228 | 49.56% |
| Household/personal/tobacco | 25 | 22 | 3 | 108 | 173 | 62.43% |
| Packaged foods/agriculture | 39 | 34 | 5 | 211 | 379 | 55.67% |

“Measurement qualified” means accepted numeric measurement-only SEC evidence
with ticker, metric, period, source, and observation lineage. It does not mean a
metric passed directional factor validation. Zero of the 38 specialized metrics
are model-weight qualified, so all specialized weights remain zero. This avoids
converting better disclosure into an artificial score bonus.

## Validation

All 17 Stage 10 checks pass:

- upstream Stage 9 independently revalidated 31/31;
- complete artifact census and byte-for-byte recomputation;
- contract and manifest self-hashes;
- exact Stage 7 and Stage 9 source bindings;
- final-rank schema, rank order, and ticker tie-break;
- all rows remain shadow-only and non-investable;
- scorecard and specialized-component census;
- measurement-versus-model-weight qualification separation;
- ticker coverage arithmetic and review-queue reconciliation;
- 40 Stage 9 baseline views remain report-only;
- dashboard readiness/source labels; and
- zero database writes with an unchanged database checksum.

An exact replay re-ran generation after acceptance. All 14 dated-file hashes and
all 14 `latest` hashes were unchanged: 28/28 byte comparisons passed.

Desktop render QA at 1,440 pixels and responsive render QA at 500 pixels pass.
The dashboard exposes readiness, source citations, score census, cohort
coverage, specialized coverage, review exceptions, Stage 9 risk/capacity, and
downloadable machine-readable artifacts without remote dependencies.

## Failure History And Remediation

- The first unversioned attempt failed the final `latest` checksum gate because
  Windows newline translation changed LF bytes to CRLF during atomic copying.
  It was never accepted. The writer now uses `newline=''` and a byte-for-byte
  post-copy check.
- `v1` passed numerical validation but render QA found three replacement glyphs
  from a non-ASCII patch-transport error. It remains immutable and superseded.
- `v2` passed numerical validation and corrected the glyphs. Mobile render QA
  then exposed a two-column card overflow. It remains immutable and superseded.
- `v3` uses a one-column mobile card layout, prevents horizontal overflow, and
  is the accepted version.

## Next Gate

Stage 10B may now build the governance lockbox and signal registry from this
accepted package. It must preserve the Stage 7 baseline, zero specialized
weights, shadow-monitor state, OOS flag zero, and portfolio gate zero unless an
explicit independent governance decision authorizes a later change. Stage 11,
Stage 12, clean-room acceptance, and production migration remain downstream.
