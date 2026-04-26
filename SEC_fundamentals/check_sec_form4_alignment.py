#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def business_days_between(start: date, end: date) -> list[str]:
    out: list[str] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Check SEC/Form4 historical as_of_date alignment.")
    parser.add_argument("--sec-db", type=Path, required=True, help="SEC fundamentals SQLite DB path.")
    parser.add_argument("--form4-db", type=Path, required=True, help="Form4 SQLite DB path.")
    parser.add_argument("--start-date", type=str, required=True, help="Start date YYYY-MM-DD.")
    parser.add_argument("--end-date", type=str, required=True, help="End date YYYY-MM-DD.")
    args = parser.parse_args()

    start = parse_iso_date(args.start_date)
    end = parse_iso_date(args.end_date)
    expected = set(business_days_between(start, end))

    sec_conn = sqlite3.connect(args.sec_db)
    form4_conn = sqlite3.connect(args.form4_db)
    try:
        sec_rows = sec_conn.execute(
            "SELECT DISTINCT as_of_date FROM sec_fundamental_snapshot_filled_security_t1_resolved "
            "WHERE as_of_date >= ? AND as_of_date <= ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        form4_rows = form4_conn.execute(
            "SELECT DISTINCT as_of_date FROM stock_signal_snapshot_tier1 "
            "WHERE as_of_date >= ? AND as_of_date <= ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        sec_conn.close()
        form4_conn.close()

    sec_dates = {str(r[0]) for r in sec_rows if r and r[0]}
    form4_dates = {str(r[0]) for r in form4_rows if r and r[0]}
    missing_in_sec = sorted(form4_dates - sec_dates)
    missing_in_form4 = sorted(sec_dates - form4_dates)
    missing_business_sec = sorted(expected - sec_dates)
    missing_business_form4 = sorted(expected - form4_dates)

    print(f"sec_dates={len(sec_dates)}")
    print(f"form4_dates={len(form4_dates)}")
    print(f"missing_in_sec={len(missing_in_sec)}")
    print(f"missing_in_form4={len(missing_in_form4)}")
    print(f"missing_business_sec={len(missing_business_sec)}")
    print(f"missing_business_form4={len(missing_business_form4)}")
    print("missing_in_sec_sample=" + ",".join(missing_in_sec[:20]))
    print("missing_in_form4_sample=" + ",".join(missing_in_form4[:20]))
    print("missing_business_sec_sample=" + ",".join(missing_business_sec[:20]))
    print("missing_business_form4_sample=" + ",".join(missing_business_form4[:20]))


if __name__ == "__main__":
    main()
