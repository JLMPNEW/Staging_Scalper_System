# Consumer Defensive Census Terminology Audit

Audit date: 2026-08-11  
Status: in progress; not a Stage 4 closure artifact yet  
Scope: discovery-only specialized-disclosure census terminology

## Purpose

This audit checks whether the Stage 4 phrase census is directionally useful across the reviewed Consumer Defensive taxonomy. It is limited to search-term quality and document availability. It does not extract numeric specialized metrics, adjudicate units, promote a parser result, assign a model weight, or certify Stage 6B.

The `2026-08-11` Stage 4 semantic rebuild is a legacy pre-hardening baseline. It must not be used as current acceptance evidence for the migration, exact-scope, lifecycle, immutable-seal, path-containment, or chronological-watermark contracts. Current sample adjudication must wait for the final isolated replay and use that replay's dated, hash-sealed artifacts. The terminology review remains distinct from Companyfacts coverage and from code acceptance.

## Required Evidence Boundary

- Configuration must pass the authoritative-input gate: the complete reviewed CSV inventory must match `data/authoritative_input_manifest.yaml` by exact repository-relative path, parsed record count, schema/review metadata, and SHA-256.
- Filing availability must be bounded by SEC acceptance datetime and the requested as-of date.
- A reproducible review uses SEC `--cache-only` input from an exact complete reconciliation whose current ingestion-config hash, issuer-scope hash, lifecycle identities, and cache manifest match the requested date. Missing required submission, Companyfacts, archive, or eligible filing-document cache entries are explicit sync failures, not acceptable hydration-only statuses.
- The census reads only document bytes selected into the immutable `sealed/YYYY-MM-DD` snapshot. It may not read mutable acquisition aliases or all documents accumulated for an accession.
- Seal objects are backed by the global immutable SHA-256 CAS, and the manifest records each logical path's byte count and SHA-256 plus the aggregate manifest hash. Canonical relative paths, nested SEC document names, symlink resolution, and filesystem containment must validate. Cache identity proves repeatability; it does not convert reconstructed evidence into strict OOS evidence.
- Reverse-time SEC mutation is prohibited. A historical census sequence starts from a fresh scratch database at the earliest date and advances chronologically with the SEC ingestion watermark.
- Only metrics applicable to the security's reviewed cohort and subtype may be searched.
- Discovery-census evidence must carry a census-specific source identity. It must not be represented as output from the deferred Stage 6B dedicated parser.
- Evidence locations may be hashed or excerpt-bounded; this review must not publish full filing text.
- `applicable_term_hit`, `applicable_no_term_hit`, `parse_unavailable`, `not_applicable`, and a manually adjudicated true negative remain distinct. A term hit is discovery evidence, not proof that a numeric metric was disclosed.

## Stratification Plan

The reviewed sample must cover the following dimensions where eligible documents exist:

| Dimension | Required coverage |
|---|---|
| Cohort | Beverages; Consumer Staples Distribution & Retail; Household Personal & Tobacco; Packaged Foods & Agricultural Products |
| Census outcome | phrase hit and applicable miss |
| Security role | active and historical/delisted |
| Reporting profile | domestic issuer and foreign private issuer/ADR |
| Form | annual and interim forms, including 20-F or 6-K where present |
| Metric family | demand/volume, pricing/mix, margin/cost, distribution/store, customer/channel, and leverage |
| Applicability | universally applicable, subtype-specific, and prohibited/not-applicable examples |

The target is at least one credible positive and one credible negative example per cohort. A missing stratum is documented as unavailable rather than populated with an inapplicable example.

## Adjudication Schema

Each sampled item must record:

| Field | Meaning |
|---|---|
| `asof_date` | Census cutoff used for the review |
| `ticker` | Reviewed security |
| `security_role` | Active or historical/delisted |
| `cohort_id` / `applicability_subtype` | Reviewed routing policy |
| `metric_id` | Candidate metric |
| `form` / `accepted_at` | Point-in-time document identity |
| `census_status` | Stored discovery outcome |
| `review_verdict` | true positive, false positive, true negative, false negative, unavailable, or not applicable |
| `term_action` | retain, narrow, expand, prohibit, or no change |
| `evidence_hash` | Stable evidence locator/hash |
| `review_notes` | Concise rationale without numeric parser adjudication |

## Open Findings And Remediation

1. The legacy production census and source registry assigned discovery rows to `consumer_defensive_disclosure_census`; `shared_dedicated_sec_parser` remains a planned Stage 6B metric source. The hardened shared-parser intake boundary is not specialized-metric output. Current provenance and seal checks must be reconfirmed by the final isolated replay before adjudication.
2. The legacy report passed 19 of 20 then-current checks and reported Companyfacts coverage of 113 of 119 profiles. Those results do not cover the current hardened checks. BTI, BUD, FMX, JBS, KOF, and UL remain the six known foreign-private-issuer/inline-XBRL fallback gaps unless the new replay proves otherwise. They are not census-term defects, canonicalization bugs, or shared-parser-kernel failures.
3. No terminology hit or miss has been accepted as a numeric specialized observation. All candidate specialized metrics remain nonproduction and zero-weight.
4. The legacy evidence boundary contained 2,147,828 raw XBRL facts, 230,720 canonical facts, 49,879 FX rows including 52 quarantined, 956 hydrated documents, 4,522 census-summary rows, and 781 census-evidence rows. Current counts are intentionally pending the hardened isolated replay.
5. The canonical foreign-key child index, exact raw delete index, and bulk insert remediate the identified query shape. The historical 48.2-second optimized run predates current full sealing/reconciliation and is not a current end-to-end performance claim.
6. Stage 6B still needs a complete PIT historical filing/document inventory back to `2019-01-02`. A current-date census seal cannot be extrapolated into historical parser readiness.

## Review Ledger

The rebuilt census is available. The ledger remains intentionally pending until the current hit/no-hit sample is manually adjudicated.

| Cohort | Positive reviewed | Negative reviewed | Foreign/historical coverage | Status |
|---|---:|---:|---|---|
| Beverages | 0 | 0 | pending | pending adjudication |
| Consumer Staples Distribution & Retail | 0 | 0 | pending | pending adjudication |
| Household Personal & Tobacco | 0 | 0 | pending | pending adjudication |
| Packaged Foods & Agricultural Products | 0 | 0 | pending | pending adjudication |

## Closure Criteria

This audit may be marked complete only when:

- the rebuilt Stage 4 structural and semantic validations pass under the current validator check set;
- census rows use the correct census-specific provenance;
- the reviewed SEC cache-only run has no missing-cache failures and its file/aggregate SHA-256 manifest is recorded;
- the census date has an exact current config/scope reconciliation, complete lifecycle identities, and a verified immutable seal, and every consumed document is selected from that seal;
- the stratified ledger is populated or unavailable strata are explicitly explained;
- every false positive/negative has a recorded retain/narrow/expand/prohibit decision;
- the disclosure-term registry is updated only through reviewed changes and the census is rerun after those changes;
- the final dated report, authoritative-input manifest hash, and SEC cache-manifest hash are recorded; and
- the document states explicitly that Stage 6B parser implementation and numeric metric promotion remain deferred.

Until then, Stage 4 remains open on hardened replay acceptance, any Companyfacts gaps confirmed by that replay, and this terminology audit. Stage 5 must not rely on this audit as a completed gate.
