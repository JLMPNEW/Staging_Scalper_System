# Transportation Investable Universe v3 — Immediate Batch

Status: implemented and validated  
Policy effective date: 2026-08-14  
Data cutoff: 2026-08-13

## Scope completed

This batch freezes an outcome-blind investable universe without using the
previous holdout results to select securities. The transportation research
catalog remains intact at 120 active tickers, while 40 tickers are eligible for
the redesigned model and 80 are explicitly retained as research-only.

The eligible groups are:

- `surface_freight_core`: 19
- `passenger_airlines`: 10
- `oil_tanker_operators`: 11

The policy, eligible rows, and explicit exclusions are stored in:

- `data/transportation_investable_universe_v3.yaml`
- `system_csvs/transportation_investable_tickers_v3.csv`
- `system_csvs/transportation_investable_exclusions_v3.csv`

## Data and identity gates

The active transportation catalog now contains 120 tickers. Together with 48
delisted identities, the historical store validates 168 identities: 167 with
usable price history and one approved delisted exclusion (`RRTS`). The
2026-08-13 historical-load audit passed with:

- 340,010 active price bars
- 184,706 delisted price bars
- complete `SPY`, `IYT`, and `XTN` benchmark coverage
- all required FX pairs current through the cutoff
- SEC filing coverage for all 168 identities
- raw XBRL coverage for all 168 identities

The selected-universe coverage audit is read-only and passed with 40 of 40
tickers raw-complete. It performs no historical reconstruction, calibration, or
production promotion.

## Tanker specialized-metric search

The tanker expansion uses a single exhaustive document census and one bounded
dedicated-parser run for 11 tickers and 16 direct specialized metrics. The
census contains 259 accessions and 304 documents with no missing documents.

Dedicated-parser run 98 completed all 259 accessions with zero failures. Its
521 shadow evidence rows contain:

- 12 accepted
- 297 review
- 212 rejected

After unioning accepted shadow evidence with the canonical store, zero tanker
metrics meet the unchanged breadth and depth gates. Therefore the parser audit
correctly leaves:

- historical reconstruction unauthorized
- calibration unauthorized
- production promotion unauthorized
- canonical candidate data unchanged

The next gate is
`REVIEW_SHADOW_EVIDENCE_AND_FREEZE_REALISTIC_METRIC_SET`. Repeating the same
document search is not an accepted next action; the captured evidence must be
reviewed first.

## Acceptance commands

```text
python industrials/transportation/scripts/15b_validate_transportation_historical_raw_load.py --config industrials/config.yaml --asof 2026-08-13
python industrials/transportation/scripts/36a_validate_transportation_investable_universe_v3.py
python industrials/transportation/scripts/36b_build_transportation_investable_v3_coverage.py --config industrials/config.yaml --asof 2026-08-13
python industrials/transportation/scripts/36f_audit_transportation_tanker_parser_coverage.py --config industrials/config.yaml --asof 2026-08-13 --run-id 98
```

All four gates must pass before the project can advance to a separately
authorized historical feature reconstruction and genuinely untouched
calibration window.
