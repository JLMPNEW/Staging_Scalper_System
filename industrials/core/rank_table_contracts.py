from __future__ import annotations

import csv
from pathlib import Path


DEFENSE_FINAL_RANK_EXTRA_COLUMNS = [
    "oos_score_asof_date",
    "research_calibration_eligible_flag",
    "market_cap_source",
    "liquidity_capacity_reason",
]


def semiconductor_final_rank_header(project_root: Path) -> list[str]:
    path = project_root / "output" / "technology_reports" / "semi_dashboard" / "semiconductor_final_rank_table.csv"
    if not path.exists():
        raise FileNotFoundError(f"Semiconductor rank-table contract header not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle))


def defense_final_rank_header(project_root: Path) -> list[str]:
    columns = [
        column.replace("big_tech_capex_", "defense_budget_backlog_")
        for column in semiconductor_final_rank_header(project_root)
    ]
    for column in DEFENSE_FINAL_RANK_EXTRA_COLUMNS:
        if column not in columns:
            columns.append(column)
    return columns
