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
- `defense_listing_dates.csv`
- `defense_cik_ticker_overrides.csv`
- `defense_norgate_symbol_overrides.csv`
- `defense_sec_reporting_overrides.csv`
- `defense_scoring_eligibility_policy.csv`

The enriched `ticker_mapping/defense_tickers.csv` should be copied here before Stage 2 universe loading. The delisted calibration seed `ticker_mapping/aerospace_defense_delisted.csv` should also be copied here before Stage 2B/Stage 8 research work. After validation, this folder is the canonical input location for the industrials defense pipeline.

`defense_listing_dates.csv` bounds each ticker's historical eligibility window. Historical feature builds must not include a ticker before `first_eligible_date` or after `last_eligible_date`, even when the current active universe CSV still contains the ticker.

`defense_cik_ticker_overrides.csv` is only for documented identity exceptions. Normal rows should reconcile through SEC bulk files, EDGAR search, exchange directories, or Norgate resolution.

`defense_norgate_symbol_overrides.csv` is for reviewed Norgate symbol lineage exceptions, including OTC migrations and exact delisted suffixes that cannot be inferred from the base ticker plus exit year.

`defense_sec_reporting_overrides.csv` is for audited SEC reporting edge cases such as recent IPO/development-stage rows, parent/segment rows, legacy archive-required delisted entities, non-filing rows, and foreign metadata-only issuers.

`defense_scoring_eligibility_policy.csv` defines the Stage 6 rank-ready and calibration eligibility treatment for each reporting profile and lifecycle-stage combination. It is a system contract, not a generated result.
