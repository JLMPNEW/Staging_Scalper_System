from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

from market_positioning import api_collectors
from market_positioning.api_collectors import (
    finra_short_interest_records,
    normalize_ibkr_fee_rate,
    sync_finra_equity_short_interest_files,
    sync_sec_13f_data_sets,
)
from market_positioning.core import aggregate_13f_ownership_for_tickers, connect, init_db


def write_csv(path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_normalize_ibkr_fee_rate_empty_unit_defaults_to_decimal() -> None:
    assert normalize_ibkr_fee_rate(0.003, unit="") == 0.003
    assert normalize_ibkr_fee_rate(0.003, unit="decimal") == 0.003
    assert normalize_ibkr_fee_rate(0.3, unit="percent") == 0.003


def test_normalize_ibkr_fee_rate_rejects_unknown_unit() -> None:
    try:
        normalize_ibkr_fee_rate(0.003, unit="basis_points")
    except ValueError as exc:
        assert "Unsupported IBKR fee-rate unit" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unsupported IBKR fee-rate unit")


def test_finra_short_interest_records_maps_current_short_position(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_http_json(url, *, payload, user_agent, timeout_sec):  # noqa: ANN001, ARG001
        calls.append(payload)
        return [
            {
                "issueSymbolIdentifier": "AAA",
                "settlementDate": "2025-01-15",
                "currentShortShareNumber": 1000,
                "averageShortShareNumber": 250,
                "averageDailyVolumeQuantity": 500,
                "daysToCoverNumber": None,
            }
        ]

    monkeypatch.setattr(api_collectors, "http_json", fake_http_json)
    rows = finra_short_interest_records(
        tickers=["AAA"],
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 15),
        api_url="https://example.test/finra",
        sleep_sec=0.0,
        user_agent="unit",
    )

    assert len(rows) == 1
    assert rows[0][0] == "AAA"
    assert rows[0][4] == 1000
    assert rows[0][7] == 2
    assert calls[0]["compareFilters"][0]["fieldName"] == "settlementDate"  # type: ignore[index]


def test_finra_equity_short_interest_file_sync_and_export(tmp_path, monkeypatch) -> None:
    universe_csv = tmp_path / "universe.csv"
    db_path = tmp_path / "market_positioning.sqlite"
    cache_dir = tmp_path / "cache"
    write_csv(
        universe_csv,
        ["ticker", "company_name"],
        [{"ticker": "AAA", "company_name": "Alpha Inc"}, {"ticker": "BBB", "company_name": "Beta Inc"}],
    )
    payload = (
        "accountingYearMonthNumber|symbolCode|issueName|issuerServicesGroupExchangeCode|marketClassCode|"
        "currentShortPositionQuantity|previousShortPositionQuantity|stockSplitFlag|averageDailyVolumeQuantity|"
        "daysToCoverQuantity|revisionFlag|changePercent|changePreviousNumber|settlementDate\n"
        "20250115|AAA|Alpha Inc|A|NYSE|1200|1000||300|4.00||20.00|200|2025-01-15\n"
        "20250115|ZZZ|Other Inc|A|NYSE|999|888||1|999.00||0.00|0|2025-01-15\n"
    ).encode()

    monkeypatch.setattr(api_collectors, "http_request", lambda *args, **kwargs: payload)
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO short_interest_snapshots(
                ticker, asof_date, settlement_date, publication_date,
                short_interest_shares, float_shares, short_interest_pct_float, days_to_cover,
                source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "AAA",
                "2025-01-15",
                "2025-01-15",
                "2025-01-15",
                1000,
                None,
                None,
                3,
                "finra_equity_short_interest_files",
                "stale.csv",
                "2025-01-15T00:00:00Z",
                "2025-01-15T00:00:00Z",
            ),
        )
        result = sync_finra_equity_short_interest_files(
            conn,
            tickers_csv=universe_csv,
            history_start_date=date(2025, 1, 15),
            end_date=date(2025, 1, 15),
            cache_dir=cache_dir,
            publication_lag_days=12,
            user_agent="unit",
            sleep_sec=0.0,
        )
        row_count = conn.execute(
            "SELECT COUNT(*) FROM short_interest_snapshots WHERE source = ?",
            ("finra_equity_short_interest_files",),
        ).fetchone()[0]
        row = conn.execute(
            "SELECT * FROM short_interest_snapshots WHERE source = ?",
            ("finra_equity_short_interest_files",),
        ).fetchone()

    assert result.rows == 1
    assert row_count == 1
    assert row["ticker"] == "AAA"
    assert row["asof_date"] == "2025-01-27"
    assert row["settlement_date"] == "2025-01-15"
    assert row["short_interest_shares"] == 1200
    assert row["days_to_cover"] == 4
    assert row["source"] == "finra_equity_short_interest_files"


def test_sec_13f_data_set_sync_matches_universe_name(tmp_path, monkeypatch) -> None:
    universe_csv = tmp_path / "universe.csv"
    cache_dir = tmp_path / "cache"
    db_path = tmp_path / "market_positioning.sqlite"
    write_csv(
        universe_csv,
        ["ticker", "company_name"],
        [{"ticker": "AAA", "company_name": "Alpha Therapeutics Inc"}],
    )
    archive = cache_dir / "2025-q1_form13f.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    submission = io.StringIO()
    submission_writer = csv.DictWriter(
        submission,
        fieldnames=["ACCESSION_NUMBER", "CIK", "NAME", "FILING_DATE", "PERIODOFREPORT"],
        delimiter="\t",
    )
    submission_writer.writeheader()
    submission_writer.writerow(
        {
            "ACCESSION_NUMBER": "0000000000-25-000001",
            "CIK": "123",
            "NAME": "Fund A",
            "FILING_DATE": "2025-02-14",
            "PERIODOFREPORT": "2024-12-31",
        }
    )
    infotable = io.StringIO()
    infotable_writer = csv.DictWriter(
        infotable,
        fieldnames=["ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "VALUE", "SSHPRNAMT"],
        delimiter="\t",
    )
    infotable_writer.writeheader()
    infotable_writer.writerow(
        {
            "ACCESSION_NUMBER": "0000000000-25-000001",
            "NAMEOFISSUER": "ALPHA THERAPEUTICS INC",
            "CUSIP": "000AAA111",
            "VALUE": "100",
            "SSHPRNAMT": "1000",
        }
    )
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SUBMISSION.tsv", submission.getvalue())
        zf.writestr("INFOTABLE.tsv", infotable.getvalue())

    monkeypatch.setattr(api_collectors, "discover_sec_13f_archives", lambda **kwargs: ["https://example.test/13f.zip"])
    monkeypatch.setattr(api_collectors, "download_cached", lambda *args, **kwargs: archive)

    with connect(db_path) as conn:
        init_db(conn)
        result = sync_sec_13f_data_sets(
            conn,
            tickers_csv=universe_csv,
            cusip_ticker_map_csv=None,
            history_start_date=date(2019, 1, 1),
            end_date=date(2026, 6, 1),
            cache_dir=cache_dir,
            user_agent="unit",
        )
        rows = conn.execute("SELECT ticker, institutional_shares FROM institutional_13f_ownership_snapshots").fetchall()

    assert result.rows == 1
    assert rows[0]["ticker"] == "AAA"
    assert rows[0]["institutional_shares"] == 1000


def test_period_13f_aggregation_is_ticker_scoped(tmp_path) -> None:
    db_path = tmp_path / "market_positioning.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        now = "2026-07-03T00:00:00Z"
        conn.executemany(
            """
            INSERT INTO institutional_13f_filings(
                filing_key, accession_number, manager_cik, manager_name, period_of_report,
                filing_date, accepted_at, source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("AAA-F1", "AAA-F1", "M1", "Manager 1", "2024-12-31", "2025-02-10", "2025-02-10", "sec_13f_data_sets", "unit", now, now),
                ("AAA-F2", "AAA-F2", "M1", "Manager 1", "2024-12-31", "2025-02-14", "2025-02-14", "sec_13f_data_sets", "unit", now, now),
                ("AAA-F3", "AAA-F3", "M2", "Manager 2", "2024-12-31", "2025-02-13", "2025-02-13", "sec_13f_data_sets", "unit", now, now),
                ("BBB-F1", "BBB-F1", "M3", "Manager 3", "2024-09-30", "2024-11-12", "2024-11-12", "sec_13f_data_sets", "unit", now, now),
            ],
        )
        conn.executemany(
            """
            INSERT INTO institutional_13f_holdings(
                filing_key, manager_cik, manager_name, ticker, cusip, period_of_report,
                filing_date, accepted_at, shares, market_value, title_of_class, share_type,
                put_call, source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("AAA-F1", "M1", "Manager 1", "AAA", "AAA111111", "2024-12-31", "2025-02-10", "2025-02-10", 100, 1000, "COM", "SH", "", "sec_13f_data_sets", "unit", now, now),
                ("AAA-F2", "M1", "Manager 1", "AAA", "AAA111111", "2024-12-31", "2025-02-14", "2025-02-14", 200, 2000, "COM", "SH", "", "sec_13f_data_sets", "unit", now, now),
                ("AAA-F3", "M2", "Manager 2", "AAA", "AAA222222", "2024-12-31", "2025-02-13", "2025-02-13", 50, 500, "COM", "SH", "", "sec_13f_data_sets", "unit", now, now),
                ("BBB-F1", "M3", "Manager 3", "BBB", "BBB111111", "2024-09-30", "2024-11-12", "2024-11-12", 999, 9990, "COM", "SH", "", "sec_13f_data_sets", "unit", now, now),
            ],
        )
        conn.execute(
            """
            INSERT INTO institutional_13f_ownership_snapshots(
                ticker, asof_date, period_of_report, institutional_shares, institutional_value,
                manager_count, new_buyer_count, exiting_holder_count, net_buyer_count,
                institutional_ownership_delta_pct, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("BBB", "2024-11-12", "2024-09-30", 999, 9990, 1, 0, 0, 0, None, "sec_13f_data_sets", now, now),
        )

        assert aggregate_13f_ownership_for_tickers(conn, ["AAA"], source="sec_13f_data_sets") == 1
        aaa = conn.execute(
            "SELECT * FROM institutional_13f_ownership_snapshots WHERE ticker = 'AAA'"
        ).fetchone()
        bbb = conn.execute(
            "SELECT * FROM institutional_13f_ownership_snapshots WHERE ticker = 'BBB'"
        ).fetchone()

    assert aaa["asof_date"] == "2025-02-14"
    assert aaa["period_of_report"] == "2024-12-31"
    assert aaa["institutional_shares"] == 250
    assert aaa["institutional_value"] == 2500
    assert aaa["manager_count"] == 2
    assert bbb["asof_date"] == "2024-11-12"
    assert bbb["institutional_shares"] == 999
