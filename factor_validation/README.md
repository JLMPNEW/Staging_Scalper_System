# Shared factor validation kernel

`factor_validation_v1` is the production-neutral statistical foundation for factor evidence across
all sector pipelines and Portfolio Layer.

The package currently provides:

- per-date, cross-sectional, average-rank Spearman IC;
- independent-window-deflated inference as the conservative default promotion p-value;
- diagnostic Newey-West inference with entry-lag-aware overlap lags, minimum observed cadence,
  explicit lag-truncation metadata, and no p-value below the five-times-lag sample floor;
- recorded minimum/median/maximum business-day gap distribution for mixed-cadence auditability;
- chronological-half and regime sign stability;
- tie-group-boundary quantiles that keep tied extrema in the extreme buckets;
- gross and measured-two-leg-turnover-adjusted net top-minus-bottom spread;
- consecutive-period rank persistence and top-bucket Jaccard turnover;
- fail-closed Benjamini-Hochberg families sealing family ID, exact membership, and alpha together;
- strict ISO dates, finite intermediate statistics, and NumPy-safe JSON evidence conversion.
- hash-sealed campaign/FDR registration with exact factor/horizon membership;
- deterministic CSV/JSON evidence packages with source, config, and code provenance;
- fail-closed statistical acceptance, atomic publication, tamper verification, and append-only supersession.

## Stage 2 evidence layout

`write_evidence_package` publishes one immutable directory at:

```text
<output_root>/<campaign>/<sector>/<factor>/<horizon>d/<cell_id>/
    acceptance.json
    campaign_registry.json
    fdr_family.json
    per_date_ic.csv
    quantile_diagnostics.csv
    summary.json
    manifest.json
```

`register_campaign` must first publish `<output_root>/<campaign>/campaign_registry.json`. Reusing
that campaign ID with changed membership, alpha, configuration, source, or code seals fails closed;
evidence publication is refused when the independent pre-registration is absent or different.
The writer also requires `ObservedProvenance` captured at runtime and refuses publication unless its
config, source, and code hashes exactly match the registered cell.

Every content file is hashed by `manifest.json`. The manifest also seals the shared contract,
campaign and cell registrations, FDR family/alpha/membership digest, declared direction and cadence,
configuration digest, source/code file digests, observed cadence, row counts, terminal state, and any
superseded manifest digest. JSON is canonical with `allow_nan=False`; CSV schemas and date sets are
verified explicitly.

Publication is built and verified in a sibling draft directory, then exposed by a single directory
rename. Existing package paths are never overwritten. A replacement uses a new campaign ID and
records the prior manifest digest; the prior accepted package remains byte-for-byte unchanged.

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

Pooled Pearson, mutual information, bootstrap inference, empirical horizon half-life, real campaign
registrations, sector adapters, and orchestrator integration remain intentionally outside Stage 2.
