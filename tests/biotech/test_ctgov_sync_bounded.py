from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_ctgov_sync_is_bounded_and_yields_ticker_order() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "biotech_index" / "scripts" / "03_sync_ctgov_trials.py")
    )
    job_type = namespace["CompanyJob"]
    result_type = namespace["SyncResult"]
    iter_results = namespace["iter_bounded_ordered_results"]
    jobs = [
        job_type(
            company_id=index,
            ticker=f"T{index:02d}",
            company_name=f"Test {index}",
            aliases=(),
            searches=(),
        )
        for index in range(10)
    ]
    started: list[str] = []

    def fake_sync(job: Any, **_kwargs: Any) -> Any:
        started.append(job.ticker)
        return result_type(
            company_id=job.company_id,
            ticker=job.ticker,
            alias_count=0,
            search_count=0,
            study_count=0,
            studies={},
        )

    results = iter_results(
        jobs,
        max_workers=3,
        sync_kwargs={},
        sync_fn=fake_sync,
    )
    first = next(results)

    assert first[0] == 1
    assert len(started) <= 3
    all_results = [first, *list(results)]
    assert [job.ticker for _, job, _ in all_results] == [job.ticker for job in jobs]
    assert sorted(started) == sorted(job.ticker for job in jobs)


def test_ctgov_study_digest_is_key_order_independent() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "biotech_index" / "scripts" / "03_sync_ctgov_trials.py")
    )
    study_digest = namespace["study_digest"]

    assert study_digest({"b": 2, "a": {"d": 4, "c": 3}}) == study_digest(
        {"a": {"c": 3, "d": 4}, "b": 2}
    )
