# Transportation Efficient Parser Batch

Status: completed through the parse-free DP6X final metric freeze on 2026-07-28.

## Objective

The transportation search is content-addressed and one-pass. Source retrieval is
finished before parsing. Every unique physical document is decoded once, while
the same content may be evaluated in more than one ticker context. All applicable
specialized metrics are evaluated together. Coverage, review-policy replay,
financial repair, feature construction, calibration, and portfolio integration
are downstream operations and must not retrieve or parse the documents again.

## Frozen scope

- 84 direct parser metrics are searched.
- 7 derived metrics are calculated later from accepted operands and are not
  searched as standalone disclosures.
- 6,591 unique new content hashes are physically extracted once.
- 6,958 ticker/content contexts are evaluated because 367 identical documents
  apply to a second ticker context.
- 95,191 scoped ticker/metric/document evaluations are represented by the
  direct source manifest.
- Completed run content from runs 58, 59, and 60 is excluded by content hash.
- No unresolved retrieval request remains: 758 residual pairs have an alternate
  ready source and 58 are terminal after the completed SEC search.

## Enforced sequence

### DP6S: residual freeze and direct-delta seal

Run `scripts/09g_freeze_transportation_efficient_parser_batch.py`.

Acceptance requires:

- source disposition is complete;
- unresolved and newly authorized retrieval counts are zero;
- every local file exists and matches its declared SHA-256;
- the direct parser metric union is exactly the adapter's 84 metrics;
- completed content hashes are excluded; and
- the general parser execution switch remains disabled.

Current sealed source:

`output/industrials/transportation/dedicated_parser/2026-07-22/transportation_non_sec_direct_delta_source_manifest.csv`

### DP6T: offline exact plan

Run `scripts/09h_plan_transportation_efficient_parser_batch.py`.

Acceptance requires 6,958 planned contexts and documents, zero missing local
documents, direct-document mode, exact per-context metric scopes, resume enabled,
SEC-only providers disabled, and the configured PDF/OCR scope. This gate performs
no semantic extraction and makes no network request.

### DP6U: unique-content text cache

Run `scripts/09i_prepare_transportation_content_text_cache.py --execute` under
the `scalper-staging` environment.

The script first converts the 51 legacy OLE Word documents with the installed
Microsoft `Wordconv.exe`. Each conversion has a 60-second timeout. It then creates
one gzip JSON text record for every unique hash. DOCX and XLSX inputs are parsed
locally; PDFs use native extraction with the configured OCR fallback. The cache
key includes the extraction settings, and a cross-process lock prevents duplicate
physical decoding.

Acceptance is `PASS` or `PASS_WITH_EXPLICIT_EXTRACTION_LIMITATIONS` only when all
6,591 hashes have an explicit cached result, no extraction failed, every legacy
Word input has a converted DOCX, and the sealed source and extraction-option
hashes still match. Empty or limited extractions remain visible; they are not
silently treated as successful metric evidence.

### DP6V: one resumable semantic execution

Run `scripts/09j_run_transportation_efficient_parser_batch.py --execute` only
after DP6U passes.

The runner fails closed unless DP6S, DP6T, and DP6U match the same source hash,
adapter version, counts, and extraction settings. It calls the independent
`dedicated_parser` exactly once with all manifest-scoped metrics, resume enabled,
no force flag, SEC-only providers disabled, and adjudication generation skipped.
The adapter reads the DP6U cache, so semantic execution does not physically decode
the 6,591 source bodies again. Re-running the command reuses the passing execution
gate or resumes only incomplete work under the same sealed work keys.

### DP6W and later: parse-free decisions

After DP6V passes:

1. Union reviewed evidence from run 58, delta/repair evidence from runs 59 and
   60, and the DP6V run.
2. Apply the frozen semantic fixtures and review policy without source parsing.
3. Execute the frozen financial repairs and calculate the seven derived metrics
   from accepted operands.
4. Freeze the final 90-metric coverage table and select the realistic production
   metric subset.
5. Only then build market/financial/specialized feature tables and point-in-time
   history.
6. Run walk-forward calibration once against the frozen feature contract.
7. Validate the existing portfolio-layer adapter, shadow outputs, and production
   promotion gates.

If policy thresholds change, repeat DP6W from stored evidence. If a metric is
dropped, rebuild coverage and features from stored evidence. Neither case
authorizes another retrieval or semantic parser batch.

## Resource-safety invariants

- Retrieval invocations after DP6S: 0.
- Physical extraction per new content hash: at most 1 for a fixed extraction
  option hash.
- Semantic parser invocations for this delta: 1 resumable invocation.
- Feature builds before final metric freeze: 0.
- Historical materializations before final metric freeze: 0.
- Calibrations before final metric freeze: 0.
- Portfolio writes before promotion: 0.
- General transportation parser authorization remains `false`; only the sealed
  DP6V runner can authorize this batch.

## Completed results

DP6U cached all 6,591 unique content hashes with zero failed extractions. The
51 legacy Word documents were converted once. A bounded PyMuPDF recovery pass
improved 67 PDF cache records: 33 became nonempty and 34 remain explicit
native-text-empty limitations. No cache record is silently missing.

DP6V executed the semantic batch once as parser run 65. All 6,958 logical
ticker/content contexts completed, none failed, and the before/after cache
inventory hashes match. The execution gate records one parser invocation and
zero physical document re-extractions.

DP6W unions reviewed run-58 evidence, SEC delta run 59, PDF repair run 60, and
direct-document run 65. Evaluation 2 restores all seven already-applied exact
policy confirmations. Across 2,535 applicable pairs, accepted coverage is
178 pairs (7.02%), usable coverage is 35.38%, and discovery coverage is
53.18%. Relative to the pre-direct-document union, the new documents add 22
usable-review pairs and 67 discovered pairs without automatically promoting
ambiguous evidence.

The final all-source adjudication is idempotent: all 719 remaining
review-required pairs are deferred, with zero new exact confirmations and zero
policy candidates. The semantic fixture freeze contains those 719 pairs and
2,007 evidence rows. The financial repair freeze retains 45 explicit pairs:
23 alignment/formula gaps over already-loaded facts, nine formula-defined
not-applicable cases, and 13 source/period gaps. These are financial-pipeline
work and do not authorize another specialized semantic parser batch.

DP6X freezes all 90 metric dispositions using accepted evidence periods only.
The result is one calibration candidate (`operating_ratio`), 54 deferred-review
metrics, 14 diagnostic-only metrics, and 21 excluded metrics. It records zero
additional parser batches required. The next expensive action is a single
selected-feature and point-in-time history build; calibration remains blocked
until that frozen panel passes validation.

## Bounded post-freeze repair results

DP6Y through DP7A completed on 2026-07-29 without another retrieval or semantic
parser batch.

- DP6Y hash-sealed exactly 898 residual items: 45 financial pairs, 100
  text-hit/no-value pairs, 34 unique native-text-empty PDF hashes, and 719
  stored-evidence review pairs.
- DP6Z read only the current financial table and 54 already-compressed text
  cache records. Four capital-raise ratios were resolved from aligned feature
  operands. Two more ratios were resolved from reviewed exact same-period
  primary-source tables: EHLD 2025 capital-raise dependence and HMR Q1 2026
  stock compensation to revenue.
- Nine cash-runway observations were reclassified as formula-defined not
  applicable. Nineteen alignment cases and 11 source/period cases remain
  explicit financial gaps.
- The 100 no-value pairs produced no unsafe promotion: 51 stored evidence sets
  are non-numeric and 49 contain ambiguous numbers. The 34 empty PDF hashes
  remain explicit limitations because no local Tesseract engine is installed.
- DP7A increased accepted coverage from 178/2,535 to 184/2,526 applicable pairs
  (7.28%). Usable coverage is 35.75% and discovery coverage is 53.60%.
- Final stored-evidence adjudication still defers all 719 ambiguous pairs. The
  policy replay reused evaluation 2 idempotently with zero source opens and
  zero parser, Arelle, EdgarTools, or OCR calls.
- The final 90-metric dispositions are unchanged: one calibration candidate,
  54 deferred-review, 14 diagnostic-only, and 21 excluded metrics.

DP8 and DP9 subsequently completed without another retrieval, parse, database
write, or generic feature build. The frozen v3 outputs contain 854,640
specialized discovery rows and 1,025,568 complete rows across 9,496 historical
memberships. G8 independently re-streamed both panels with zero future-date
errors. The calibration-subset manifest selects only `operating_ratio` from
the complete-panel hash. The implementation must not repeat the
6,591-document semantic parse or the v3 feature materialization.
