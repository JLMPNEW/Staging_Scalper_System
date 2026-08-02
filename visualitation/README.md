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
  Shows manifest acceptance, regime context, KPI tiles, IB realized P&L
  (MTD/YTD tiles + per-symbol breakdown; computed from deduplicated IB
  statement trades by trade date ≤ run date), an IB positions chart
  (stacked cost-vs-market bars incl. cash, green/red tips = unrealized
  gain/loss, labels = % of account; from ledger/holding_state.csv +
  broker_cash_report.csv), and the styled book table (ticker, weight,
  IB_quantity, earnings, sector, rating, states, benchmark/relative
  returns/MAs, current price, starter/add/trim bands with high-before-low
  for starter and add). current_price renders green-bold when ≤
  starter_band_high and gets a light-green fill when ≤ add_band_high.
  Run date selectable in the sidebar (defaults to latest run).

## Planned pages (from the visualization design discussion, 2026-08-01)

- Holdings vs targets (ledger/holding_state + exits/exit_signals)
- Cross-sector scores/rankings (stocks_scores + per-sector rank tables)
- Macro regime history (MacroLayer regime_v2 probability series)
- Actions (costs/trade_list + exits/exit_actions + payout_plan)
- System health (manifests, staleness, risk governor directive)
