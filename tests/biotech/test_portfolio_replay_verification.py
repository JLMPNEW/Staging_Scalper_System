from __future__ import annotations

import csv
from pathlib import Path

from biotech_index.core.portfolio_profitability import ReplayCostModel
from biotech_index.core.portfolio_replay_verification import (
    ReplayVerificationSettings,
    compare_replay_payloads,
    replay_normalized_artifacts,
)


def _write(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_normalized_artifacts_replay_deterministically(tmp_path: Path) -> None:
    price_rows = []
    for day, xbi, aaa in (
        ("2026-01-02", 100.0, 10.0),
        ("2026-01-05", 100.0, 10.0),
        ("2026-01-06", 101.0, 11.0),
        ("2026-01-07", 102.0, 12.0),
    ):
        price_rows.extend(
            [
                {"ticker": "XBI", "bar_date": day, "close": xbi},
                {"ticker": "AAA", "bar_date": day, "close": aaa},
            ]
        )
    _write(tmp_path / "portfolio_replay_price_inputs.csv", ["ticker", "bar_date", "close"], price_rows)
    _write(
        tmp_path / "portfolio_replay_targets.csv",
        ["fold_id", "strategy", "signal_date", "ticker", "target_weight", "avg_dollar_volume"],
        [
            {"fold_id": "f1", "strategy": "challenger", "signal_date": "2026-01-02", "ticker": "AAA", "target_weight": 1.0, "avg_dollar_volume": 1_000_000},
            {"fold_id": "f1", "strategy": "production", "signal_date": "2026-01-02", "ticker": "XBI", "target_weight": 1.0, "avg_dollar_volume": ""},
        ],
    )
    _write(
        tmp_path / "portfolio_replay_terminal_events.csv",
        ["ticker", "terminal_date", "equity_recovery", "recovery_type", "drop_otc_tape"],
        [],
    )
    _write(
        tmp_path / "portfolio_replay_folds.csv",
        ["fold_id", "start_date", "end_date"],
        [{"fold_id": "f1", "start_date": "2026-01-02", "end_date": "2026-01-07"}],
    )
    settings = ReplayVerificationSettings("XBI", effective_trials=3, bootstrap_iterations=25)
    model = ReplayCostModel(
        base_one_way_cost_bps=0.0,
        benchmark_one_way_cost_bps=0.0,
        market_impact_coefficient_bps=0.0,
        max_adv_participation_pct=100.0,
    )
    first = replay_normalized_artifacts(tmp_path, model=model, settings=settings)
    second = replay_normalized_artifacts(tmp_path, model=model, settings=settings)
    assert first == second
    assert float(str(first["candidate_terminal_wealth"])) > float(str(first["incumbent_terminal_wealth"]))
    assert first["candidate_replay_type"] == "independent_challenger_all_folds"
    assert first["independent_challenger_fold_count"] == 1
    assert first["production_fallback_fold_count"] == 0
    assert compare_replay_payloads(first, second)["verification_status"] == "pass"


def test_verification_reports_numeric_mismatch() -> None:
    result = compare_replay_payloads({"terminal_wealth": 10.0}, {"terminal_wealth": 9.0})
    assert result["verification_status"] == "fail"
    assert result["mismatch_count"] == 1
