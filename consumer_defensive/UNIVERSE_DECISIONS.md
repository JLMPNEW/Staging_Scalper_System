# Consumer Defensive Universe Decisions

Status: adopted for Stage 2 implementation

## Binding decisions

1. Consumer Defensive is an independent top-level package, database, orchestration lane, parser adapter, factor-validation adapter, and Portfolio Layer adapter. It imports no sector package.
2. The authoritative live universe is `ticker_mapping/consumer_defensive.csv`. Its calibration cohorts are Beverages; Consumer Staples Distribution & Retail; Household, Personal & Tobacco; and Packaged Foods & Agricultural Products.
3. `recognized_membership_required: true` is a hard point-in-time rule. A security is eligible on a date only when Norgate shows it in at least one approved index on that date.
4. The approved indices are Russell 3000, S&P Composite 1500, NYSE Composite, and Nasdaq Composite. Eligibility uses their union; membership in all four is not required.
5. Norgate is authoritative through three explicit database surfaces: `US Equities` and `US Equities Delisted` for provider asset/listing identity and adjusted/total-return history, and `US Indices` for point-in-time membership. Major-exchange status is provider-authoritative. XLP, IYK, and FSTA holdings validate the current surface only and must never be backfilled as historical membership.
6. The reviewed 2026-08-10 Norgate preflight found all 108 then-current securities in at least one approved index. That dated result is a reference baseline, not a substitute for the final isolated Stage 2 replay. Historical dates before admission are intentionally excluded.
7. Current-universe replay is not survivorship-correct. Historical calibration uses exact membership intervals, delisted securities, and verified terminal events. Unresolved terminal outcomes remain visible in audit coverage but are not calibration-eligible.
8. `CCE` is a historical ticker in the continuous `CCEP` security lineage, and `DPS` is a historical ticker in the continuous `KDP` lineage. Neither is loaded as a separate delisted calibration security. Both are ticker aliases/security events.
9. `CENTA`, not `CENT`, is retained. The verified 63-session median dollar volume was approximately $10.8 million for CENTA versus $3.2 million for CENT, and Central designed the non-voting class to enhance liquidity.
10. Del Monte Corporation (CIK `1047340`) uses its NYSE ticker `DMC` from 2026-06-29. Historical ticker `FDP` remains a predecessor alias through 2026-06-26 on continuous Norgate asset `132283`. Unrelated DMC Global remains ticker `BOOM`, CIK `34067`, and is out of scope.
11. `VLGEA` is the verified Nasdaq Class A ticker for Village Super Market; `VLGA` is not a valid listed ticker.
12. `LMNR` remains outside the investable universe while its reviewed 63-session median dollar volume is below the configured support floor. `YSWY` remains outside the calibration universe until it has enough point-in-time history for the frozen 126-day momentum and 252-day risk features.
13. `country` in the existing live CSV is listing country. It must not be silently treated as issuer domicile; issuer domicile requires issuer-level evidence.
14. Security types normalize to `Common Stock`, `Ordinary Shares`, or `ADR/ADS`. Duplicate tickers, duplicate provider assets, overlapping alias intervals, cross-cohort assignments, and multiple live primary listings fail closed.

## Stage 2 acceptance

- Load and validate the 110-row live security master and taxonomy.
- Load ticker aliases and security events without creating duplicate securities.
- Resolve Norgate asset IDs and persist approved-index point-in-time membership.
- Require the loaded current and historical candidate sets to match their reviewed inputs and terminal-event scope exactly.
- Preserve exact provider symbols, including punctuation-bearing share classes.
- Fence `US Equities`, `US Equities Delisted`, and `US Indices` at catalog, candidate, and final reads; any drift publishes neither database rows nor reports.
- Prove every live name has approved membership as of the run date.
- Preserve delisted history only when provider identity, dates, membership, prices, and terminal treatment are reconciled.
- Produce complete cohort/date breadth diagnostics, including explicit zero-name combinations, before Stage 3 market-data ingestion.
