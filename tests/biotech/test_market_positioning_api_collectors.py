from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

from market_positioning import api_collectors
from market_positioning.api_collectors import (
    finra_short_interest_records,
    sync_finra_equity_short_interest_files,
    sync_sec_13f_data_sets,
)
from market_positioning.core import connect, init_db


def write_csv(path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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
    assert rows[0][7] == 4
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
        row = conn.execute("SELECT * FROM short_interest_snapshots").fetchone()

    assert result.rows == 1
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
