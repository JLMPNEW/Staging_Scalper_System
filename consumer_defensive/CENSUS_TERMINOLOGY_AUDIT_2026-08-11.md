# Consumer Defensive Census Terminology Audit

Audit date: 2026-08-11  
Status: complete; deterministic review pack adjudicated and post-remediation census v3 validated in the accepted fresh migration-v10 replay (does not certify Stage 6B numeric extraction)
Scope: discovery-only specialized-disclosure census terminology

## Purpose

This audit checks whether the Stage 4 phrase census is directionally useful across the reviewed Consumer Defensive taxonomy. It is limited to search-term quality and document availability. It does not extract numeric specialized metrics, adjudicate units, promote a parser result, assign a model weight, or certify Stage 6B.

The `2026-08-11` Stage 4 semantic rebuild is a legacy pre-hardening baseline. It must not be used as current acceptance evidence for the migration, exact-scope, lifecycle, immutable-seal, path-containment, or chronological-watermark contracts. The terminology sample was adjudicated against the exact retained `2026-08-10` v5 seal on the disposable migration-v10 continuation and then rerun under census parser v3. A separate fresh chronological migration-v10 replay from an empty database reproduced the current census-v3 rows and passes all 40 Stage 4 checks. The terminology review remains distinct from numeric specialized-metric extraction and Stage 6B promotion.

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

1. The legacy production census and source registry assigned discovery rows to `consumer_defensive_disclosure_census`; `shared_dedicated_sec_parser` remains a planned Stage 6B metric source. The hardened shared-parser intake boundary is not specialized-metric output. Current provenance and seal checks pass in the fresh chronological replay.
2. The legacy report passed 19 of 20 then-current checks and reported Companyfacts coverage of 113 of 119 profiles. The fresh migration-v10 replay now passes all 40 current checks. BTI and BUD were proven to be nonfinancial metadata-only 6-K anchors; FMX, JBS, KOF, and UL were covered by the sector-owned exact-seal inline-XBRL fallback. The terminology sample is adjudicated, but neither result enables Stage 6B promotion.
3. No terminology hit or miss has been accepted as a numeric specialized observation. All candidate specialized metrics remain nonproduction and zero-weight.
4. The fresh migration-v10 replay contains 2,152,806 raw facts, 231,024 canonical facts, 49,867 FX rows including 52 quarantined, 952 selected hydrated documents, 4,522 census-summary rows, and 779 post-remediation census-evidence rows. It reproduced the exact 1,287-file v5 SEC seal and did not modify production or the preserved v5 evidence database. Current census-v3 semantic hashes exactly match the retained continuation; the latter additionally preserves older v2 census history and four superseded document-association rows.
5. `08c_build_consumer_defensive_census_review_pack.py` generated the initial deterministic 10-row census-v2 sample from 1,067 applicable candidates with SHA-256 `8087698405cf805dfe9b4f0cfaf72ba9edea3f2f515ad965e5fb60b9c6060275`. Manual review accepted six true negatives with `no_change`, three true positives with `retain`, and one true positive with a `narrow` action. The broad standalone `sales leaders` trigger was removed while `active representatives` and `active distributors` were retained. Census parser/version v3 was rerun from the exact seal: 952 documents parsed, 4,522 summaries and 779 evidence rows were written with zero failures. The post-remediation 10-row sample has SHA-256 `938ce70bf9151986e69c3664df15eb1cb585443cf0d8a543eff5d60e17f0071b`; its completed ledger has SHA-256 `47938bc357c252d79150e0c3a1ba8f59a7399e3436b3aaa9e6a3bd1fa8c1dd61` and validates `PASS` with six true negatives, four true positives, six `no_change`, and four `retain` actions.
6. The canonical foreign-key child index, exact raw delete index, and bulk insert remediate the identified query shape. The historical 48.2-second optimized run predates current full sealing/reconciliation and is not a current end-to-end performance claim.
7. Stage 6B still needs a complete PIT historical filing/document inventory back to `2019-01-02`. A current-date census seal cannot be extrapolated into historical parser readiness.

## Review Ledger

The rebuilt census and completed v3 ledger are available. The post-remediation ledger is `ADJUDICATED` and validates against the exact regenerated evidence keyset.

| Cohort | Positive reviewed | Negative reviewed | Foreign/historical coverage | Status |
|---|---:|---:|---|---|
| Beverages | 1 true positive | 2 true negatives | foreign included | reviewed |
| Consumer Staples Distribution & Retail | 1 true positive | 2 true negatives | historical included | reviewed |
| Household Personal & Tobacco | 1 true positive | 1 true negative | historical included | reviewed; broad trigger narrowed |
| Packaged Foods & Agricultural Products | 1 true positive | 1 true negative | historical included | reviewed |

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

The terminology adjudication, reviewed term remediation, fresh chronological replay, production rollout, and current validator are complete. The former Companyfacts/inline-XBRL code gap is resolved. Production was migrated only after a verified backup-only rehearsal; its `2026-08-11` census has 4,522 current v3 summaries and 778 evidence rows with zero parse failures, and the live validator passes 40/40. The approved terminology decisions remain bound to the exact `2026-08-10` sample and were not silently reassigned to the one-row-different production census. This audit is complete as a terminology and discovery-coverage artifact, but it does not certify Stage 6B numeric extraction. Stage 5 may now begin.
