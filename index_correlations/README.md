# Index correlations

This pipeline publishes rolling correlations for 11 sector and broad-market
ETFs. It is a derived-analytics consumer of the portfolio layer's existing
Stage 2 Norgate cache. It does not fetch market data and does not persist a
second copy of prices or returns.

## Source contract

The only production inputs are:

- `portfolio_layer/output/cache/norgate_market_instruments.sqlite`
- `portfolio_layer/output/cache/norgate_market_instruments_manifest.json`

Before calculation, the pipeline:

1. requires an accepted Stage 2 manifest that is at least as current as
   `--as-of`;
2. verifies the manifest points to the exact SQLite file being read;
3. reconciles every ETF's row count and SHA-256 content hash;
4. requires total-return-adjusted observations for every ETF on the exact
   requested date;
5. requires identical calendars across all ETFs, without forward filling or a
   silent inner join; and
6. requires enough verified history for the largest configured window.

The ETF set is XBI, IHI, SOXX, IGV, XLK, XAR, XLI, IYT, XLP, SPY, and QQQ.

## Run

From the repository root:

```powershell
python -m index_correlations.pipeline --as-of 2026-09-03
```

An intentional same-date rebuild requires `--force`. A normal same-input
rerun verifies the existing output hashes and returns `UP_TO_DATE`.

## Outputs

Artifacts are date-partitioned under
`output/index_correlations/YYYY-MM-DD/`:

- `rolling_pearson_{90,120,250}.csv`
- `rolling_kendall_tau_{90,120,250}.csv`
- `latest_correlations.csv`
- `source_coverage.csv`
- `correlation_validation.csv`
- `correlation_manifest.json`

The manifest records the source manifest hash, per-series source and as-of
hashes, pipeline code hash, validation gates, and hashes for every output.
There are no raw-price, aligned-price, or return CSVs.

The Streamlit page discovers the newest ISO-dated directory and validates the
manifest, current pipeline hash, complete output set, and every output hash
before displaying a rolling matrix. It does not fall back silently when a
newer dated publication is incomplete or invalid.

## Orchestration

`orchestration/registry.yaml` registers `index_correlations` as a required,
non-network Tier 1 job immediately after `portfolio_layer`. This ordering is
intentional: the portfolio run refreshes and seals the Stage 2 cache first,
then correlations consume that same cache in read-only mode. A stale,
incomplete, restated, or mismatched source fails closed.
