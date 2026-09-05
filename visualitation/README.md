# Portfolio Command Center

Read-only Streamlit decision-support dashboard over point-in-time portfolio,
broker, macro and index-risk artifacts. `Position_Monitor.py` is the only live
entry point; index correlations are integrated as a tab so every panel shares
one selected as-of date.

## Run

From the repository root:

```powershell
C:\Users\josel\miniconda3\envs\scalper-staging\python.exe -m streamlit run visualitation\Position_Monitor.py --server.port 8510 --server.address 127.0.0.1 --server.headless true
```

Open <http://localhost:8510/>.

## Dashboard tabs

- **Overview** — account value, realized P&L MTD/YTD, dividends and net
  interest YTD, cash, beta, H1 current/next estimates, and MTD/YTD performance
  versus SPY and QQQ.
- **Positions** — ledger-to-target reconciliation with next earnings dates.
- **Index risk** — 42-day half-life tactical correlations, 250-day structural
  correlations, coverage gates, holding-to-index mapping, and the exact-date
  verified ETF correlation matrix.
- **Research queue** — All/Target/Held/Monitored views with next earnings,
  current price, MA50, MA200, and selected-name starter/add bands.
- **Data quality** — panel-level lineage, seals, coverage, and failure states.
- **Sector value · future** — contract placeholder for the point-in-time ETF
  fundamental and constituent-relative-value layer.

## Point-in-time and integrity policy

- The selected run is the time boundary for every tab. The app never replaces
  a missing correlation publication with a newer date.
- The rendered final book must match the SHA-256 recorded by its final manifest.
- Index ETF matrices are accepted only through the fail-closed reader in
  `index_correlations/dashboard_data.py`.
- Portfolio/index correlations are displayed with explicit name and gross-value
  coverage. Below the 80% gate they are diagnostic, not decision-grade.
- H1 is a shadow candidate model and is labelled as such; it is not presented as
  the portfolio sizing authority.
- Correlation uses total-return-adjusted prices. Future ETF valuation must use a
  separate point-in-time holdings/fundamental/unadjusted-price contract.

## Orchestrator wiring

The global nightly path is `orchestration/run_nightly.py` →
`orchestration/run_all.py` → `orchestration/registry.yaml`. The required,
non-network `index_correlations` job runs after `portfolio_layer` and publishes
`output/index_correlations/<date>/correlation_manifest.json`. The dashboard is a
consumer only; it is not started by the data orchestrator.
