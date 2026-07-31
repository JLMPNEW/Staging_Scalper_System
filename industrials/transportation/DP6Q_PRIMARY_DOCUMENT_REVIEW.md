# Transportation DP6Q Primary-Document Review

## Outcome

DP6Q passed for the 2026-07-22 point-in-time source census. It closes the
compact review gate between primary-document enumeration and the expensive
one-time hydration pass. It does not retrieve document bodies or authorize
the dedicated parser.

The gate reviews all 160 sealed issuer endpoints, including:

- 47 endpoints with no primary-document candidate after bounded discovery;
- 19 endpoints with candidates and one or more failed optional discovery
  branches;
- 12 endpoints with a reviewed primary-site or archive-index access
  limitation; and
- 130 issuer-linked document candidates on external domains.

## Implementation

The review implementation is split into:

- `primary_document_review.py`, which applies deterministic endpoint
  dispositions, validates the external-domain policy, adjudicates every
  document, and deduplicates physical hydration requests;
- `scripts/09e_review_transportation_primary_documents.py`, which verifies
  every DP6O input hash, runs the local review, writes atomic artifacts, and
  seals the DP6Q manifest; and
- `review_policies/transportation_external_asset_domain_policy.csv`, which
  contains 26 reviewed ticker/domain decisions covering all 130 external
  rows.

The policy treats the enumerated URL and issuer-page referrer as
`fact_source_reported`. Whether an external host is an acceptable primary
asset lane is a reviewed `analyst_interpretation`. A zero-document or
access-limited result remains `missing_required_source`; the implementation
does not synthesize a value or claim that a fallback was retrieved.

## Review Results

All 160 endpoints received an explicit disposition:

| Disposition | Endpoints |
| --- | ---: |
| Accept enumerated set | 82 |
| Accept with bounded discovery gaps | 19 |
| Accept with reviewed access limitation | 1 |
| Retain zero result after bounded discovery | 42 |
| Retain zero result with declared fallback lane | 5 |
| Retain zero result and require declared fallback lane | 11 |

The 130 external rows received these decisions:

| Decision | Documents |
| --- | ---: |
| Approve for one-time hydration | 111 |
| Exclude from hydration | 19 |

The excluded rows are social-media links, secondary media, an unrelated
academic-research link, or issuer-hosted governance/tariff assets outside the
specialized-metric scope. Issuer-controlled domains and the 407 already known
issuer asset-host rows remain approved without changing their prior source
classification.

## Frozen Hydration Scope

The reviewed document manifest preserves all 9,268 enumerated rows:

- 9,249 documents are included in the one-time hydration scope;
- 19 reviewed false positives are excluded;
- 166 document bodies already captured in the discovery cache are reused;
- 9,083 documents still require a body; and
- content-digest or canonical-URL fanout reduces those documents to 8,404
  physical hydration requests, a savings of 679 requests before final
  SHA-256 content deduplication.

Every approved row retains the complete applicable transportation parser
metric and supporting-operand scope. Search terms used to discover a document
do not restrict the later parse.

## Acceptance Gates

DP6Q passes only when:

1. the DP6O enumeration manifest and all five input artifacts match their
   sealed hashes and row counts;
2. all 160 endpoint rows reconcile, and every exceptional status has a
   deterministic reviewed disposition;
3. the 47 zero, 19 partial, and 12 access-limited queues reconcile exactly to
   DP6O;
4. every external ticker/domain key has one approved policy and no unused
   policy exists;
5. every one of the 9,268 documents is either included or explicitly
   excluded;
6. every approved uncached document maps to exactly one deduplicated hydration
   request;
7. hydration-request fanout reconciles to the approved uncached document
   count;
8. all approved documents retain `parse_all_applicable_metrics=1`; and
9. retrieval, parser, feature, historical-materialization, calibration,
   portfolio, and production authorization all remain false.

## Artifacts

The 2026-07-22 output directory contains:

- `transportation_primary_document_endpoint_review.csv`;
- `transportation_primary_document_external_domain_adjudication.csv`;
- `transportation_primary_document_reviewed_manifest.csv`;
- `transportation_primary_document_hydration_requests.csv`; and
- `transportation_primary_document_review_manifest.json`.

The manifest records `PASS` and sets the next gate to
`HYDRATE_HASH_AND_CONTENT_DEDUPLICATE_PRIMARY_DOCUMENTS_ONCE`.

## Next Gate

The hydration implementation must consume only the hash-sealed 8,404-request
manifest, reuse the 166 cached bodies, validate content type and size, compute
SHA-256 for every successful body, content-deduplicate the resulting corpus,
and preserve fanout to all approved document IDs. It must be restartable and
must not invoke the parser until the complete hydrated corpus and any explicit
retrieval failures are sealed.
