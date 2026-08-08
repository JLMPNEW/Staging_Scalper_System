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

## Safety boundary

This package has no configuration loading, filesystem I/O, sector imports, score mutation, portfolio
integration, or promotion rules. Sector adapters must construct point-in-time-safe observations and
declare whether the supplied forward return is excess, sector-residual, or beta-residual. Sector
owners retain promotion authority. A result with `evidence_eligible=true` means only that the minimum
sample and selected inference are available; it is not a promotion decision.

Pooled Pearson, mutual information, bootstrap inference, empirical horizon half-life, artifact
writers, and sector adapters are intentionally outside this first implementation unit.
