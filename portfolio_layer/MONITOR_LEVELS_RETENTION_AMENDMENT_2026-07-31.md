# Monitor and Levels Provisional Retention Amendment

Issued: 2026-07-31

Amends:

- `MONITOR_LEVELS_IMPLEMENTATION_PLAN.md`
- `MONITOR_LEVELS_PROVIDER_AMENDMENT_2026-07-31.md`

Status: FROZEN FOR IMPLEMENTATION

This amendment records the operator's decision to begin private, local, provisional retention of
normalized FMP and Alpha Vantage observations while provider-specific retention language remains
under review. It supersedes earlier language requiring written clarification before any normalized
snapshot is retained.

## Authorized retention

- Normalized estimate and revision fields may be stored locally in
  `db/expectations_monitor.sqlite`.
- Internal derived signals may be stored only for shadow research and monitoring.
- Provider values remain provider-specific and may not be averaged without a separately validated
  reconciliation contract.
- Retrieval timestamps, endpoint IDs, fiscal periods, source hashes, normalization hashes,
  entitlement versions, and retention classes are mandatory.

## Prohibited retention and disclosure

- Complete raw API payloads are not retained. Only their SHA-256 digest is stored.
- Credentials, rendered authenticated URLs, account information, implementation details, portfolio
  policies, and internal artifacts are never sent to providers or any other external party.
- Vendor observations and derived outputs are private and may not be redistributed, displayed,
  sold, or provided to third parties.
- No provider data may alter Stage 1 scores, target books, orders, or broker positions.

## Deletion and invalidation

Every retained snapshot is covered by provider/date purge tooling. Exact snapshot dependencies are
recorded for downstream artifacts. A purge must:

1. require a non-empty operator reason;
2. support a no-write dry run;
3. invalidate every dependent artifact before deleting source snapshots;
4. preserve dependency tombstones and an append-only purge event;
5. force regeneration before any invalidated artifact is relied upon.

## Provider roles

- FMP is the primary estimate provider because it covered 50/50 tested symbols.
- Alpha Vantage is a secondary revision and disagreement source because it covered 40/50 while
  providing additional 7/30/60/90-day revision fields.
- Missing secondary-provider data is explicit missing coverage, never a neutral revision.
- All provider-derived monitor behavior remains shadow-only until prospective evidence passes.
