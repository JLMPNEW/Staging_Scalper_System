# Consumer Defensive v2 implementation path

Status date: 2026-08-27

This document is the authority for Consumer Defensive calibration and promotion
work after retirement of the legacy prospective-evidence and Stage12 protocol.
Legacy completion-roadmap sections describing those routes are historical only.
This document records the completed v2 architecture, the latest independently
validated report-only evidence, and the controlled handoff sequence. Validated
evidence is not production or Portfolio Layer activation.

## 1. Sector isolation: complete

- Consumer Defensive remains a self-contained package and database owner.
- Thirty-seven legacy Consumer future-evidence/Stage12 files were moved to
  `archive/consumer_defensive_legacy_future_evidence_20260826`.
- The archive is recoverable and hash-censused in `MANIFEST.json`; it is not
  admissible v2 evidence.
- Active Consumer code has no imports from Biotechnology, Transportation,
  Technology, Medical Devices, or `future_only_evidence`.
- Consumer branches were removed from `future_only_evidence`; its Transportation
  builders and regression tests remain active.
- The Portfolio adapter no longer imports the retired shared activation package.
  A legacy Consumer `promoted` row fails closed instead of silently activating.

## 2. Frozen shared-service boundary: complete

The authoritative contract is
`data/consumer_defensive_shared_service_contract_v1.yaml`.

Approved code services are `dedicated_parser` and `factor_validation`. Approved
platform services are the global orchestrator and `portfolio_layer`. The ticker
mapping is read as immutable reference data. SEC insider and market-positioning
SQLite databases are read-only inputs. SEC EDGAR, Yahoo Finance, and Norgate
remain approved providers. Macro, risk, valuation, optimizer, Black-Litterman,
and execution services are reached only downstream through `portfolio_layer`.

This boundary preserves shared infrastructure without sharing sector strategy,
calibration, promotion, or database ownership.

## 3. Consumer-owned v2 framework: complete

The v2 framework consists of:

- `core/promotion_framework_v2.py` and its frozen YAML policy;
- `core/calibration_v2.py` for nested purged walk-forward folds, label-completion
  purge, embargo, paired net-alpha lower confidence bounds, absolute/relative/
  robust profit factor, deflated Sharpe ratio, probability of backtest
  overfitting, drawdown, expected shortfall, transaction costs, liquidity,
  turnover, and concentration;
- independent decisions for beverages, distribution/retail,
  household/personal/tobacco, and packaged foods/agriculture;
- a one-tier-at-a-time state machine:
  `benchmark_production -> active_pilot -> active_scaled -> active_full`, with
  rollback on failed active evidence; and
- minimum winning and losing observation counts so an all-winning sample cannot
  create an artificial infinite-profit-factor pass.

`scripts/27_run_consumer_defensive_v2_foundation.py` remains the non-activating
global-orchestration status entrypoint. It cannot write Portfolio Layer ranks or
allocate active capital.

## 4. Point-in-time calibration inputs: complete

- `core/historical_features_v2.py` reconstructs a fresh point-in-time core
  feature panel from the sealed Stage 6C membership, frozen market-data policy,
  point-in-time financial observations, positioning history, and Stage 7 core
  feature definitions. It does not reuse the burned legacy Stage 8 panel.
- `core/institutional_history_v2.py` reconstructs historical 13F features from
  first-filed manager reports available by each point-in-time deadline. It
  excludes later amendments/backfills, put/call positions, and non-common-share
  rows, deduplicates manager-period filings, and hashes the resulting snapshot.
- Specialized metrics may receive nonzero candidate weights only for the exact
  scope, horizon, and direction accepted by the sealed `factor_validation`
  campaign. An unaccepted cell remains zero-weight; extraction coverage alone
  never authorizes a specialized factor weight.
- Source identifiers, row counts, policy hashes, and feature-panel hashes are
  carried into the immutable evidence chain.

## 5. Process-separated preregistration and execution: complete

### Label-blind preregistration

`scripts/28_preregister_consumer_defensive_calibration_v2.py` is a separate,
label-blind process. It requires explicit `--db`, `--factor-root`, `--asof`, and
`--stage6c-run-id` inputs. The database is opened with SQLite URI `mode=ro` and
`PRAGMA query_only=ON`. Only non-label Stage 6C metadata is read before the
candidate search space is frozen.

The script verifies the promotion framework, shared-service contract, sealed
factor campaign, and methodology file hashes, then publishes an immutable,
versioned pair in a content-addressed directory:

- `consumer_defensive_calibration_candidate_registry_v2.json`
- `consumer_defensive_calibration_preregistration_v2.json`

The pair records `forward_label_accessed=false` and disables database,
production, and Portfolio writes. `--dry-run` performs validation and builds the
payloads without publishing them.

### Label-access report-only execution

`scripts/29_run_consumer_defensive_calibration_v2.py` is the only sequence-1
entrypoint that opens the forward labels. Before opening the database, it loads
and validates an existing immutable candidate-registry/preregistration pair and
rechecks the framework, shared contract, factor campaign, and code bindings.
Its explicit `--db` is also opened with `mode=ro` and `query_only`; explicit
`--factor-root` and `--prereg-root` inputs are required.

The script runs the preregistered search in report-only mode. It performs no
Consumer database write, no Portfolio Layer write, and no production activation.
`--dry-run` executes the complete calibration without publishing artifacts.

## 6. Return-path and terminal-event accounting: complete

The two performance views have distinct roles and must not be conflated:

- Horizon-specific forward labels select candidates and measure the registered
  21-, 63-, and 126-session relative objectives. Overlapping forward labels are
  research observations, not a trade-P&L identity.
- Absolute profitability is measured from the daily realized portfolio path.
  Each monthly signal opens equal-weight long/short sleeves after the registered
  entry lag; those sleeves are bought and held until the next selected monthly
  signal rebalance. The final signal is carried for 21 sessions.

The realized path applies the reviewed Consumer terminal-event policy. Entry
marks must be observed; internal missing sessions carry the last observable
economic mark; reviewed terminal events or successor transitions determine the
economic value per original share; and an unverified event crossing fails
closed. A zero recovery is permitted only for a verified wipeout. Terminal-event
policy, reviewed event data, and implementation hashes are sealed into the
preregistration methodology contract.

## 7. Cryptographically bound evidence and independent validation: complete

Script 29 publishes exactly six immutable, self-hashed report artifacts:

1. `consumer_defensive_calibration_input_manifest_v2.json`
2. `consumer_defensive_calibration_fold_registry_v2.json`
3. `consumer_defensive_calibration_realized_path_attestation_v2.json`
4. `consumer_defensive_calibration_results_v2.json`
5. `consumer_defensive_calibration_decision_v2.json`
6. `consumer_defensive_calibration_independent_validation_v2.json`

The artifacts are cross-bound to the preregistration, candidate registry, input
manifest, fold registry, realized-path attestation, decision, framework, and
methodology-code hashes as applicable. The sixth file is the execution process's
validation attestation; it does not replace a separate-process review.

The command
`scripts/26_validate_consumer_defensive_promotion_framework_v2.py --evidence-root <script-29-output-directory>`
is the independent file-level validator. It safely reloads all six files,
verifies every self-hash and cross-hash, revalidates the decision under the
frozen framework, and independently recomputes every realized-path row's
position, cash, market value, NAV, gross return, and net return identities
without calling the execution validator. Its original `--decision`
contract-validation mode remains available.

## 8. Latest validated Sequence 1 evidence

Validation date: 2026-08-27. Calibration as-of date: 2026-08-14.

- Preregistration SHA-256:
  `069e3ef0641d1a1c55f1e50fa88d40ad2b3a8090fd5433d461a710210fde351c`
- Candidate-registry SHA-256:
  `56287d481f537f25d105da7bc4846ffdf041aa804e212e70e656bc53fe9583ee`
- Decision SHA-256:
  `4e5de92ea43814982cd540c8d2ae760f7cef1f25f0e2fe1af1c2f1285c200df2`
- Fold-registry SHA-256:
  `2f17af8d98d0ccf141adf5c3a306c045aa876cf76071ca946146429389334b44`
- Realized-path-attestation SHA-256:
  `22a363db9e36185ef8c024284c710ae9e080d9336423805b4578160bbdec9fb7`
- Results SHA-256:
  `d26a54519a60d188675de8fa7a49a68b49ec3c060ed0840cfb9bfcdd1cc8fe06`
- Evidence directory:
  `output/consumer_defensive/framework_v2/sequence1/2026-08-14/069e3ef0641d1a1c`

External script-26 evidence validation passed all file, self-hash, cross-hash,
decision, and independently recomputed path-identity checks across 8,295 path
rows. The validated artifacts report `production_write_performed=false`,
`portfolio_write_performed=false`, and `recalibration_required=false`.

All four cohort states are `benchmark_production`. Failed-gate counts are:

- beverages: 12
- distribution/retail: 3
- household/personal/tobacco: 8
- packaged foods/agriculture: 24

`benchmark_production` is benchmark-only, zero-cap, and non-active. The validated
decision therefore does not authorize an active promotion or Portfolio handoff.

The earlier `ec434c` run is retained as audit history only. It was rejected for
a candidate-hash cross-binding failure. The corrected code was re-preregistered
and re-executed to produce the validated evidence listed above; the rejected run
is not admissible promotion evidence.

## 9. Production status: disabled and zero-cap

Consumer Defensive remains disabled, non-required, and zero-cap in its active
configuration. `benchmark_production` is a benchmark-only state, not permission
to allocate capital. Neither script 28, script 29, report publication, nor a
passing report-file validation can change that state or authorize Portfolio
Layer activation.

The validated Sequence 1 result is evidence for the controlled decision process,
not activation. Any later state change must pass the explicit controls below.

## Validated handoff order

1. **Complete:** Script 28 published the label-blind, immutable, versioned
   candidate registry and preregistration pair for the selected Stage 6C run.
2. **Complete:** Script 29 ran against that exact pair and published the six
   report-only evidence artifacts without database or Portfolio writes.
3. **Complete:** Script 26 independently validated the exact evidence directory,
   including all 8,295 realized-path rows. Cost, leakage, capacity,
   concentration, and terminal-event evidence are bound into the reviewed set.
4. **Conditional; not authorized by the current decision:** Only a cohort that
   qualifies under a validated v2 decision may receive a Consumer-owned v2
   activation-registry record. All current cohorts remain benchmark-only.
5. **Conditional; not authorized:** Publish a dated Consumer handoff file and
   manifest only after a qualifying activation-registry record exists.
6. **Conditional; not authorized:** A Portfolio-owned validator, without
   importing Consumer strategy code or reading the Consumer database, must then
   verify the registry, handoff, and configured cohort/sector caps.
7. **Conditional; not authorized:** Rehearse migration and rollback against a
   backup copy and preserve the resulting evidence.
8. **Conditional; not authorized:** Require explicit approval and configuration
   activation before Portfolio Layer consumption or any active allocation.
   Report generation alone never promotes the sector.
