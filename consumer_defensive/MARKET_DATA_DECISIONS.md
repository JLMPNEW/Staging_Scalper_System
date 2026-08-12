# Consumer Defensive Market Data Decisions

Status: adopted for Stage 3 implementation

1. Yahoo adjusted chart data is the primary production source for active securities and the `XLP`/`SPY` benchmarks.
2. Norgate total-return data is mandatory for historical/delisted securities and is a whole-ticker fallback for active securities.
3. Point-in-time membership remains Norgate-authoritative and is independent of price-source selection.
4. A ticker uses one continuous adjusted-price source for a return series. Per-date Yahoo/Norgate splicing is prohibited because adjustment bases can differ.
5. Both sources remain stored with distinct source IDs. A dated selection record identifies the source used for scoring and research.
6. Yahoo is selected for an active ticker when its coverage is sufficiently current and begins near the security's required start. Otherwise the complete Norgate series is selected and the fallback is reported.
7. Norgate is selected first for a delisted ticker. Yahoo is not required for delisted coverage.
8. Raw OHLCV remains unadjusted. `adjusted_close` is the return-series field: Yahoo adjusted close or Norgate total-return close.
9. Historical/delisted prices reaching the final quoted date do not by themselves make a security survivorship-complete. Terminal cash, stock, bankruptcy, or successor consideration remains a separate event contract.
10. Stage 3 must begin no later than `2017-11-28`, support the first requested snapshot on `2019-01-02`, and fail if either benchmark lacks adequate history.
11. The reviewed ledger is `system_csvs/consumer_defensive_terminal_events.csv`; its exact ticker set must match the 11 delisted securities loaded by Stage 2.
12. Fixed cash and wipeout outcomes are terminal values. For stock consideration, the stock leg equals `share_ratio * raw_close_at_reference * adjusted_close_at_horizon / adjusted_close_at_reference`; nominal cash is added without pretending it was reinvested.
13. The reviewed successor source is Yahoo for PFGC and SJM and Norgate asset `NTCOY-202408` for the former NTCO ADS lineage. A stock leg cannot splice providers.
14. Dean Foods' 2021-05-28 zero-distribution cancellation is the economic boundary. Norgate quotes through 2021-06-02 remain in raw storage for audit but cannot extend the holding period.
15. WBA's fixed 11.45 cash floor is stored. Its separate right worth up to 3.00 remains unresolved; WBA is `survivorship_complete=0` and `calibration_eligible=0` until reviewed proceeds are known.
16. The resolver returns `pre_terminal_event` before an event date, preventing future terminal terms from being applied to an earlier horizon.
17. Yahoo and Norgate payload identity, requested range, chronological order, row shape, and finite-value rules are validated before cache or database publication. Cache-only mode fails on a malformed cache; only a separately validated live response may repair it.
18. A refresh replaces prices and corporate actions only within its exact requested range. Stale in-range rows are removed, while earlier and later rows are preserved.
19. Coverage is measured against the relevant trading calendar and must expose internal-session gaps. First/last dates and row counts alone cannot establish completeness.
20. Norgate price/action publication fences both `US Equities` and `US Equities Delisted` for the complete extraction. Fingerprint drift leaves price/action facts unchanged and records only a failed zero-row ingestion run.
21. Cache-only and reconstructed historical runs prove reproducible input identity, not strict OOS status. The historical contract and model lock date govern OOS labels.
22. A security's required source-coverage window begins at the later of the global history start, its listing start, or 400 calendar days before its first calibration-eligible recognized-membership interval. The 400-day buffer is the shared historical contract, not a ticker-specific waiver. A reviewed terminal-event exclusion with no eligible interval, such as WBA, falls back to its first recognized interval so excluded securities still retain auditable prices.
23. A security whose first applicable interval is after an audit date is future-only and is omitted from that historical source-selection audit. Its already-loaded raw history remains untouched and is evaluated when the security first becomes PIT-relevant.
24. Within the required window, a series fails above 2% missing trading sessions or above five consecutive missing sessions. Missing-session ratios above 1% are retained as explicit warnings. These controls never authorize cross-provider date splicing.
25. The read-only `2026-08-10` v5 audit evaluated all 119 securities plus the Yahoo-only `XLP` and `SPY` benchmarks. Under the relevant-window contract all 121 selected a qualifying whole-ticker source: 108 Yahoo and 13 Norgate. MAMA's required window is `2020-06-10` through `2026-08-10`, where Norgate has 1,549 of 1,549 expected sessions. Its 42 sparse omissions were confined to 2017-2019, before both the 400-day warm-up and its `2021-07-15` first recognized admission. The read-only `2019-01-02` audit selected all 103 then-relevant candidates and correctly excluded future-only MAMA.
