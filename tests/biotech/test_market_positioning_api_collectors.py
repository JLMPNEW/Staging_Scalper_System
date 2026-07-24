from __future__ import annotations

import csv
import io
import sys
import types
import zipfile
from datetime import date

import pytest

from market_positioning import api_collectors
from market_positioning.api_collectors import (
    filter_ibkr_tickers_for_asof,
    finra_short_interest_records,
    load_universe_membership_end_map,
    normalize_ibkr_fee_rate,
    prune_ibkr_rows_after_membership_end,
    sync_finra_equity_short_interest_files,
    sync_ibkr_borrow_availability,
    sync_sec_13f_data_sets,
)
from market_positioning.core import aggregate_13f_ownership_for_tickers, connect, init_db
from market_positioning.ibkr_capacity import bounded_streaming_batch_size


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


def test_ibkr_shortable_generic_tick_uses_streaming_request(monkeypatch, tmp_path) -> None:
    class FakeContract:
        conId = 123

    class FakeTicker:
        shortableShares = 5000.0

    class FakeBar:
        date = date(2026, 7, 17)
        close = 0.01

    class FakeIB:
        instances: list[FakeIB] = []

        def __init__(self) -> None:
            self.connected = False
            self.market_data_requests: list[dict[str, object]] = []
            self.cancelled: list[object] = []
            self.active_market_data_lines = 0
            self.max_active_market_data_lines = 0
            self.__class__.instances.append(self)

        def connect(self, *_args, **_kwargs) -> None:
            self.connected = True

        def disconnect(self) -> None:
            self.connected = False

        def isConnected(self) -> bool:  # noqa: N802
            return self.connected

        def reqMarketDataType(self, _market_data_type: int) -> None:  # noqa: N802
            return None

        def qualifyContracts(self, _contract) -> list[FakeContract]:  # noqa: N802
            return [FakeContract()]

        def reqHistoricalData(self, *_args, **_kwargs) -> list[object]:  # noqa: N802
            return [FakeBar()]

        def reqMktData(self, contract, **kwargs) -> FakeTicker:  # noqa: N802
            self.market_data_requests.append({"contract": contract, **kwargs})
            self.active_market_data_lines += 1
            self.max_active_market_data_lines = max(
                self.max_active_market_data_lines,
                self.active_market_data_lines,
            )
            return FakeTicker()

        def cancelMktData(self, contract) -> None:  # noqa: N802
            self.cancelled.append(contract)
            self.active_market_data_lines -= 1

        def sleep(self, _seconds: float) -> None:
            return None

    class FakeStock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setitem(sys.modules, "ib_insync", types.SimpleNamespace(IB=FakeIB, Stock=FakeStock))

    universe = tmp_path / "tickers.csv"
    universe.write_text(
        "ticker,exchange\n" + "".join(f"TEST{index:03d},NASDAQ\n" for index in range(101)),
        encoding="utf-8",
    )
    with connect(tmp_path / "market_positioning.sqlite") as conn:
        init_db(conn)
        sync_ibkr_borrow_availability(
            conn,
            tickers_csv=universe,
            history_start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 17),
            snapshot_wait_sec=0.0,
            sleep_sec=0.0,
            batch_size=500,
        )

    fake_ib = FakeIB.instances[-1]
    assert len(fake_ib.market_data_requests) == 101
    assert fake_ib.market_data_requests[0]["genericTickList"] == "236"
    assert fake_ib.market_data_requests[0]["snapshot"] is False
    # Reserve 10% of the 100-line account allowance for TWS/manual subscriptions.
    assert fake_ib.max_active_market_data_lines == 90
    assert fake_ib.active_market_data_lines == 0
    assert len(fake_ib.cancelled) == 101


def test_ibkr_streaming_batch_limit_and_failure_cleanup(monkeypatch, tmp_path) -> None:
    assert bounded_streaming_batch_size(0) == 1
    assert bounded_streaming_batch_size(50) == 50
    assert bounded_streaming_batch_size(100) == 90
    assert bounded_streaming_batch_size(500) == 90

    class FakeContract:
        conId = 123

    class FakeBar:
        date = date(2026, 7, 17)
        close = 0.01

    class FakeIB:
        instances: list[FakeIB] = []

        def __init__(self) -> None:
            self.connected = False
            self.active_market_data_lines = 0
            self.cancelled = 0
            self.__class__.instances.append(self)

        def connect(self, *_args, **_kwargs) -> None:
            self.connected = True

        def disconnect(self) -> None:
            self.connected = False

        def isConnected(self) -> bool:  # noqa: N802
            return self.connected

        def reqMarketDataType(self, _market_data_type: int) -> None:  # noqa: N802
            return None

        def qualifyContracts(self, _contract) -> list[FakeContract]:  # noqa: N802
            return [FakeContract()]

        def reqHistoricalData(self, *_args, **_kwargs) -> list[object]:  # noqa: N802
            return [FakeBar()]

        def reqMktData(self, *_args, **_kwargs) -> object:  # noqa: N802
            self.active_market_data_lines += 1
            return object()

        def cancelMktData(self, _contract) -> None:  # noqa: N802
            self.cancelled += 1
            self.active_market_data_lines -= 1

        def sleep(self, _seconds: float) -> None:
            if self.active_market_data_lines:
                raise RuntimeError("simulated wait failure")

    class FakeStock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setitem(sys.modules, "ib_insync", types.SimpleNamespace(IB=FakeIB, Stock=FakeStock))
    universe = tmp_path / "tickers.csv"
    universe.write_text("ticker,exchange\nTEST,NASDAQ\n", encoding="utf-8")

    with connect(tmp_path / "market_positioning.sqlite") as conn:
        init_db(conn)
        with pytest.raises(RuntimeError, match="simulated wait failure"):
            sync_ibkr_borrow_availability(
                conn,
                tickers_csv=universe,
                history_start_date=date(2026, 7, 10),
                end_date=date(2026, 7, 17),
                snapshot_wait_sec=0.0,
                sleep_sec=0.0,
            )

    fake_ib = FakeIB.instances[-1]
    assert fake_ib.cancelled == 1
    assert fake_ib.active_market_data_lines == 0


def test_ibkr_historical_catchup_can_skip_current_shortable_snapshot(monkeypatch, tmp_path) -> None:
    class FakeContract:
        conId = 123

    class FakeBar:
        date = date(2026, 7, 17)
        close = 0.01

    class FakeIB:
        instances: list[FakeIB] = []

        def __init__(self) -> None:
            self.connected = False
            self.market_data_requests = 0
            self.__class__.instances.append(self)

        def connect(self, *_args, **_kwargs) -> None:
            self.connected = True

        def disconnect(self) -> None:
            self.connected = False

        def isConnected(self) -> bool:  # noqa: N802
            return self.connected

        def reqMarketDataType(self, _market_data_type: int) -> None:  # noqa: N802
            return None

        def qualifyContracts(self, _contract) -> list[FakeContract]:  # noqa: N802
            return [FakeContract()]

        def reqHistoricalData(self, *_args, **_kwargs) -> list[object]:  # noqa: N802
            return [FakeBar()]

        def reqMktData(self, *_args, **_kwargs):  # noqa: ANN201, N802
            self.market_data_requests += 1
            raise AssertionError("Current shortableShares must not be requested during historical catch-up")

        def sleep(self, _seconds: float) -> None:
            return None

    class FakeStock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setitem(sys.modules, "ib_insync", types.SimpleNamespace(IB=FakeIB, Stock=FakeStock))

    universe = tmp_path / "tickers.csv"
    universe.write_text("ticker,exchange\nTEST,NASDAQ\n", encoding="utf-8")
    with connect(tmp_path / "market_positioning.sqlite") as conn:
        init_db(conn)
        sync_ibkr_borrow_availability(
            conn,
            tickers_csv=universe,
            history_start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 17),
            shortable_snapshot=False,
            sleep_sec=0.0,
        )
        fee_dates = [
            row[0]
            for row in conn.execute(
                "SELECT asof_date FROM ibkr_borrow_fee_rate_daily WHERE ticker = 'TEST'"
            )
        ]
        shortable_count = int(conn.execute("SELECT COUNT(*) FROM ibkr_shortable_shares_snapshots").fetchone()[0])

    assert FakeIB.instances[-1].market_data_requests == 0
    assert fee_dates == ["2026-07-17"]
    assert shortable_count == 0


def test_ibkr_membership_filter_and_prune_recycled_symbol_rows(tmp_path) -> None:
    universe = tmp_path / "tickers.csv"
    universe.write_text(
        "ticker,membership_end_date\nACTIVE,\nINVN,2017-05-18\n",
        encoding="utf-8",
    )
    membership_ends = load_universe_membership_end_map(universe)
    eligible, ended = filter_ibkr_tickers_for_asof(
        ["ACTIVE", "INVN"],
        membership_ends,
        date(2026, 7, 17),
    )
    assert eligible == ["ACTIVE"]
    assert ended == {"INVN"}

    with connect(tmp_path / "market_positioning.sqlite") as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO ibkr_borrow_fee_rate_daily(
                ticker, asof_date, con_id, borrow_fee_rate, source, created_at, updated_at
            ) VALUES ('INVN', '2017-05-18', 1, 0.01, 'interactive_brokers', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO ibkr_borrow_fee_rate_daily(
                ticker, asof_date, con_id, borrow_fee_rate, source, created_at, updated_at
            ) VALUES ('INVN', '2026-07-17', 2, 0.10, 'interactive_brokers', 'now', 'now')
            """
        )
        deleted = prune_ibkr_rows_after_membership_end(conn, membership_ends)
        remaining_dates = [
            row[0]
            for row in conn.execute(
                "SELECT asof_date FROM ibkr_borrow_fee_rate_daily WHERE ticker = 'INVN' ORDER BY asof_date"
            )
        ]
    assert deleted == (1, 0)
    assert remaining_dates == ["2017-05-18"]


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
