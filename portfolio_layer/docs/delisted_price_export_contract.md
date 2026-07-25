# Delisted Price Export Contract (Stage 11 survivorship panel)

The portfolio layer never reads a sector database (independence principle). To make the Stage 11
backtest panel survivorship-complete, each sector pipeline publishes its delisted/halted/acquired
price history as flat CSV exports under its own report tree. `backtest/15b_build_survivorship_panel.py`
consumes whatever exports exist and flags everything it cannot cover — labels computed from
incomplete names are marked, never silently survivor-biased.

## 1. Price export (required for survivorship completeness)

**Path pattern (globbed):** `output/<sector>_reports/market_data/*delisted_price_export.csv`
(e.g. `output/biotech_index_reports/market_data/biotech_delisted_price_export.csv`)

One row per ticker-day, covering each delisted name from at least the configured Stage 11
development-window start (or its listing date if later) through its final trading day:

| column | required | meaning |
|---|---|---|
| `ticker` | yes | contract ticker as it appeared in `stocks_scores` (uppercase) |
| `date` | yes | trading day, YYYY-MM-DD |
| `adjclose` | yes | split- AND dividend-adjusted close (total-return-consistent with Norgate/Yahoo adjusted) |
| `adj_open` | yes for execution tests | open adjusted by `adjclose / close`; never synthesized from a close |
| `adj_high` | yes for execution tests | high adjusted by `adjclose / close` |
| `adj_low` | yes for execution tests | low adjusted by `adjclose / close` |
| `close` | no | unadjusted close (audit) |
| `volume` | no | audit |
| `source_symbol` | no | provider symbol (e.g. `ANAC-201606`) |

Names also fetchable from Yahoo may overlap; the panel uses Yahoo as primary and fills gaps from the
export, warning when overlapping `adjclose` values disagree by more than the configured tolerance
(adjustment-policy mismatch).

The Stage 11 execution panel requires the adjusted OHLC extension. A close-only export remains
valid for survivorship and forward-close labels, but it is explicitly incomplete for `D+1`-open
execution and must not be used to infer an entry price.

## 2. Delisting events (required for the `survivorship_complete=1` flag on ended names)

**Path pattern:** `output/<sector>_reports/market_data/*delisting_events.csv`

One row per delisted ticker:

| column | required | meaning |
|---|---|---|
| `ticker` | yes | contract ticker |
| `delist_date` | yes | last trading day (YYYY-MM-DD) |
| `delist_reason` | no | `acquired` / `merged` / `bankrupt` / `delisted_exchange` / ... |
| `terminal_value` | no | per-share cash/stock consideration when acquired (forward-return truth) |

A name whose price series simply *stops* is indistinguishable from a data hole; only an event row
lets the panel mark its history complete. Without one the name is classified `ended_uncovered`
(`survivorship_complete=0`) and Stage 11 labels from it carry that flag.

## 3. Interim hints (already published, consumed automatically)

Until the exports above exist, `15b` reads the existing Norgate import **reports**
(`output/*_reports/market_data/norgate_delisted*price_import.csv`) as delisting-date *hints*
(`last_bar_date` where `status=loaded`). Hints classify an ended name as `delisted_covered` only if
Yahoo/export price history actually reaches the hinted delist date; they do not supply prices.

## 4. What each sector needs to do

The sector repos already load Norgate delisted bars into their own DBs (see the import reports).
Publishing the contract is a dump of those same bars + membership metadata to the two CSVs above.
Priority: **biotech** (highest delisting rate → worst survivor bias), then med_devices, technology.
