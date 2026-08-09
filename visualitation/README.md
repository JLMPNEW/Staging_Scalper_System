# Visualization Dashboard

Read-only Streamlit dashboard over sealed `portfolio_layer` artifacts. It never
writes into `portfolio_layer/` — it is a pure consumer of run outputs.

## Run

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe -m streamlit run app.py
```

Then open http://localhost:8501 (or the port Streamlit prints).

## Pages

- **app.py — Final Target Book**: reads
  `portfolio_layer/output/runs/<date>/final/final_target_book.csv`
  (macro-regime preamble + book table), `final_manifest.json`, and the
  cumulative `broker_trades` table in `portfolio_layer/db/portfolio_layer.sqlite`.
  Shows manifest acceptance, regime context (active V1 source plus the H1
  shadow-candidate estimate from MacroLayer/out/regime_h1, latest as-of ≤ run
  date), KPI tiles, IB P&L (realized + mark-to-market MTD/YTD read from the
  book preamble's SEALED IB statement figures — the authoritative numbers —
  with a separate trade-level attribution block and per-symbol breakdown
  derived from broker_trades, which excludes dividends/interest and therefore
  does not tie to the sealed total), an IB positions chart
  (stacked cost-vs-market bars incl. cash, green/red tips = unrealized
  gain/loss, labels = % of account; from ledger/holding_state.csv +
  broker_cash_report.csv), and the styled book table (ticker, weight,
  IB_quantity, earnings, sector, rating, states, benchmark/relative
  returns/MAs, current price, starter/add/trim bands with high-before-low
  for starter and add). current_price renders green-bold when ≤
  starter_band_high and gets a light-green fill when ≤ add_band_high;
  avg_cost_price fills green/red vs current price. price_band_status is shown
  because most bands are `diagnostic_only_missing_intrinsic` (not actionable).
  Run date selectable in the sidebar (defaults to latest run).

## Schema notes

- The book preamble packs MULTIPLE `key,value` pairs per line (the IB P&L rows),
  so it is parsed pairwise via `csv.reader` — never `partition(",")`.
- Run dirs before 2026-07-31 use an older book schema with no preamble and no
  sector/rating/state/price columns. The app degrades to blanks and shows a
  schema-warning banner rather than raising.
- Both SQLite DBs run in WAL mode, so cache keys use `db_signature()`
  (main file + `-wal` sidecar mtime/size), not the main file's mtime alone.

## Planned pages (from the visualization design discussion, 2026-08-01)

- Holdings vs targets (ledger/holding_state + exits/exit_signals)
- Cross-sector scores/rankings (stocks_scores + per-sector rank tables)
- Macro regime history (MacroLayer regime_v2 probability series)
- Actions (costs/trade_list + exits/exit_actions + payout_plan)
- System health (manifests, staleness, risk governor directive)
