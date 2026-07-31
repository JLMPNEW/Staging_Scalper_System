from __future__ import annotations

import json
from pathlib import Path

import pytest

from industrials.defense.production_activation import (
    promote_rows,
    read_lock_registry,
    register_effective_lock,
)
from industrials.defense.research_artifacts import (
    PILLAR_SCORE_FIELDS,
    PRODUCTION_LOCK_REGISTRY_FIELDS,
    load_production_lock,
    sha256_file,
)


def decision(
    path: Path,
    *,
    asof: str,
    lock_id: str,
    mode: str,
    version: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "pass",
                "promoted": True,
                "asof_date": asof,
                "lock_id": lock_id,
                "scoring_mode": mode,
                "score_model_version": version,
                "promotion_payload": {
                    "lock_id": lock_id,
                    "scoring_mode": mode,
                    "score_model_version": version,
                    "weights": {
                        field: 1.0 / len(PILLAR_SCORE_FIELDS)
                        for field in PILLAR_SCORE_FIELDS
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def write_registry(path: Path, rows: list[dict[str, str]]) -> None:
    import csv

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=PRODUCTION_LOCK_REGISTRY_FIELDS,
        )
        writer.writeheader()
        writer.writerows(rows)


def registry_row(
    *,
    lock_id: str,
    start: str,
    end: str,
    mode: str,
    version: str,
    decision_path: Path,
) -> dict[str, str]:
    return {
        "lock_id": lock_id,
        "effective_from": start,
        "effective_to": end,
        "lock_date": start,
        "train_start_date": "2019-01-04",
        "train_end_date": "2023-04-06",
        "scoring_mode": mode,
        "score_model_version": version,
        "validation_method": "weekly_pit_panel_validation_ic_holdout_backtest",
        "decision_manifest_path": str(decision_path),
        "decision_manifest_sha256": sha256_file(decision_path),
        "enabled": "1",
        "created_at_utc": "2026-07-27T00:00:00+00:00",
    }


def test_effective_lock_registry_preserves_history_and_selects_new_model(
    tmp_path: Path,
) -> None:
    old_decision = tmp_path / "old.json"
    new_decision = tmp_path / "new.json"
    decision(
        old_decision,
        asof="2026-07-02",
        lock_id="old",
        mode="baseline",
        version="defense_shadow_v0.1.0",
    )
    decision(
        new_decision,
        asof="2026-07-27",
        lock_id="new",
        mode="specialized_v1",
        version="defense_specialized_v1",
    )
    registry = tmp_path / "locks.csv"
    write_registry(
        registry,
        [
            registry_row(
                lock_id="old",
                start="2026-07-02",
                end="2026-07-26",
                mode="baseline",
                version="defense_shadow_v0.1.0",
                decision_path=old_decision,
            ),
            registry_row(
                lock_id="new",
                start="2026-07-27",
                end="",
                mode="specialized_v1",
                version="defense_specialized_v1",
                decision_path=new_decision,
            ),
        ],
    )
    config = {
        "oos_calibration_standards": {
            "families": {
                "defense": {
                    "production_lock_registry_csv": str(registry),
                }
            }
        }
    }

    old = load_production_lock(
        config,
        base_dir=tmp_path,
        asof="2026-07-24",
    )
    new = load_production_lock(
        config,
        base_dir=tmp_path,
        asof="2026-07-27",
    )

    assert old is not None
    assert old["lock_id"] == "old"
    assert old["scoring_mode"] == "baseline"
    assert new is not None
    assert new["lock_id"] == "new"
    assert new["scoring_mode"] == "specialized_v1"


def test_effective_lock_registry_rejects_overlaps(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    decision(
        first,
        asof="2026-07-02",
        lock_id="first",
        mode="baseline",
        version="old",
    )
    decision(
        second,
        asof="2026-07-20",
        lock_id="second",
        mode="specialized_v1",
        version="new",
    )
    registry = tmp_path / "locks.csv"
    write_registry(
        registry,
        [
            registry_row(
                lock_id="first",
                start="2026-07-02",
                end="2026-07-24",
                mode="baseline",
                version="old",
                decision_path=first,
            ),
            registry_row(
                lock_id="second",
                start="2026-07-20",
                end="",
                mode="specialized_v1",
                version="new",
                decision_path=second,
            ),
        ],
    )
    config = {
        "oos_calibration_standards": {
            "families": {
                "defense": {
                    "production_lock_registry_csv": str(registry),
                }
            }
        }
    }

    with pytest.raises(ValueError, match="ranges overlap"):
        load_production_lock(config, base_dir=tmp_path, asof="2026-07-21")


def test_registration_closes_previous_lock_and_promotion_stamps_model(
    tmp_path: Path,
) -> None:
    old_decision = tmp_path / "old.json"
    new_decision = tmp_path / "new.json"
    decision(
        old_decision,
        asof="2026-07-02",
        lock_id="old",
        mode="baseline",
        version="old",
    )
    decision(
        new_decision,
        asof="2026-07-27",
        lock_id="new",
        mode="specialized_v1",
        version="new",
    )
    registry = tmp_path / "locks.csv"
    write_registry(
        registry,
        [
            registry_row(
                lock_id="old",
                start="2026-07-02",
                end="",
                mode="baseline",
                version="old",
                decision_path=old_decision,
            )
        ],
    )

    register_effective_lock(
        registry_path=registry,
        lock_id="new",
        effective_from="2026-07-27",
        lock_date="2026-07-24",
        train_start="2019-01-10",
        train_end="2023-04-06",
        score_model_version="new",
        decision_manifest_path=str(new_decision),
        decision_manifest_sha256=sha256_file(new_decision),
        created_at_utc="2026-07-28T00:00:00+00:00",
    )

    locks = read_lock_registry(registry)
    assert locks[0]["effective_to"] == "2026-07-26"
    assert locks[1]["effective_from"] == "2026-07-27"
    row = {
        "ticker": "AAA",
        "rank_ready_flag": "1",
        "model_status": "complete",
        "final_score": "50",
        **{field: "50" for field in PILLAR_SCORE_FIELDS},
    }
    promoted = promote_rows(
        [row],
        weights={field: 1.0 for field in PILLAR_SCORE_FIELDS},
        effective_date="2026-07-27",
        lock_date="2026-07-24",
        train_start="2019-01-10",
        train_end="2023-04-06",
        score_model_version="new",
        lock_id="new",
    )[0]
    assert promoted["score_model_version"] == "new"
    assert promoted["calibration_lock_date"] == "2026-07-24"
    assert promoted["calibration_production_start_date"] == "2026-07-27"
    assert promoted["portfolio_candidate_gate"] == "1"
