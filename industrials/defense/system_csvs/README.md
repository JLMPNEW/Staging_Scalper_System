# Defense System CSVs

This folder is for defense pipeline input CSVs and manually maintained seed CSVs.

Generated results do not belong here. Defense dashboard outputs should be written under:

```text
output/industrials/defense/dashboard/YYYY-MM-DD/
```

Expected pipeline CSVs:

- `defense_tickers.csv`
- `defense_historical_membership.csv`
- `aerospace_defense_delisted.csv`
- `defense_ticker_aliases.csv`
- `defense_cik_ticker_overrides.csv`

The enriched `ticker_mapping/defense_tickers.csv` should be copied here before Stage 2 universe loading. The delisted calibration seed `ticker_mapping/aerospace_defense_delisted.csv` should also be copied here before Stage 2B/Stage 8 research work. After validation, this folder is the canonical input location for the industrials defense pipeline.

`defense_cik_ticker_overrides.csv` is only for documented identity exceptions. Normal rows should reconcile through SEC bulk files, EDGAR search, exchange directories, or Norgate resolution.
