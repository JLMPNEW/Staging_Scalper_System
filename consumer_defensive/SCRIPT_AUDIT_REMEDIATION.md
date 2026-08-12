# Consumer Defensive Script Audit and Remediation

Record type: historical point-in-time audit ledger. The current authoritative contracts are `README.md`, `STAGE_GATES.md`, `IMPLEMENTATION.md`, and the latest validated replay artifacts. Counts and pass statements below must not be treated as acceptance of later migration, scope, lifecycle, immutable-seal, path, or watermark hardening.

Audit scope: all 17 entry-point scripts in `consumer_defensive/scripts`, their
direct `consumer_defensive/core` dependencies, and the associated Consumer
Defensive tests. The audit covered command-line contracts, run tracking,
point-in-time behavior, failure propagation, artifact durability, database
mutation boundaries, historical reproducibility, and configured-database
integration.

## Corrected defects

### Entry-point and orchestration contracts

- Standardized strict ISO-date parsing and rejected inverted date ranges.
- Added ticker normalization/deduplication and fail-closed rejection of unknown
  targeted tickers, preventing successful no-op runs caused by misspellings.
- Reworked Stage 4 wrappers to validate their prerequisites, record run status,
  publish artifacts, and return nonzero status for partial or failed work.
- Corrected the combined market-data wrapper so Yahoo, Norgate, successor, or
  terminal-event failures cannot be hidden by a zero exit code.
- Corrected preflight and validation wrappers that reported warnings or partial
  provider failures as success.
- Added bounded readiness checks so an as-of run cannot be satisfied by future
  facts or documents.
- Made FX history truncation explicit: it is rejected unless the operator opts
  into partial history, and unsupported currency units or zero-row results fail.

### Point-in-time and survivorship correctness

- Bounded price-coverage and source-selection decisions to the requested
  historical window; future bars no longer make an old snapshot appear covered.
- Required source selections to match the exact requested as-of date, preventing
  stale/future selections from leaking into a historical snapshot.
- Limited selected securities to listing windows that overlap the requested
  history.
- Reconciled newly listed securities with the policy's first-snapshot minimum
  observation rule instead of applying the normal partial-history threshold.
- Limited historical SEC raw-fact replacement and canonical-fact rebuilding to
  facts accepted by the requested cutoff; later facts are preserved.
- Prevented an older historical filing from downgrading a company's stored
  latest filing profile.
- Versioned disclosure summaries by as-of date and migrated the legacy table in
  place without discarding existing rows.
- Bounded evidence deletion by cutoff, preserving evidence for later snapshots.
- Scoped Stage 4 validation to the configured parser version and requested as-of
  date, and ignored stale data for securities no longer in the taxonomy.

### Data integrity and failure safety

- Tightened Norgate binary-series validation to accept only exact numeric 0/1;
  fractional, null, and nonnumeric values are rejected instead of coerced.
- Enforced the recognized-membership and major-exchange preflight policies for
  candidate securities, including candidates with no qualifying listed days.
- Made report, price, JSON, membership, preflight, and Yahoo-cache writes atomic
  through temporary-file replacement.
- Contained Yahoo worker failures so all attempted tickers are reported and the
  ingestion run always closes as `partial` or `failed`, rather than remaining
  indefinitely `running`.
- Made the current universe CSV authoritative for active taxonomy membership:
  stale taxonomy rows are removed while company/security history is retained.
- Restricted the disclosure census to the requested tickers and made parsing
  unavailability or parse failures explicit build failures.
- Corrected the Stage 2 validator's run-stat contract and added an integration
  regression test for it.

## Regression coverage added

`tests/consumer_defensive/test_script_contracts.py` now covers all 17 script
help/import contracts, date and ticker validation, invalid Norgate binary data,
membership enforcement, unknown-target no-ops, Stage 2 and Stage 4 run closure,
parser-version isolation, disclosure-schema migration, atomic writers, Yahoo
worker exceptions, historical price isolation, stale selection isolation, and
authoritative universe reload behavior.

Existing tests were extended to prove that historical SEC and canonical builds
preserve future facts and that source selection is recomputed for each as-of
date.

## Verification results

- Consumer Defensive test suite: **43 passed**.
- Ruff static checks for Consumer Defensive code/tests: **passed**.
- Python bytecode compilation: **passed**.
- Configured Stage 2 identity validation: **passed** (108 active securities).
- Configured Stage 3 build and validation at 2026-08-11: **passed** (108
  eligible/features).
- Historical Stage 3 build and validation at 2019-01-02: **passed** (98 eligible
  features; the expected limited-history listing is policy-admissible).
- Configured Stage 4 financial build and validation at 2026-08-11: **passed**
  (119 taxonomy rows, 119 feature rows, 93 complete and 26 partial).

The remaining configured warnings are intentional data-policy diagnostics, not
script failures: early limited history for UTZ, WBA's terminal-event calibration
exclusion, and approved Norgate fallback behavior for JBS and MAMA.
