"""Stage 5 - tactical rotation sleeve (shadow-only).

Self-contained, PROD-independent rotation signal generator built from the sealed Stage 2
price/return panel. Emits optimizer-contract tables (SectorName/ScorePct/State and
Ticker/MarketName/Score/ScorePct/State) but never mutates the Stage 3/4 live book.
"""