from __future__ import annotations

from pathlib import Path

from industrials.core.rank_table_contracts import (
    DEFENSE_FINAL_RANK_EXTRA_COLUMNS,
    defense_final_rank_header,
    semiconductor_final_rank_header,
)


def test_defense_rank_header_preserves_semiconductor_base_contract() -> None:
    project_root = Path(__file__).resolve().parents[2]

    semiconductor = semiconductor_final_rank_header(project_root)
    defense = defense_final_rank_header(project_root)

    expected_base = [column.replace("big_tech_capex_", "defense_budget_backlog_") for column in semiconductor]
    assert defense[: len(expected_base)] == expected_base
    assert defense[-len(DEFENSE_FINAL_RANK_EXTRA_COLUMNS) :] == DEFENSE_FINAL_RANK_EXTRA_COLUMNS
    assert len(defense) == len(expected_base) + len(DEFENSE_FINAL_RANK_EXTRA_COLUMNS)
