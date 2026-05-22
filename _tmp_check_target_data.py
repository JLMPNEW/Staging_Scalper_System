import sqlite3
from pathlib import Path

BIOTECH_DB = Path(r"C:\Users\josel\Documents\STAGING\DB\biotech_index.sqlite")
FORM4_DB = Path(r"C:\Users\josel\Documents\STAGING\DB\sec_insider.sqlite")
TICKERS = ["BBIO", "ARWR", "NBIX", "ASND"]


def norm_expr(col: str) -> str:
    return f"UPPER(REPLACE({col}, '.', '-'))"


def count_biotech(con: sqlite3.Connection, table: str, ticker: str, *, asof: str | None = None) -> int:
    where = f"{norm_expr('c.ticker')} = ?"
    params: list[object] = [ticker]
    if asof:
        where += " AND t.asof_date = ?"
        params.append(asof)
    return int(
        con.execute(
            f"SELECT COUNT(*) FROM {table} t JOIN companies c ON c.company_id = t.company_id WHERE {where}",
            params,
        ).fetchone()[0]
        or 0
    )


def form4_tables(con: sqlite3.Connection) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        for candidate in ("ticker", "issuer_ticker", "symbol", "issuer_trading_symbol"):
            if candidate in cols:
                out.append((table, candidate))
                break
    return out


def main() -> None:
    with sqlite3.connect(BIOTECH_DB) as con:
        con.row_factory = sqlite3.Row
        print("biotech")
        for ticker in TICKERS:
            row = con.execute(
                """
                SELECT company_id, ticker, company_name, cik, universe_status, is_active, source_screen_decision, reason_codes
                FROM companies
                WHERE UPPER(REPLACE(ticker, '.', '-')) = ?
                """,
                (ticker,),
            ).fetchone()
            print(f"{ticker}: company={dict(row) if row else None}")
            for table in (
                "ctgov_query_hits",
                "trial_company_links",
                "sec_filings",
                "sec_biotech_events",
                "sec_companyfacts_observations",
                "market_bars_daily",
                "market_features_daily",
                "daily_features",
                "daily_scores",
            ):
                try:
                    if table in {"ctgov_query_hits"}:
                        n = int(con.execute(f"SELECT COUNT(*) FROM {table} WHERE {norm_expr('ticker')} = ?", (ticker,)).fetchone()[0] or 0)
                    elif table in {"market_bars_daily"}:
                        n = int(con.execute(f"SELECT COUNT(*) FROM {table} WHERE {norm_expr('ticker')} = ?", (ticker,)).fetchone()[0] or 0)
                    else:
                        n = count_biotech(con, table, ticker)
                except sqlite3.Error:
                    continue
                if n:
                    print(f"  {table}: {n}")
    if FORM4_DB.exists():
        with sqlite3.connect(FORM4_DB) as con:
            print("form4")
            for ticker in TICKERS:
                hits = []
                for table, col in form4_tables(con):
                    try:
                        n = int(con.execute(f"SELECT COUNT(*) FROM {table} WHERE {norm_expr(col)} = ?", (ticker,)).fetchone()[0] or 0)
                    except sqlite3.Error:
                        continue
                    if n:
                        hits.append(f"{table}.{col}:{n}")
                print(f"{ticker}: {'; '.join(hits) if hits else 'NO_FORM4_HITS'}")
    else:
        print(f"form4 db missing: {FORM4_DB}")


if __name__ == "__main__":
    main()
