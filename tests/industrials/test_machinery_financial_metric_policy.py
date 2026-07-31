from __future__ import annotations

import runpy
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_partial_capital_raise_dependence_is_classified_as_proxy() -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "scripts"
            / "08_build_industrials_financial_features.py"
        )
    )
    reason = namespace["dynamic_metric_proxy_reason"](
        "capital_raise_dependence",
        {
            "mapped_xbrl",
            "capital_raise_proceeds_partial_component_coverage",
        },
    )

    assert reason == "capital_raise_proceeds_partial_component_coverage"
