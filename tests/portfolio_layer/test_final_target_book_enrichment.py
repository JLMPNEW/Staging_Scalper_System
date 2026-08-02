from __future__ import annotations

import csv
import importlib.util
import io
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_enricher() -> ModuleType:
    path = (
        PROJECT_ROOT
        / "portfolio_layer"
        / "orchestration"
        / "21_enrich_final_target_book.py"
    )
    spec = importlib.util.spec_from_file_location("final_target_enricher_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enriched_book_includes_broker_only_stock_and_requested_context() -> None:
    enricher = _load_enricher()
    rows = enricher.compose_rows(
        weights={"AAA": 0.8, "CASH": 0.2},
        scores={
            "AAA": {
                "sector": "Information Technology",
                "rating": "buy",
                "final_score": "0.12",
                "score_confidence": "0.85",
            }
        },
        states={
            "AAA": {
                "internal_state": "stable",
                "action_state": "hold",
            }
        },
        market_signals={
            "AAA": {
                "ticker": "AAA",
                "benchmark_ticker": "XLK",
                "rel_ret_5d": "-0.01",
                "rel_ret_20d": "0.02",
                "below_ma50": "1",
                "below_ma200": "0",
                "inputs_json": (
                    '{"latest_adj_close":125.5,"ma50":130.0,"ma200":120.0}'
                ),
            }
        },
        levels={
            "AAA": {
                "market_structure_json": '{"latest_price": 125.5}',
                "starter_band_low": "115",
                "starter_band_high": "120",
                "add_band_low": "105",
                "add_band_high": "110",
                "trim_band_low": "140",
                "trim_band_high": "150",
            }
        },
        earnings={
            "AAA": {"next_earnings_date": "2026-08-05"},
            "BBB": {"next_earnings_date": "2026-08-12"},
        },
        holdings={"BBB": {"net_shares": "25"}},
        holding_prices={"BBB": 42.25},
        holding_as_of="2026-07-31",
    )
    by_ticker = {row["ticker"]: row for row in rows}

    assert enricher.MACRO_FIELDS == [
        "active_current_regime",
        "active_next_regime",
        "current_confidence",
        "next_confidence",
        "macro_as_of_date",
    ]
    assert "layer_source" not in enricher.BOOK_FIELDS
    assert by_ticker["AAA"]["current_price"] == 125.5
    assert by_ticker["AAA"]["benchmark_ticker"] == "XLK"
    assert by_ticker["AAA"]["rel_ret_5d"] == "-0.01"
    assert by_ticker["AAA"]["ma50"] == 130.0
    assert by_ticker["AAA"]["below_ma50"] == "1"
    assert by_ticker["AAA"]["starter_band_low"] == "115"
    assert by_ticker["BBB"]["weight"] == 0.0
    assert by_ticker["BBB"]["IB_Holding"] == 1
    assert by_ticker["BBB"]["IB_quantity"] == "25"
    assert by_ticker["BBB"]["is_scored"] == 0
    assert by_ticker["BBB"]["is_monitored"] == 0
    assert by_ticker["BBB"]["final_score"] == ""
    assert by_ticker["BBB"]["score_confidence"] == ""
    assert by_ticker["BBB"]["current_price"] == 42.25
    assert by_ticker["BBB"]["next_earnings_date"] == "8/12/2026"


def test_final_report_uses_macro_preamble_not_repeated_columns(tmp_path: Path) -> None:
    enricher = _load_enricher()
    report = tmp_path / "final_target_book.csv"
    macro = {
        "active_current_regime": "SLOW_GROWTH",
        "active_next_regime": "STAGFLATION",
        "current_confidence": "0.14",
        "next_confidence": "0.04",
        "macro_as_of_date": "2026-07-31",
    }
    rows = [{field: "" for field in enricher.BOOK_FIELDS}]
    rows[0]["ticker"] = "AAA"
    ib_performance = {
        "ib_mark_to_market_mtd_profit": 12043.79,
        "ib_mark_to_market_ytd_profit": 48863.66,
        "ib_realized_profit_loss_mtd": 10185.30,
        "ib_realized_short_term_mtd": 9828.60,
        "ib_realized_long_term_mtd": 0.0,
        "ib_dividends_mtd": 57.05,
        "ib_net_broker_interest_mtd": 299.65,
        "ib_realized_profit_loss_ytd": 53238.46,
        "ib_realized_short_term_ytd": 51980.95,
        "ib_realized_long_term_ytd": -2149.36,
        "ib_dividends_ytd": 1800.84,
        "ib_net_broker_interest_ytd": 1606.03,
        "ib_profit_as_of_date": "2026-07-31",
    }
    enricher.write_final_report(
        report,
        ib_performance=ib_performance,
        macro=macro,
        rows=rows,
    )
    parsed = list(csv.reader(io.StringIO(report.read_text(encoding="utf-8"))))
    assert parsed[0] == [
        "ib_mark_to_market_mtd_profit",
        "12043.79",
        "ib_mark_to_market_ytd_profit",
        "48863.66",
        "ib_profit_as_of_date",
        "7/31/2026",
    ]
    assert parsed[1] == [
        "ib_realized_profit_loss_mtd",
        "10185.3",
        "ib_realized_short_term_mtd",
        "9828.6",
        "ib_realized_long_term_mtd",
        "0.0",
        "ib_dividends_mtd",
        "57.05",
        "ib_net_broker_interest_mtd",
        "299.65",
    ]
    assert parsed[2] == [
        "ib_realized_profit_loss_ytd",
        "53238.46",
        "ib_realized_short_term_ytd",
        "51980.95",
        "ib_realized_long_term_ytd",
        "-2149.36",
        "ib_dividends_ytd",
        "1800.84",
        "ib_net_broker_interest_ytd",
        "1606.03",
    ]
    assert parsed[3:8] == [
        ["active_current_regime", "SLOW_GROWTH"],
        ["active_next_regime", "STAGFLATION"],
        ["current_confidence", "0.14"],
        ["next_confidence", "0.04"],
        ["macro_as_of_date", "7/31/2026"],
    ]
    assert parsed[8] == []
    assert parsed[9] == enricher.BOOK_FIELDS
    assert not set(enricher.MACRO_FIELDS) & set(parsed[9])


def test_ib_performance_reconciles_realized_income_and_consistent_total(
    tmp_path: Path,
) -> None:
    enricher = _load_enricher()
    statement = tmp_path / "statement.csv"
    statement.write_text(
        "\n".join(
            [
                (
                    "Month & Year to Date Performance Summary,Header,"
                    "Asset Category,Symbol,Description,Mark-to-Market MTD,"
                    "Mark-to-Market YTD,Realized S/T MTD,Realized S/T YTD,"
                    "Realized L/T MTD,Realized L/T YTD"
                ),
                (
                    "Month & Year to Date Performance Summary,Data,"
                    "Total (All Assets),,,12043.78973034,48863.66332662,"
                    "9828.60328945,51980.95303607,0,-2149.36195515"
                ),
                (
                    "Month & Year to Date Performance Summary,Data,"
                    "Total (All Assets),,,12043.78973034,48863.66332662,"
                    "9828.60328945,51980.95303607,0,-2149.36195515"
                ),
                (
                    "Cash Report,Header,Currency Summary,Currency,Total,"
                    "Securities,Futures,Paxos,Month to Date,Year to Date"
                ),
                "Cash Report,Data,Dividends,Base Currency Summary,0,0,0,0,57.05,1800.84",
                (
                    "Cash Report,Data,Broker Interest Paid and Received,"
                    "Base Currency Summary,0,0,0,0,299.65,1606.03"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = enricher._ib_performance(statement, as_of="2026-07-31")
    assert result["ib_mark_to_market_mtd_profit"] == pytest.approx(12043.78973034)
    assert result["ib_mark_to_market_ytd_profit"] == pytest.approx(48863.66332662)
    assert result["ib_realized_profit_loss_mtd"] == pytest.approx(10185.30328945)
    assert result["ib_realized_profit_loss_ytd"] == pytest.approx(53238.46108092)

    statement.write_text(
        statement.read_text(encoding="utf-8").replace(
            "12043.78973034,48863.66332662\n",
            "12043.78973034,48863.66332662\n",
            1,
        )
        + (
            "Month & Year to Date Performance Summary,Data,"
            "Total (All Assets),,,1,2,3,4,5,6\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="disagree"):
        enricher._ib_performance(statement, as_of="2026-07-31")


def test_target_weight_parser_fails_closed() -> None:
    enricher = _load_enricher()

    with pytest.raises(ValueError, match="duplicate"):
        enricher._weights(
            [
                {"ticker": "AAA", "weight": "0.5"},
                {"ticker": "AAA", "weight": "0.5"},
            ]
        )
    with pytest.raises(ValueError, match="sum to one"):
        enricher._weights([{"ticker": "AAA", "weight": "0.8"}])
