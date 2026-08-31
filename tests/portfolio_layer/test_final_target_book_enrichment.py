from __future__ import annotations

import csv
import importlib.util
import io
import json
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


def test_final_price_validation_is_basis_and_date_aware() -> None:
    enricher = _load_enricher()
    base = {
        "ticker": "AAA",
        "price_source": "levels_market_structure",
        "current_price": 100.25,
        "_market_latest_price": 100.0,
        "_market_latest_date": "2026-08-10",
        "_level_latest_date": "2026-08-10",
        "_market_price_basis": "adjusted_close",
        "_level_price_basis": "raw_unadjusted_nominal",
    }

    assert enricher._market_level_price_errors([base]) == ([], 0, 1)
    same_basis_bad = {**base, "_level_price_basis": "adjusted_close"}
    assert enricher._market_level_price_errors([same_basis_bad])[0] == [
        "AAA:same_basis_price:100.0!=100.25"
    ]
    stale_level = {**base, "_level_latest_date": "2026-08-07"}
    assert enricher._market_level_price_errors([stale_level])[0] == [
        "AAA:date:2026-08-10!=2026-08-07"
    ]
    unknown_basis = {**base, "_level_price_basis": ""}
    assert enricher._market_level_price_errors([unknown_basis])[0] == [
        "AAA:unsupported_basis:adjusted_close->missing"
    ]


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


def test_final_report_pair_publish_replaces_both_files(tmp_path: Path) -> None:
    enricher = _load_enricher()
    output = tmp_path / "final_target_book.csv"
    manifest = tmp_path / "final_manifest.json"
    staged_output = tmp_path / ".report.staged"
    staged_manifest = tmp_path / ".manifest.staged"
    output.write_text("old report", encoding="utf-8")
    manifest.write_text(
        '{"acceptance":"PASS","version":"old"}', encoding="utf-8"
    )
    staged_output.write_text("new report", encoding="utf-8")
    staged_manifest.write_text(
        '{"acceptance":"PASS","version":"new"}', encoding="utf-8"
    )

    enricher.publish_final_report_pair(
        staged_report=staged_output,
        staged_manifest=staged_manifest,
        output_path=output,
        manifest_path=manifest,
    )

    assert output.read_text(encoding="utf-8") == "new report"
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == "new"
    assert not staged_output.exists()
    assert not staged_manifest.exists()


def test_final_report_pair_publish_restores_old_pair_on_second_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enricher = _load_enricher()
    output = tmp_path / "final_target_book.csv"
    manifest = tmp_path / "final_manifest.json"
    staged_output = tmp_path / ".report.staged"
    staged_manifest = tmp_path / ".manifest.staged"
    output.write_bytes(b"old report")
    manifest.write_bytes(b"old manifest")
    staged_output.write_bytes(b"new report")
    staged_manifest.write_bytes(b"new manifest")
    real_replace = enricher.os.replace
    calls = 0

    def fail_second_replace(source: object, destination: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated manifest swap failure")
        real_replace(source, destination)

    monkeypatch.setattr(enricher.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="simulated manifest swap failure"):
        enricher.publish_final_report_pair(
            staged_report=staged_output,
            staged_manifest=staged_manifest,
            output_path=output,
            manifest_path=manifest,
        )

    assert output.read_bytes() == b"old report"
    assert manifest.read_bytes() == b"old manifest"


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


def test_daily_ib_statement_uses_latest_prior_sealed_cumulative_performance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enricher = _load_enricher()
    runs_root = tmp_path / "runs"
    current_dir = runs_root / "2026-08-17" / "ledger"
    prior_dir = runs_root / "2026-08-14" / "ledger"
    current_dir.mkdir(parents=True)
    prior_dir.mkdir(parents=True)
    daily = tmp_path / "daily.csv"
    daily.write_text(
        "Cash Report,Header,Currency Summary,Currency,Total,Securities,Futures,Paxos,\n"
        "Cash Report,Data,Dividends,Base Currency Summary,113.4,113.4,0,0,\n",
        encoding="utf-8",
    )
    cumulative = tmp_path / "cumulative.csv"
    cumulative.write_text(
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
                    "Total (All Assets),,,100,200,10,20,1,2"
                ),
                (
                    "Cash Report,Header,Currency Summary,Currency,Total,"
                    "Securities,Futures,Paxos,Month to Date,Year to Date"
                ),
                "Cash Report,Data,Dividends,Base Currency Summary,0,0,0,0,3,4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    for path in (
        prior_dir / "broker_statement_sources.csv",
        prior_dir / "ledger_manifest.json",
    ):
        path.write_text("sealed\n", encoding="utf-8")
    prior_source = {
        "period_end": "2026-08-14",
        "source_file": str(cumulative),
        "source_sha256": enricher.sha256_file(cumulative),
        "base_currency": "USD",
    }
    monkeypatch.setattr(
        enricher,
        "_sealed_csv",
        lambda *_args, **_kwargs: ([prior_source], {}),
    )

    result, source, source_path, inputs, age = (
        enricher._cumulative_ib_performance_from_ledger_history(
            runs_root=runs_root,
            ledger_as_of="2026-08-17",
            current_statement_source={
                "period_end": "2026-08-17",
                "source_file": str(daily),
                "source_sha256": enricher.sha256_file(daily),
                "base_currency": "USD",
            },
            max_staleness_days=7,
        )
    )

    assert result["ib_profit_as_of_date"] == "2026-08-14"
    assert result["ib_realized_profit_loss_mtd"] == pytest.approx(14.0)
    assert source == prior_source
    assert source_path == cumulative.resolve()
    assert cumulative.resolve() in inputs
    assert age == 3


def test_cumulative_performance_fallback_never_crosses_month_boundary(
    tmp_path: Path,
) -> None:
    enricher = _load_enricher()
    current_dir = tmp_path / "runs" / "2026-08-03" / "ledger"
    prior_dir = tmp_path / "runs" / "2026-07-31" / "ledger"
    current_dir.mkdir(parents=True)
    prior_dir.mkdir(parents=True)
    daily = tmp_path / "daily.csv"
    daily.write_text(
        "Cash Report,Header,Currency Summary,Currency,Total,Securities,Futures,Paxos,\n",
        encoding="utf-8",
    )

    with pytest.raises(
        enricher.CumulativePerformanceUnavailable,
        match="No same-month sealed IB statement",
    ):
        enricher._cumulative_ib_performance_from_ledger_history(
            runs_root=tmp_path / "runs",
            ledger_as_of="2026-08-03",
            current_statement_source={
                "period_end": "2026-08-03",
                "source_file": str(daily),
                "source_sha256": enricher.sha256_file(daily),
            },
            max_staleness_days=7,
        )

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
