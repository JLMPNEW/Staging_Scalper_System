from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_sec_sync_submits_only_one_live_company_per_worker() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "biotech_index" / "scripts" / "06_sync_sec_filings.py")
    )
    company_type = namespace["Company"]
    result_type = namespace["CompanySyncResult"]
    iter_results = namespace["iter_bounded_company_results"]
    companies = [
        company_type(
            company_id=index,
            ticker=f"T{index}",
            company_name=f"Test {index}",
            cik=str(index),
        )
        for index in range(10)
    ]
    started: list[str] = []

    def fake_sync(company: Any, **_kwargs: Any) -> Any:
        started.append(company.ticker)
        return result_type(company=company, filings=[])

    results = iter_results(
        companies,
        max_workers=3,
        sync_kwargs={},
        sync_fn=fake_sync,
    )
    first = next(results)

    assert first[0] == 1
    assert len(started) <= 3
    assert len([first, *list(results)]) == len(companies)
    assert sorted(started) == sorted(company.ticker for company in companies)
