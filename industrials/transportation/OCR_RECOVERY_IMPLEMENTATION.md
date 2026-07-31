# Transportation OCR recovery implementation

Date: 2026-07-29  
As-of corpus: 2026-07-22  
Status: implemented and acceptance-gated

## Objective

Exhaust the known image-only PDF lane without repeating the complete
transportation retrieval or semantic parse. The implementation keeps the
shared `dedicated_parser` repository independent, uses the existing
transportation adapter, and never mutates the original one-pass text cache.

## Implemented sequence

1. Install and verify a Tesseract runtime.
2. Seal the known image-only scope from the existing bounded-repair artifacts.
3. OCR each unique empty PDF once in an isolated cache namespace.
4. Diagnose failed OCR inputs before semantic parsing.
5. Re-download only structurally truncated PDFs and reuse prior successes on
   retry.
6. Combine all recoverable contexts into one source manifest.
7. Run one all-metric semantic parse over that recovery union.
8. Merge run evidence into reviewed evaluation 2 without reparsing earlier
   sources.
9. Reapply the existing sealed financial repairs, adjudicate stored evidence,
   replay policy idempotently, and freeze final metric dispositions.

The installed runtime is:

`C:\Users\josel\AppData\Local\Programs\Tesseract-OCR-Portable\tesseract.exe`

Verified version:

`tesseract v5.4.0.20240606`

The Conda binary was not used because its Windows runtime failed to start.
The recovery command accepts an explicit `--tesseract-exe`, so execution does
not depend on ambient `PATH` ordering.

## Scope and recovery results

| Gate | Result |
|---|---:|
| Hash-sealed image-only PDFs | 34 |
| Original ticker/document contexts | 38 |
| Original PDF pages | 464 |
| PDFs recovered directly with OCR | 27 |
| OCR text characters recovered | 954,562 |
| PDFs diagnosed as exactly 1 MiB and structurally truncated | 7 |
| Truncated PDFs recovered by exact-URL re-download | 6 |
| Final recoverable contexts | 37 of 38 |
| Final unique content hashes | 33 |
| Final ticker count | 19 |
| Remaining source limitation | 1 USAK context |

The six re-download recoveries include the legacy ATSG URL, whose media path
is case-sensitive, plus GLOG, GLOP, and Teekay documents. The USAK issuer URL
fails TLS and the old document is no longer available from the current
official site. A third-party mirror was tested but rejected because its first
1 MiB did not match the sealed truncated document hash; it was not silently
substituted.

## Semantic parse result

Dedicated-parser run 66 processed all 37 recovered contexts:

| Check | Result |
|---|---:|
| Newly executed work | 37 |
| Resume-linked work | 0 |
| Failed work | 0 |
| Physical document re-extractions | 0 |
| Parser invocations | 1 |
| Network requests during semantic parse | 0 |

Run 66 produced 34 evidence rows across 12 tickers and 24 specialized
metrics:

- 33 `REJECTED_POLICY`
- 1 `REVIEW_REQUIRED`

The review candidate was HA `passenger_load_factor=0.819`. It remains
deferred because the OCR text block is a statement of operations and does not
provide a reliable load-factor label/value association. It was not
auto-accepted.

## Final coverage

The OCR evidence did not change ticker-metric coverage status relative to the
already-finalized bounded union. It added stored evidence, but every affected
ticker-metric pair already had an equal or stronger status from other
documents.

| Measure | Final |
|---|---:|
| Applicable ticker-metric pairs | 2,526 |
| Accepted direct pairs | 49 |
| Financial-derived pairs | 135 |
| Total accepted pairs | 184 |
| Accepted coverage | 7.284% |
| Review-required pairs | 719 |
| Usable coverage | 35.748% |
| Discovered coverage | 53.603% |
| Financial-input gaps | 30 |

The final 90-metric disposition remains:

- 1 calibration candidate: `operating_ratio`
- 54 deferred review metrics
- 14 diagnostic-only metrics
- 21 excluded for insufficient evidence

No additional parser batch is required before building the selected market,
financial, and point-in-time feature tables.

## Acceptance gates

| Gate | Acceptance |
|---|---|
| DP7B bounded OCR recovery | `PASS_WITH_EXPLICIT_LIMITATIONS` |
| DP7C truncated-PDF repair and recovery union | `PASS_WITH_EXPLICIT_LIMITATIONS` |
| DP7C OCR-delta semantic execution, run 66 | `PASS` |
| DP6W all-source OCR union coverage | `PASS` |
| DP7A financial repair overlay | `PASS` |
| DP6I stored-evidence adjudication | `PASS` |
| Policy-only replay, evaluation 2 | `COMPLETED`, idempotent reuse |
| DP6L semantic fixture freeze | `PASS` |
| DP6X final metric disposition freeze | `PASS` |

The two limitation-bearing gates refer only to the one unavailable USAK
source. They do not represent parser failures or incomplete work.

## Implementation files

- `ocr_recovery.py`: Tesseract discovery/verification, isolated document
  handling, cache inventory sealing, and OCR result summaries.
- `scripts/09p_recover_transportation_ocr_delta.py`: exact 34-PDF OCR pass.
- `scripts/09r_recover_transportation_truncated_pdfs.py`: exact seven-hash
  truncated-document recovery with resumable retries.
- `scripts/09q_run_transportation_ocr_delta_parser.py`: one semantic batch
  over the final recovery union.
- `scripts/09k_build_transportation_all_source_union_coverage.py`: supports
  sealed supplemental execution gates.
- `scripts/09l_freeze_transportation_final_metric_dispositions.py`: merges
  and validates every supplemental run.
- `tests/industrials/test_transportation_ocr_recovery.py`: focused recovery
  helper tests.

## Key sealed artifacts

All paths below are under
`output/industrials/transportation/dedicated_parser/2026-07-22/`:

- `transportation_ocr_delta_manifest.json`
- `transportation_ocr_delta_cache_results.csv`
- `transportation_ocr_recovery_union_manifest.json`
- `transportation_ocr_recovery_union_source_manifest.csv`
- `transportation_ocr_delta_parser_execution_gate.json`
- `transportation_ocr_bounded_repair_union_coverage_manifest.json`
- `transportation_ocr_bounded_repair_union_evidence_adjudication_manifest.json`
- `transportation_ocr_repair_policy_replay_run58.json`
- `transportation_final_metric_freeze_manifest.json`

## Re-execution contract

Successful OCR, retrieval, semantic, and policy stages are idempotent and
hash-sealed. A rerun reuses successful artifacts. `--retry-limitations` on
the truncated-PDF repair reuses recovered documents and retries only
unresolved hashes. Broad retrieval, historical feature materialization,
calibration, portfolio publishing, and production promotion are not
authorized by this sequence.
