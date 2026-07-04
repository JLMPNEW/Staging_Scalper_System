from __future__ import annotations

import pytest

from industrials.defense.research_artifacts import (
    DEFAULT_PILLAR_WEIGHTS,
    PILLAR_SCORE_FIELDS,
    forward_window_calendar_days,
    normalize_weights,
    purged_split_snapshot_dates,
    spearman,
    split_snapshot_dates,
    weighted_score,
)


def test_spearman_handles_monotonic_and_reverse_relationships() -> None:
    assert spearman([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == pytest.approx(1.0)
    assert spearman([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]) == pytest.approx(-1.0)


def test_split_snapshot_dates_requires_at_least_three_dates() -> None:
    assert split_snapshot_dates(["2026-07-02", "2026-07-03"]) == {
        "2026-07-02": "insufficient_history",
        "2026-07-03": "insufficient_history",
    }


def test_split_snapshot_dates_assigns_train_validation_holdout() -> None:
    dates = [f"2026-07-{day:02d}" for day in range(1, 11)]
    splits = split_snapshot_dates(dates)

    assert list(splits.values()).count("train") == 6
    assert list(splits.values()).count("validation") == 2
    assert list(splits.values()).count("holdout") == 2


def test_weighted_score_uses_default_weights_when_all_weights_zero() -> None:
    row = {field: "50" for field in PILLAR_SCORE_FIELDS}
    row["final_score"] = "75"

    assert normalize_weights({field: 0.0 for field in PILLAR_SCORE_FIELDS}) == DEFAULT_PILLAR_WEIGHTS
    assert weighted_score(row, {field: 0.0 for field in PILLAR_SCORE_FIELDS}) == 50.0


def test_forward_window_calendar_days_converts_trading_days_and_adds_embargo() -> None:
    assert forward_window_calendar_days(63, 0) == 89  # ceil(63 * 7/5)
    assert forward_window_calendar_days(63, 21) == 110
    assert forward_window_calendar_days(0, 0) == 0


def test_purged_split_relabels_boundary_overlap_as_embargo() -> None:
    # 10 weekly snapshots -> base split 6 train / 2 validation / 2 holdout.
    # forward window of 5 trading days (~7 calendar) + 0 embargo purges any
    # train snapshot within 7 days of the first validation snapshot, and any
    # validation snapshot within 7 days of the first holdout snapshot.
    dates = [f"2026-01-{day:02d}" for day in (2, 9, 16, 23, 30)] + [
        "2026-02-06",
        "2026-02-13",
        "2026-02-20",
        "2026-02-27",
        "2026-03-06",
    ]
    base = split_snapshot_dates(dates)
    purged = purged_split_snapshot_dates(dates, forward_days=5, embargo_days=0)

    # Last train snapshot (2026-02-06) is exactly 7 days before the first
    # validation snapshot (2026-02-13) -> purged. Last validation snapshot
    # (2026-02-20) is 7 days before first holdout (2026-02-27) -> purged.
    assert base["2026-02-06"] == "train" and purged["2026-02-06"] == "embargo"
    assert base["2026-02-20"] == "validation" and purged["2026-02-20"] == "embargo"
    # Earlier snapshots keep their labels; holdout is never purged.
    assert purged["2026-01-02"] == "train"
    assert purged["2026-02-27"] == "holdout"
    assert purged["2026-03-06"] == "holdout"
    # No forward window -> purge is a no-op.
    assert purged_split_snapshot_dates(dates, forward_days=0, embargo_days=0) == base
