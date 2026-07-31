# Transportation DP6R Primary-Document Hydration

## Purpose and boundary

DP6R consumes only the hash-sealed DP6Q hydration request manifest. It retrieves
primary-document bytes once, validates the response, computes SHA-256, stores one
copy per unique content hash, and fans that content back out to every approved
transportation document and ticker. It does not run the dedicated parser, build
features, materialize history, calibrate, publish to the portfolio layer, or
authorize production.

The implementation covers all 112 active and 48 inactive transportation
identities represented by the DP6Q contract. A missing or inaccessible response
remains an explicit primary-document source gap; it is never converted into a
metric value.

## Implementation

The gate is implemented by:

- `primary_document_hydration.py`, which provides atomic content-addressed
  storage, SHA-256 verification, per-domain throttling, validated resume
  metadata, response validation, redirect quarantine, request-to-document
  fanout, and content-level deduplication;
- `scripts/09f_hydrate_transportation_primary_documents.py`, which verifies the
  DP6Q artifact hashes, selects full or narrowly filtered recovery scopes,
  writes atomic request/document/content/failure artifacts, and keeps every
  downstream authorization false;
- `review_policies/transportation_hydration_redirect_policy.csv`, which approves
  three exact issuer-to-asset-host routes and excludes two reviewed
  non-financial redirects; and
- `review_policies/transportation_hydration_host_recovery_policy.csv`, which
  limits origin-referer and cookie-preflight recovery to four hosts proven by
  direct diagnostics.

The cache root is
`output/industrials_cache/transportation/non_sec_primary_documents`. Request
metadata is stored independently from content bytes. A successful retry changes
only the request metadata and content catalog; already verified content is not
downloaded again.

## Safety and restart contracts

DP6R is fail-closed:

1. The DP6Q review manifest, request manifest, and reviewed-document manifest
   must match their sealed paths, row counts, versions, and SHA-256 hashes.
2. One process lock prevents concurrent writers to the family cache.
3. Content is written atomically and re-read for byte-count and SHA-256
   verification.
4. Concurrent URLs that return identical bytes serialize only on that content
   hash, preventing Windows write races without serializing unrelated files.
5. A completed request resumes as `CACHE_HIT_VALID` only when its metadata,
   request identity, manifest hash, file size, and content hash all reconcile.
6. Failed and quarantined requests are not retried unless an explicit recovery
   option is supplied.
7. Redirected response bytes are preserved but cannot become content-ready
   until the exact route is reviewed. Reviewed non-financial redirects become
   terminal exclusions, not source gaps.
8. Retryable-only and HTTP-status recovery modes select from the canonical
   failure artifact. They cannot silently widen the sealed DP6Q scope.
9. The parser and all downstream stages remain disabled in every diagnostic,
   recovery, and canonical run.

## Initial canonical pass

The 2026-07-22 canonical first pass completed all 8,404 physical requests in
1,829.996 seconds and sealed `PASS_WITH_REQUIRED_RECOVERY`:

| Result | Count |
| --- | ---: |
| Content-ready requests | 4,230 |
| Hydrated requests | 4,094 |
| Validated cache hits | 136 |
| Failed requests | 4,130 |
| Quarantined redirects | 44 |
| Content-ready document mappings | 4,446 |
| Primary-document source-gap mappings | 4,803 |
| Unique ready content hashes | 4,119 |
| Unique catalog hashes including discovery cache | 4,284 |
| Content-level document deduplication savings | 162 |
| Content-ready tickers | 87 |

All 166 DP6Q discovery-cache bodies were reused. The three DP6Q inputs were
unchanged, and parser, feature, historical-materialization, calibration,
portfolio, and production invocation counts were zero.

The initial failure census was:

| Failure class | Requests |
| --- | ---: |
| Wayback or other retryable connection/read failures | 3,574 |
| HTTP 403 | 525 |
| Reviewed redirect required | 44 |
| HTTP 404 | 30 |
| Other non-retryable HTTP failure | 1 |

## Targeted recovery sequence

Recovery is deliberately source-lane specific:

1. Retryable archive failures use the unchanged capture URLs, a bounded worker
   pool, and a conservative per-domain start rate. Successes become normal
   validated cache hits on every later pass.
2. Diana Shipping, J.B. Hunt, Castor Maritime, and Globus Maritime 403s may use
   the reviewed browser-user-agent, issuer-origin referer, and same-session
   cookie preflight. Q4 CDN and SEC are not included because diagnostics did not
   prove that recovery method.
3. The 42 InvestorRoom/Squarespace asset redirects are approved only for their
   exact ticker, source-domain, and final-domain routes. The Expeditors Toyota
   page and Volaris sustainability page are explicit non-financial exclusions.
4. The canonical command is rerun without a retry flag after recovery. That run
   performs no network access for sealed successes or retained failures and
   recomputes the complete request, document, content, and gap artifacts.

Wayback diagnostics demonstrated a burst limit: a new service window recovered
most of its first small batch and then sharply degraded. The final retryable
lane therefore uses non-overlapping 20-request batches, a 60-second cooldown,
and atomic progress states of `RUNNING` and `COOLDOWN`. This is slower but avoids
repeating successful content or spending a continuous high-rate run on known
throttling.

## Final recovery reseal

The zero-network canonical reseal after all bounded recovery lanes records:

| Result | Count |
| --- | ---: |
| Content-ready requests | 6,562 |
| Terminal reviewed request exclusions | 2 |
| Failed requests | 1,840 |
| Content-ready document mappings | 7,095 |
| Primary-document source-gap mappings | 2,152 |
| Request-ready unique content hashes | 6,429 |
| Discovery-cache-only unique content hashes | 165 |
| Unique catalog hashes including discovery cache | 6,594 |
| Content-ready tickers | 98 |

This is the final `PASS_WITH_REQUIRED_RECOVERY` seal. It does not conceal the
remaining unavailable sources and does not itself authorize parsing. DP6S
separately proves that every residual ticker/metric pair is either covered by
an alternate ready document or terminal after the completed source search.

## Acceptance gates

DP6R can report `PASS` only when:

1. all 8,404 sealed requests have a terminal result;
2. every request is content-ready or explicitly excluded by reviewed policy;
3. every approved included document has a verified content hash and cache path;
4. every unique content hash maps to exactly one cache path;
5. request/document fanout and deduplication counts reconcile;
6. all protected source artifacts are unchanged;
7. the failure artifact is empty; and
8. parser, feature, history, calibration, portfolio, and production invocations
   remain zero.

`PASS_WITH_REQUIRED_RECOVERY` is a valid completed hydration run but does not
authorize parsing. It means every request was attempted and sealed, while one or
more approved documents still have explicit source gaps.

## Artifacts

The canonical output directory is
`output/industrials/transportation/dedicated_parser/2026-07-22` and contains:

- `transportation_primary_document_hydration_request_results.csv`;
- `transportation_primary_document_hydrated_manifest.csv`;
- `transportation_primary_document_content_catalog.csv`;
- `transportation_primary_document_hydration_failures.csv`;
- `transportation_primary_document_hydration_progress.json`; and
- `transportation_primary_document_hydration_manifest.json`.

Recovery diagnostics are isolated under `dp6r_diagnostics` and cannot overwrite
the canonical artifacts.

## Next gate

DP6S freezes residual dispositions and the exact content-hash delta; DP6T builds
the offline direct-document plan; DP6U extracts each unique physical body once;
and only a passing DP6U gate may authorize the resumable DP6V semantic run.
The complete sequence and acceptance gates are documented in
`DP6S_EFFICIENT_PARSER_BATCH.md`.
