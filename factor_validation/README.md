# Shared factor validation kernel

`factor_validation_v1` is the production-neutral statistical foundation for factor evidence across
all sector pipelines and Portfolio Layer.

The package currently provides:

- per-date, cross-sectional, average-rank Spearman IC;
- independent-window-deflated inference as the conservative default promotion p-value;
- diagnostic Newey-West inference with entry-lag-aware overlap lags, minimum observed cadence,
  explicit lag-truncation metadata, and no p-value below the five-times-lag sample floor;
- recorded minimum/median/maximum business-day gap distribution for mixed-cadence auditability;
- fail-closed transition diagnostics for ambiguous mixed-cadence panels unless a transition cadence
  is explicitly declared;
- chronological-half and regime sign stability;
- tie-group-boundary quantiles that keep tied extrema in the extreme buckets;
- gross and measured-two-leg-turnover-adjusted net top-minus-bottom spread;
- consecutive-period rank persistence and top-bucket Jaccard turnover;
- fail-closed Benjamini-Hochberg families sealing family ID, exact membership, and alpha together;
- strict ISO dates, finite intermediate statistics, and NumPy-safe JSON evidence conversion.
- hash-sealed campaign/FDR registration with exact factor/horizon membership;
- deterministic CSV/JSON evidence packages with real-file source/config/code provenance and complete
  `FactorValidationConfig` sealing;
- a root-level, fsync'd, hash-chained campaign ledger plus persisted head anchor;
- fail-closed statistical acceptance, atomic per-package publication, ledger-backed tamper verification, and
  reachable append-only supersession.

## Stage 2 evidence layout

`write_evidence_package` publishes one immutable directory at:

```text
<output_root>/<campaign>/packages/<cell_id>/
    acceptance.json
    campaign_registry.json
    fdr_family.json
    per_date_ic.csv
    quantile_diagnostics.csv
    summary.json
    manifest.json
```

The compact physical path avoids Windows path-limit failures for long sector and factor IDs; the
full sector/factor/horizon identity remains sealed in the registry and manifest. Verification retains
read compatibility with the original descriptive path used by already-published campaigns.

`register_campaign` must first publish `<output_root>/<campaign>/campaign_registry.json`. It requires
a `ProvenanceFileSet` for every cell and hashes those concrete regular files itself; callers cannot
register or publish using precomputed provenance claims alone. Reusing
that campaign ID with changed membership, alpha, configuration, source, or code seals fails closed;
evidence publication is refused when the independent pre-registration is absent or different.
The writer hashes the same concrete files again at publication and also requires the runtime
`FactorValidationConfig` to equal the complete sealed configuration.

Every registration and publication attempt is appended to:

```text
<output_root>/factor_validation_campaign_ledger.jsonl
<output_root>/factor_validation_campaign_ledger.head.json
```

Each event includes the prior-event digest. Successful publication events bind the manifest digest,
cell, terminal state, environment, complete family p-vector, package path, and supersession pointer.
Campaign-level reconciliation reports can be bound through `campaign_report_published` events, so a
report is verified against the external ledger rather than trusting its own files. The ledger rejects
unknown event/state domains and more than one active publication for the same logical cell; the
active predecessor is rechecked while holding the ledger lock at the publication commit point.
Public package verification requires a matching successful event. `verify_campaign_ledger` also
detects deleted packages, modified manifests or registries, incomplete FDR-family publication,
dangling/cross-cell supersession, interrupted attempts, and chain/head tampering. Head replacement
retries transient Windows/OneDrive locks. A stale derived head can be repaired only through explicit
`repair_campaign_ledger_head` after the full append-only chain validates.

Every content file is hashed by `manifest.json`. The manifest also seals the shared contract,
campaign and cell registrations, FDR family/alpha/membership digest, declared direction and cadence,
the complete validation configuration and digest, source/code file digests, Python/NumPy/SciPy/platform
provenance, observed cadence, row counts, terminal state, and any superseded manifest digest. Manifest
keys are exact-schema. JSON is canonical with `allow_nan=False`; CSV schemas and date sets are verified
explicitly.

Publication is built and verified in a sibling draft directory, then exposed by a single directory
rename. Existing package paths are never overwritten. A replacement uses a new campaign ID and
records the prior manifest digest; the prior accepted package remains byte-for-byte unchanged.
Deleting a prior package does not permit resubmission because its successful ledger event remains.

Public writers accept raw `FactorObservation` collections, never caller-constructed validation
results. They rerun the registered kernel configuration before deriving acceptance or publication
state. `write_evidence_package` is limited to single-member FDR families. Multi-member families must
use `write_evidence_family`, which validates all registered observations/configs/files before
publication, derives the complete sibling p-vector internally, and publishes the complete family.
Each package becomes visible atomically; if a process stops between members, the ledger remains
fail-closed as an incomplete family and a rerun reuses only byte-identical, ledger-anchored packages.
An irrecoverable partial code version requires an explicit append-only `family_abandoned` transition;
it is allowed only when incompleteness is the ledger's sole error and permanently blocks additional
publication into that campaign.
No public writer accepts a caller-supplied result or sibling p-vector. Minimum-evidence failures are
published as explicit `rejected` evidence with no promotion-facing p-value so failed attempts remain
auditable without becoming promotable.

`accepted` means the kernel evidence is eligible, has a promotion-facing p-value, passes its exact
pre-registered BH-FDR family, and has the registered IC direction. It is still not a sector promotion
decision: sector-specific magnitude, stability, capacity, and economic gates remain outside Stage 2.

## Safety boundary

This package has no sector imports, score mutation, portfolio integration, or sector promotion rules.
Its filesystem I/O is limited to an explicitly supplied evidence output root. Sector adapters must
construct point-in-time-safe observations and declare whether the supplied forward return is excess,
sector-residual, or beta-residual. Sector owners retain promotion authority. A result with
`evidence_eligible=true` means only that the minimum sample and selected inference are available; it
is not a promotion decision.

Pooled Pearson, mutual information, bootstrap inference, empirical horizon half-life, sector adapters,
and orchestrator integration remain intentionally outside Stage 2.

The local ledger is an on-disk trust anchor, not a remote signature or WORM store. It detects ordinary
file deletion, regeneration, and coordinated package tampering. An administrator who can rewrite the
ledger, its head, and every package can replace the entire local history; deployments requiring that
threat model should mirror or sign ledger heads in an external immutable system.
