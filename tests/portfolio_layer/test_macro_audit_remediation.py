from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MACRO_ROOT = PROJECT_ROOT / "portfolio_layer" / "MacroLayer"
if str(MACRO_ROOT) not in sys.path:
    sys.path.insert(0, str(MACRO_ROOT))

from connectors.eia_seriesid import EiaSeriesIdConnector, _extract_total, _parse_float  # noqa: E402
from connectors.fred_alfred import (  # noqa: E402
    ALFRED_EARLIEST_REALTIME_DATE,
    FredAlfredConnector,
)
from macro_raw_config import load_macro_raw_config  # noqa: E402
from macro_storage import _upsert_observations, _upsert_sync_state, init_db  # noqa: E402
from macro_types import FetchResult, FetchTask, ObservationRecord  # noqa: E402
from run_macro_raw_pipeline import (  # noqa: E402
    _normalize_oecd_bundle_windows,
    _required_source_api_key,
    build_fetch_tasks,
)
from connectors.phillyfed_ads import _wide_ads_to_long  # noqa: E402
from backfill_cfnai_first_prints import _iter_month_keys  # noqa: E402
from build_macro_composites import _validate_policy_feature_pairs  # noqa: E402


def _spec(**overrides: object) -> SimpleNamespace:
    values = {
        "registry_key": "metric|fred",
        "metric_key": "metric",
        "source_name": "fred_alfred",
        "source_dataset": "dataset",
        "source_series_id": "SERIES",
        "ref_area": "USA",
        "frequency": "monthly",
        "seasonal_adjustment": "SA",
        "units": "index",
        "vintage_policy": "true_vintage",
        "history_start_date": date(2000, 1, 1),
        "revision_window_days": 30,
        "notes": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _observation(value: float, retrieved_at: str) -> ObservationRecord:
    return ObservationRecord(
        metric_key="metric",
        source_name="eia_seriesid",
        source_dataset="dataset",
        source_series_id="SERIES",
        ref_area="USA",
        frequency="monthly",
        seasonal_adjustment=None,
        units="index",
        observation_period="2026-01",
        observation_date="2026-01-01",
        release_date=None,
        vintage_date=None,
        value=value,
        source_last_updated=None,
        retrieved_at=retrieved_at,
        revision_flag=0,
        notes_hash=None,
    )


def test_true_vintage_task_never_truncates_realtime_history() -> None:
    tasks = build_fetch_tasks(
        specs=[_spec()],
        state={"metric|fred": {"last_vintage_date": "2026-07-01"}},
        as_of_date=date(2026, 8, 1),
        mode="daily",
    )
    assert tasks[0].vintage_start == date(1776, 7, 4)
    assert tasks[0].observation_start == date(2000, 1, 1)


def test_fred_connector_uses_full_realtime_range() -> None:
    class Response:
        fetched_at = "2026-08-01T00:00:00Z"
        url = "https://example.test"
        status_code = 200
        content = b"{}"

        @staticmethod
        def json() -> dict[str, object]:
            return {"count": 0, "offset": 0, "limit": 100000, "observations": []}

    class Client:
        def __init__(self) -> None:
            self.params: dict[str, object] | None = None

        def get(self, _url: str, *, params: dict[str, object]) -> Response:
            self.params = params
            return Response()

    client = Client()
    connector = FredAlfredConnector(client, "not-a-real-key")
    task = FetchTask(
        spec=_spec(),
        observation_start=date(2000, 1, 1),
        observation_end=date(2026, 8, 1),
        vintage_start=date(2026, 7, 1),
        as_of_date=date(2026, 8, 1),
    )
    connector._paged_observation_payloads(task=task, output_type=1, compact_revisions=True)
    assert client.params is not None
    assert client.params["realtime_start"] == ALFRED_EARLIEST_REALTIME_DATE


def test_eia_parses_zero_and_missing_total_is_unknown() -> None:
    assert _parse_float({"value": 0}) == 0.0
    assert _extract_total({"response": {"data": []}}) is None


def test_eia_paginates_when_total_is_omitted() -> None:
    class Response:
        fetched_at = "2026-08-01T00:00:00Z"
        url = "https://example.test"
        status_code = 200
        content = b"{}"

        def __init__(self, rows: list[dict[str, object]]) -> None:
            self._rows = rows

        def json(self) -> dict[str, object]:
            return {"response": {"data": self._rows}}

    class Client:
        def __init__(self) -> None:
            self.offsets: list[int] = []

        def get(self, _url: str, *, params: dict[str, str]) -> Response:
            offset = int(params["offset"])
            self.offsets.append(offset)
            count = 5000 if offset == 0 else 1
            return Response([{"period": "2026", "value": 0}] * count)

    client = Client()
    connector = EiaSeriesIdConnector(client, "not-a-real-key")
    task = FetchTask(_spec(source_name="eia_seriesid"), None, None, None, date(2026, 8, 1))
    pages = connector._paged_rows(task)
    assert len(pages) == 2
    assert client.offsets == [0, 5000]


def test_non_vintage_revisions_are_append_only_and_unchanged_fetches_are_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    assert _upsert_observations(
        conn,
        run_id="run-1",
        registry_key="metric|eia",
        observations=[_observation(1.0, "2026-08-01T00:00:00Z")],
    ) == 1
    assert _upsert_observations(
        conn,
        run_id="run-2",
        registry_key="metric|eia",
        observations=[_observation(1.0, "2026-08-02T00:00:00Z")],
    ) == 0
    assert _upsert_observations(
        conn,
        run_id="run-3",
        registry_key="metric|eia",
        observations=[_observation(2.0, "2026-08-03T00:00:00Z")],
    ) == 1
    rows = conn.execute(
        "SELECT value, revision_flag, retrieved_at FROM macro_observation_raw ORDER BY observation_id"
    ).fetchall()
    assert rows == [
        (1.0, 0, "2026-08-01T00:00:00Z"),
        (2.0, 1, "2026-08-03T00:00:00Z"),
    ]


def test_empty_success_does_not_erase_sync_watermarks() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    spec = _spec(source_name="eia_seriesid", vintage_policy="current_only")
    populated = FetchResult(spec=spec, observations=[_observation(1.0, "2026-08-01T00:00:00Z")])
    _upsert_sync_state(conn, result=populated)
    _upsert_sync_state(conn, result=FetchResult(spec=spec))
    row = conn.execute(
        "SELECT last_observation_date, last_row_count FROM macro_sync_state WHERE registry_key = ?",
        (spec.registry_key,),
    ).fetchone()
    assert row == ("2026-01-01", 1)


def test_config_loader_fails_closed_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_macro_raw_config(tmp_path / "missing.yaml")


def test_api_keys_must_come_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {"sources": {"fred_alfred": {"api_key_env": "TEST_FRED_KEY", "api_key": "literal"}}}
    monkeypatch.delenv("TEST_FRED_KEY", raising=False)
    with pytest.raises(ValueError, match="Literal API keys are forbidden"):
        _required_source_api_key(cfg=cfg, source_name="fred_alfred", default_env_name="FRED_API_KEY")
    monkeypatch.setenv("TEST_FRED_KEY", "environment-value")
    assert _required_source_api_key(
        cfg=cfg, source_name="fred_alfred", default_env_name="FRED_API_KEY"
    ) == "environment-value"
from build_macro_features import (  # noqa: E402
    FeatureState,
    PitDailyRow,
    _daily_row_tuple,
    _lookup_lag_candidate,
    _standardize_events,
)
from macro_serving_common import (  # noqa: E402
    RawCandidate,
    candidate_rank,
    effective_available_date,
    load_metric_serving_specs,
)


def _raw_candidate(*, period: date, vintage: date | None, value: float = 1.0) -> RawCandidate:
    return RawCandidate(
        registry_key=f"metric|{period.isoformat()}",
        metric_key="metric",
        ref_area="USA",
        source_name="fred_alfred",
        source_series_id="SERIES",
        frequency="daily",
        observation_period=period.isoformat(),
        observation_date=period,
        observation_date_text=period.isoformat(),
        release_date=vintage,
        release_date_text=vintage.isoformat() if vintage else None,
        vintage_date=vintage,
        vintage_date_text=vintage.isoformat() if vintage else None,
        effective_available_date=vintage or period,
        effective_available_date_text=(vintage or period).isoformat(),
        value=value,
        retrieved_at="2026-08-01T00:00:00Z",
        source_priority=1,
    )


def _feature_state(period: str, value: float) -> FeatureState:
    return FeatureState(
        as_of_date_text=period,
        raw_value_selected=value,
        transformed_value=value,
        sign_adjusted_value=value,
        zscore_value=None,
        percentile_value=None,
        standardized_value=None,
        registry_key="metric|source",
        source_name="source",
        source_series_id="SERIES",
        observation_period_selected=period,
        observation_date_selected=period,
        release_date_selected=None,
        vintage_date_selected=None,
        effective_available_date_selected=period,
        staleness_days=0,
        max_staleness_days=30,
        source_quality_weight=1.0,
        coverage_flag=1,
    )


def test_non_vintage_availability_never_predates_period_end_plus_lag() -> None:
    # No release/vintage metadata: availability is period end + cadence lag
    # (45 days for monthly), never the period start.
    assert effective_available_date(
        observation_date=date(2026, 1, 1),
        release_date=None,
        vintage_date=None,
        retrieved_at="2026-03-15T12:00:00Z",
        frequency="monthly",
        source_name="eia_seriesid",
    ) == date(2026, 3, 17)
    assert effective_available_date(
        observation_date=date(2026, 1, 1),
        release_date=date(2026, 2, 10),
        vintage_date=date(2026, 2, 10),
        retrieved_at="2026-03-15T12:00:00Z",
        frequency="monthly",
    ) == date(2026, 2, 10)


def test_backfilled_history_availability_is_not_clamped_to_retrieval() -> None:
    # Backfilled non-vintage history must keep realistic publication-date
    # availability; clamping to the ingest date collapses decades of history
    # onto a handful of retrieval dates and starves standardization windows.
    assert effective_available_date(
        observation_date=date(2005, 6, 1),
        release_date=None,
        vintage_date=None,
        retrieved_at="2026-04-11T12:00:00Z",
        frequency="daily",
        source_name="fred_alfred",
    ) == date(2005, 6, 2)
    assert effective_available_date(
        observation_date=date(2005, 6, 1),
        release_date=None,
        vintage_date=None,
        retrieved_at="2026-04-11T12:00:00Z",
        frequency="monthly",
        source_name="oecd_sdmx",
    ) == date(2005, 6, 30) + timedelta(days=45)


def test_newer_observation_period_outranks_later_revision_of_old_period() -> None:
    old_revised = _raw_candidate(period=date(2025, 12, 1), vintage=date(2026, 8, 1))
    newer_period = _raw_candidate(period=date(2026, 1, 1), vintage=date(2026, 2, 1))
    assert candidate_rank(newer_period) > candidate_rank(old_revised)


def test_metric_serving_spec_comes_from_most_populated_registry() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = "2026-08-01T00:00:00Z"
    registry_sql = """
        INSERT INTO macro_metric_registry (
            registry_key, metric_key, regime_block, source_name, ref_area, frequency,
            vintage_policy, update_cadence, revision_window_days, source_priority,
            worker_hint, enabled, created_at_utc, updated_at_utc
        ) VALUES (?, 'metric', 'growth', ?, ?, ?, 'current_only', 'monthly', 0, ?, 1, 1, ?, ?)
    """
    conn.execute(registry_sql, ("thin", "source-a", "WRONG", "daily", 2, now, now))
    conn.execute(registry_sql, ("deep", "source-b", "USA", "monthly", 1, now, now))
    for index in range(3):
        obs = _observation(float(index), f"2026-08-0{index + 1}T00:00:00Z")
        obs = ObservationRecord(**{**obs.__dict__, "observation_period": f"2026-0{index + 1}", "observation_date": f"2026-0{index + 1}-01"})
        _upsert_observations(conn, run_id=f"run-{index}", registry_key="deep", observations=[obs])
    specs = load_metric_serving_specs(conn)
    assert [(item.metric_key, item.ref_area, item.frequency) for item in specs] == [
        ("metric", "USA", "monthly")
    ]


def test_daily_lag_uses_elapsed_days_not_prior_event_count() -> None:
    policy = SimpleNamespace(lookback_periods=7, frequency="daily")
    periods = [date(2026, 1, 1), date(2026, 1, 5), date(2026, 1, 10)]
    best = {period: _raw_candidate(period=period, vintage=period) for period in periods}
    lagged = _lookup_lag_candidate(
        policy=policy,
        current_period=date(2026, 1, 10),
        best_by_period=best,
        sorted_periods=periods,
    )
    assert lagged is not None and lagged.observation_date == date(2026, 1, 1)


def test_standardization_replaces_revisions_in_same_period() -> None:
    policy = SimpleNamespace(
        zscore_window=3,
        percentile_window=3,
        min_history_periods=3,
        standardized_clip_min=None,
        standardized_clip_max=None,
    )
    states = [
        _feature_state("2026-01-01", 1.0),
        _feature_state("2026-02-01", 2.0),
        _feature_state("2026-02-01", 20.0),
        _feature_state("2026-03-01", 3.0),
    ]
    standardized = _standardize_events(states, policy)
    assert standardized[2].zscore_value is None
    assert standardized[3].zscore_value is not None
from build_macro_probabilities import (  # noqa: E402
    PROBABILITY_SPECS,
    _build_calibration_and_diagnostics,
    _build_monthly_probability_dataset,
)
from build_macro_regime_decision import (  # noqa: E402
    DecisionConfig,
    TrackState,
    _evaluate_track,
)
from build_macro_regime_smoothed import SmoothingConfig, _build_smoothed_outputs  # noqa: E402


def test_now_probability_uses_prior_history_direct_mapping() -> None:
    periods = pd.period_range("2026-01", periods=4, freq="M")
    value_map = pd.DataFrame({"G_NOW": [-1.0, 0.0, 1.0, 2.0]}, index=periods)
    date_map = pd.DataFrame(
        {"G_NOW": pd.to_datetime(["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"])},
        index=periods,
    )
    spec = next(item for item in PROBABILITY_SPECS if item.probability_key == "P_G_NOW")
    dataset = _build_monthly_probability_dataset(value_map=value_map, date_map=date_map, spec=spec)
    calibration, diagnostics = _build_calibration_and_diagnostics(
        dataset=dataset,
        spec=spec,
        min_training_months=2,
        min_positive_months=99,
        min_negative_months=99,
        ridge_penalty=2.5,
        logloss_clip=1e-6,
        output_probability_floor=0.02,
    )
    assert calibration["training_sample_count"].tolist() == [0, 1, 2, 3]
    ready = calibration[calibration["calibration_ready_flag"].eq(1)]
    assert ready["intercept_value"].eq(0.0).all()
    assert ready["slope_value"].eq(1.0).all()
    assert diagnostics["train_auc"].isna().all()


def _decision_cfg() -> DecisionConfig:
    return DecisionConfig(
        decision_frequency="W-FRI",
        min_top_probability=0.50,
        min_confidence=0.10,
        switch_margin=0.05,
        confirm_periods=2,
        min_incumbent_probability=0.15,
        incumbent_breach_periods=2,
    )


def test_regime_initialization_requires_decision_date_gates_and_confirmation() -> None:
    probs = np.asarray([0.60, 0.20, 0.10, 0.10])
    state = TrackState(None, None, 0)
    first = _evaluate_track(state=state, probs=probs, decision_date_flag=False, cfg=_decision_cfg())
    assert first.state.active_regime is None
    assert first.reason == "AWAITING_INITIALIZATION_DECISION_DATE"
    second = _evaluate_track(state=first.state, probs=probs, decision_date_flag=True, cfg=_decision_cfg())
    assert second.state.active_regime is None and second.state.pending_count == 1
    third = _evaluate_track(state=second.state, probs=probs, decision_date_flag=True, cfg=_decision_cfg())
    assert third.state.active_regime == "EXPANSION_DISINFLATION"


def test_incumbent_floor_eventually_breaks_threshold_deadlock() -> None:
    probs = np.asarray([0.10, 0.36, 0.28, 0.26])
    state = TrackState("EXPANSION_DISINFLATION", None, 0)
    outcomes = []
    for _ in range(3):
        outcome = _evaluate_track(state=state, probs=probs, decision_date_flag=True, cfg=_decision_cfg())
        outcomes.append(outcome)
        state = outcome.state
    assert outcomes[0].switch_flag == 0
    assert outcomes[1].state.pending_count == 1
    assert outcomes[2].switch_flag == 1
    assert outcomes[2].state.active_regime == "HEATING_UP"


def test_next_regime_smoothing_uses_configured_transition_horizon() -> None:
    raw = pd.DataFrame(
        {
            "as_of_date": pd.to_datetime(["2026-01-02"]),
            "p_current_expansion_disinflation": [1.0],
            "p_current_heating_up": [0.0],
            "p_current_slow_growth": [0.0],
            "p_current_stagflation": [0.0],
            "p_next_3m_expansion_disinflation": [1.0],
            "p_next_3m_heating_up": [0.0],
            "p_next_3m_slow_growth": [0.0],
            "p_next_3m_stagflation": [0.0],
            "coverage_flag": [1],
        }
    )
    base = dict(
        transition_prior_strength=24.0,
        persistence_weight=6.0,
        adjacent_weight=1.5,
        opposite_weight=0.35,
        current_blend=0.0,
        next_blend=1.0,
        transition_bias_deadband=0.05,
    )
    one_step, _, _ = _build_smoothed_outputs(
        raw,
        write_start_date=date(2026, 1, 2),
        write_end_date=date(2026, 1, 2),
        cfg=SmoothingConfig(**base, next_horizon_steps=1),
    )
    long_horizon, _, _ = _build_smoothed_outputs(
        raw,
        write_start_date=date(2026, 1, 2),
        write_end_date=date(2026, 1, 2),
        cfg=SmoothingConfig(**base, next_horizon_steps=63),
    )
    assert one_step.loc[0, "p_smoothed_next_3m_expansion_disinflation"] > long_horizon.loc[
        0, "p_smoothed_next_3m_expansion_disinflation"
    ]
from macro_allocation import bounded_normalize, hierarchical_bounded_normalize  # noqa: E402
from run_macro_optimizer_integration import (  # noqa: E402
    _TIER1_IMPORT_ERROR,
    _case_config,
    _resolve_base_config,
    _verify_fresh_outputs,
)
from check_macro_optimizer_integration import _manifest_errors  # noqa: E402


def test_bounded_normalize_enforces_sum_cap_and_zero_score_fallback() -> None:
    weights = bounded_normalize(
        pd.Series([10.0, 0.0, 0.0], index=["A", "B", "C"]),
        upper=0.40,
        target_sum=1.0,
    )
    assert float(weights.sum()) == pytest.approx(1.0)
    assert float(weights.max()) <= 0.40 + 1e-12
    assert weights["B"] > 0.0 and weights["C"] > 0.0


def test_hierarchical_allocator_enforces_item_and_sector_caps() -> None:
    raw = pd.Series([10.0, 1.0, 2.0, 1.0, 1.0, 1.0], index=list("ABCDEF"))
    groups = pd.Series(["S1", "S1", "S2", "S2", "S3", "S3"], index=raw.index)
    weights = hierarchical_bounded_normalize(
        raw,
        groups,
        item_cap=0.30,
        group_cap=0.40,
        target_sum=1.0,
    )
    assert float(weights.sum()) == pytest.approx(1.0)
    assert float(weights.max()) <= 0.30 + 1e-12
    assert bool(weights.groupby(groups).sum().le(0.40 + 1e-12).all())


def test_bounded_allocator_fails_on_infeasible_caps() -> None:
    with pytest.raises(ValueError, match="Infeasible allocation bounds"):
        bounded_normalize(pd.Series([1.0, 1.0]), upper=0.40, target_sum=1.0)


def test_stage12d_imports_optimizer_and_selects_accepted_sealed_base() -> None:
    assert _TIER1_IMPORT_ERROR is None
    config_path, cfg = load_macro_raw_config(MACRO_ROOT / "config_macro_raw.yaml")
    layer_cfg = cfg["optimizer_integration_layer"]
    base = _resolve_base_config(config_path, layer_cfg, None)
    manifest = json.loads(base.with_name("bl_manifest.json").read_text(encoding="utf-8"))
    assert manifest["acceptance"] == "PASS"
    assert base.name == "bl_optimizer_config.yaml"


def test_stage12d_case_config_wires_absolute_macro_inputs(tmp_path: Path) -> None:
    config_path, cfg = load_macro_raw_config(MACRO_ROOT / "config_macro_raw.yaml")
    layer_cfg = cfg["optimizer_integration_layer"]
    base_path = _resolve_base_config(config_path, layer_cfg, None)
    import yaml

    base_cfg = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    case_cfg = _case_config(
        base_cfg,
        {"macro_enabled": True, "foreign_enabled": True, "long_short_enabled": False},
        tmp_path,
        base_path,
        layer_cfg,
        config_path,
    )
    tier1 = case_cfg.get("tier1_optimizer", case_cfg)
    adapter = tier1["macro_optimizer_integration"]
    assert adapter["enabled"] is True
    assert Path(adapter["inputs"]["stock_csv"]).is_absolute()
    assert Path(adapter["stock_targets"]["industry_targets_csv"]).is_absolute()
from implement_stage12d_priority_order import (  # noqa: E402
    _merge_weights,
    _read_acceptance,
    _read_weights,
    _sha256_file as _priority_sha256_file,
    build_case_comparison,
    choose_candidate,
)


def test_priority_selection_requires_acceptance_evidence() -> None:
    comparison = pd.DataFrame(
        [{"case_name": "macro_full", "sharpe_ann": 1.0, "exp_return_ann": 0.1, "vol_ann": 0.1}]
    )
    with pytest.raises(ValueError, match="fail-closed"):
        choose_candidate(
            comparison,
            pd.DataFrame(),
            preferred_case="macro_full",
            tie_break_case="macro_full",
        )


def test_case_comparison_excludes_long_short_rows() -> None:
    summary = pd.DataFrame(
        [
            {"case_name": "baseline", "portfolio": "LONG_ONLY", "sharpe_ann": 0.5, "exp_return_ann": 0.1, "vol_ann": 0.2},
            {"case_name": "baseline", "portfolio": "LONG_SHORT", "sharpe_ann": 9.0, "exp_return_ann": 0.9, "vol_ann": 0.1},
            {"case_name": "macro", "portfolio": "LONG_ONLY", "sharpe_ann": 0.6, "exp_return_ann": 0.1, "vol_ann": 0.2},
        ]
    )
    comparison = build_case_comparison(summary, "baseline")
    assert set(comparison["portfolio"]) == {"LONG_ONLY"}
    assert len(comparison) == 2


def test_weight_merge_accepts_outputs_without_low_high_columns() -> None:
    target = pd.DataFrame({"Ticker": ["A"], "Weight": [0.6]})
    baseline = pd.DataFrame({"Ticker": ["A"], "Weight": [0.5]})
    merged = _merge_weights(target, baseline)
    assert merged.loc[0, "delta_weight"] == pytest.approx(0.1)
from build_macro_stock_overlay import (  # noqa: E402
    _build_overlay_frames,
    _resolve_layer_config as _resolve_stock_overlay_config,
    _zscore_by_date as _stock_zscore_by_date,
)


def test_stock_zscore_preserves_nonconsecutive_index_alignment() -> None:
    frame = pd.DataFrame(
        {"as_of_date": pd.to_datetime(["2026-01-01", "2026-01-01"]), "value": [1.0, 3.0]},
        index=[10, 30],
    )
    z = _stock_zscore_by_date(frame, "value", min_std=1e-9, clip_value=3.0)
    assert list(z.index) == [10, 30]
    assert z.loc[10] < 0 < z.loc[30]


def test_stock_overlay_uses_configured_sector_map_and_keeps_missing_macro_uncovered() -> None:
    config_path, cfg = load_macro_raw_config(MACRO_ROOT / "config_macro_raw.yaml")
    layer_cfg = _resolve_stock_overlay_config(cfg, config_path)
    dt = pd.Timestamp("2026-01-02")
    score = pd.DataFrame(
        {
            "as_of_date": [dt, dt],
            "ticker": ["H", "T"],
            "company": ["Health", "Tech"],
            "sector_name": ["Healthcare", "Technology"],
            "industry_aggregate_name": ["Health Agg", "Tech Agg"],
            "industry_name": ["Health Ind", "Tech Ind"],
            "rating": ["Buy", "Buy"],
            "base_score": [80.0, 70.0],
            "base_optimizer_eligible": [1, 1],
            "earnings_blocked_7d": [0, 0],
            "SnapshotSource": ["source", "source"],
            "ScoreApproach": ["score", "score"],
            "RunId": ["run", "run"],
            "source_pipeline": ["not-a-sector", "also-not-a-sector"],
        }
    )
    industry = pd.DataFrame(
        {
            "as_of_date": [dt],
            "sector_name": ["Healthcare"],
            "industry_aggregate_name": ["Health Agg"],
            "industry_name": ["Health Ind"],
            "industry_macro_fit": [1.5],
            "industry_shock_prior_score": [0.2],
            "industry_macro_coverage_flag": [1],
        }
    )
    aggregate = pd.DataFrame(
        {
            "as_of_date": [dt, dt],
            "sector_name": ["Healthcare", "Technology"],
            "industry_aggregate_name": ["Health Agg", "Tech Agg"],
            "industry_aggregate_macro_fit": [1.0, 1.0],
            "aggregate_macro_coverage_flag": [1, 1],
        }
    )
    sector = pd.DataFrame(
        {
            "as_of_date": [dt, dt],
            "sector_name": ["Healthcare", "Technology"],
            "sector_macro_fit": [0.5, 0.5],
            "sector_shock_prior_score": [0.1, 0.1],
            "sector_macro_coverage_flag": [1, 1],
        }
    )
    tactical = pd.DataFrame(
        {
            "as_of_date": [dt, dt],
            "rotation_sector_name": ["Health Care", "Technology"],
            "sector_tactical_lift": [0.7, 0.4],
            "sector_tactical_lift_z": [1.0, -1.0],
        }
    )
    fit, _, _, _ = _build_overlay_frames(
        score_panel=score,
        industry_macro=industry,
        aggregate_macro=aggregate,
        sector_macro=sector,
        tactical=tactical,
        validation_returns=pd.DataFrame(),
        layer_cfg=layer_cfg,
    )
    health = fit.loc[fit["ticker"].eq("H")].iloc[0]
    tech = fit.loc[fit["ticker"].eq("T")].iloc[0]
    assert health["sector_tactical_lift"] == pytest.approx(0.7)
    assert health["macro_stock_fit_raw"] == pytest.approx(1.5)
    assert int(tech["coverage_flag"]) == 0
    assert pd.isna(tech["macro_stock_fit_raw"])
from build_macro_portfolio_inputs import _percentile_by_date  # noqa: E402
from run_macro_shadow_backtest import _non_overlapping_periods  # noqa: E402
from staging_portfolio_adapter import latest_accepted_survivorship_panel  # noqa: E402


def test_eia_rejects_repeated_full_pages() -> None:
    class Response:
        fetched_at = "2026-08-01T00:00:00Z"
        url = "https://example.test"
        status_code = 200
        content = b"{}"

        @staticmethod
        def json() -> dict[str, object]:
            return {"response": {"data": [{"period": "2026", "value": 1}] * 5000}}

    class Client:
        @staticmethod
        def get(_url: str, *, params: dict[str, str]) -> Response:
            return Response()

    connector = EiaSeriesIdConnector(Client(), "not-a-real-key")
    task = FetchTask(_spec(source_name="eia_seriesid"), None, None, None, date(2026, 8, 1))
    with pytest.raises(RuntimeError, match="repeated a page"):
        connector._paged_rows(task)


def test_first_empty_fetch_is_not_recorded_as_success() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    spec = _spec(source_name="eia_seriesid", vintage_policy="current_only")
    _upsert_sync_state(conn, result=FetchResult(spec=spec))
    row = conn.execute(
        "SELECT last_success_at_utc, last_row_count FROM macro_sync_state WHERE registry_key = ?",
        (spec.registry_key,),
    ).fetchone()
    assert row == (None, 0)


def test_partial_month_label_is_not_available_before_month_end() -> None:
    periods = pd.period_range("2026-07", periods=2, freq="M")
    value_map = pd.DataFrame({"G_NOW": [1.0, 2.0]}, index=periods)
    date_map = pd.DataFrame(
        {"G_NOW": pd.to_datetime(["2026-07-31", "2026-08-02"])},
        index=periods,
    )
    spec = next(item for item in PROBABILITY_SPECS if item.probability_key == "P_G_NOW")
    dataset = _build_monthly_probability_dataset(value_map=value_map, date_map=date_map, spec=spec)
    assert pd.Timestamp(dataset.loc[pd.Period("2026-08", freq="M"), "label_available_date"]) == pd.Timestamp("2026-08-31")


def test_percentile_preserves_missing_scores() -> None:
    frame = pd.DataFrame(
        {"as_of_date": pd.to_datetime(["2026-01-01"] * 3), "score": [1.0, np.nan, 3.0]}
    )
    result = _percentile_by_date(frame, "score")
    assert pd.isna(result.iloc[1])
    assert result.iloc[0] == pytest.approx(0.5)
    assert result.iloc[2] == pytest.approx(1.0)


def test_survivorship_selector_requires_pass_manifest(tmp_path: Path) -> None:
    failed = tmp_path / "2026-08-02"
    passed = tmp_path / "2026-08-01"
    failed.mkdir()
    passed.mkdir()
    (failed / "survivorship_manifest.json").write_text(json.dumps({"acceptance": "FAIL"}), encoding="utf-8")
    (passed / "survivorship_manifest.json").write_text(json.dumps({"acceptance": "PASS"}), encoding="utf-8")
    assert latest_accepted_survivorship_panel(tmp_path) == passed.resolve()


def test_shadow_statistics_schedule_is_non_overlapping() -> None:
    periods = pd.DataFrame(
        {
            "case_name": ["a", "b", "a", "b", "a", "b"],
            "as_of_date": ["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02", "2026-01-08", "2026-01-08"],
            "entry_date": ["2026-01-02", "2026-01-02", "2026-01-05", "2026-01-05", "2026-01-09", "2026-01-09"],
            "exit_date": ["2026-01-09", "2026-01-09", "2026-01-12", "2026-01-12", "2026-01-16", "2026-01-16"],
        }
    )
    selected = _non_overlapping_periods(periods)
    assert set(selected["as_of_date"]) == {"2026-01-01", "2026-01-08"}
    assert selected.groupby("case_name").size().to_dict() == {"a": 2, "b": 2}

def test_optimizer_outputs_must_be_freshly_rewritten(tmp_path: Path) -> None:
    weights = tmp_path / "weights_long_only.csv"
    weights.write_text("Ticker,Weight,Sleeve\nA,1.0,DOMESTIC\n", encoding="utf-8")
    os.utime(weights, ns=(1_000_000_000, 1_000_000_000))
    previous_mtime = weights.stat().st_mtime_ns
    weights.write_text("Ticker,Weight,Sleeve\nB,1.0,DOMESTIC\n", encoding="utf-8")
    hashes = _verify_fresh_outputs(
        tmp_path,
        {weights.name: previous_mtime},
        started_ns=previous_mtime + 1,
    )
    assert hashes[weights.name] == _priority_sha256_file(weights)
    with pytest.raises(RuntimeError, match="not freshly written"):
        _verify_fresh_outputs(
            tmp_path,
            {weights.name: weights.stat().st_mtime_ns},
            started_ns=weights.stat().st_mtime_ns,
        )


def test_optimizer_case_manifest_detects_output_tampering(tmp_path: Path) -> None:
    input_path = tmp_path / "input.yaml"
    output_path = tmp_path / "weights_long_only.csv"
    input_path.write_text("value: 1\n", encoding="utf-8")
    output_path.write_text("Ticker,Weight,Sleeve\nA,1.0,DOMESTIC\n", encoding="utf-8")
    manifest = {
        "run_id": "run-1",
        "case_name": "baseline",
        "status": "completed",
        "input_files": {
            "base_config": {"path": str(input_path), "sha256": _priority_sha256_file(input_path)}
        },
        "output_sha256": {output_path.name: _priority_sha256_file(output_path)},
    }
    (tmp_path / "stage12d_case_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert _manifest_errors(tmp_path, case_name="baseline", expected_run_id="run-1") == []
    output_path.write_text("Ticker,Weight,Sleeve\nB,1.0,DOMESTIC\n", encoding="utf-8")
    assert "output_hash_mismatch:weights_long_only.csv" in _manifest_errors(
        tmp_path,
        case_name="baseline",
        expected_run_id="run-1",
    )


def test_priority_reader_requires_sealed_summary_case_manifest_and_weights(tmp_path: Path) -> None:
    run_id = "run-1"
    case_name = "baseline"
    case_dir = tmp_path / case_name
    checks_dir = tmp_path / "checks"
    case_dir.mkdir()
    checks_dir.mkdir()
    weights_path = case_dir / "weights_long_only.csv"
    weights_path.write_text("Ticker,Weight,Sleeve\nA,1.0,DOMESTIC\n", encoding="utf-8")
    case_manifest_path = case_dir / "stage12d_case_manifest.json"
    case_manifest_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "case_name": case_name,
                "status": "completed",
                "output_sha256": {weights_path.name: _priority_sha256_file(weights_path)},
            }
        ),
        encoding="utf-8",
    )
    summary_path = tmp_path / "stage12d_optimizer_case_summary.csv"
    summary_path.write_text(
        "run_id,case_name,portfolio,sharpe_ann,exp_return_ann,vol_ann\n"
        "run-1,baseline,LONG_ONLY,1.0,0.1,0.1\n",
        encoding="utf-8",
    )
    acceptance_path = checks_dir / "stage12d_optimizer_acceptance_summary.csv"
    acceptance_path.write_text(
        "run_id,case_name,check_name,value,threshold,passed\n"
        "run-1,baseline,weights_schema,1,1,1\n",
        encoding="utf-8",
    )
    breach_path = checks_dir / "stage12d_target_breaches.csv"
    breach_path.write_text("case_name,target_type,name,actual,max_weight\n", encoding="utf-8")
    acceptance_manifest = {
        "run_id": run_id,
        "acceptance": "PASS",
        "case_names": [case_name],
        "case_summary_sha256": _priority_sha256_file(summary_path),
        "case_manifest_sha256": {case_name: _priority_sha256_file(case_manifest_path)},
        "files": {
            acceptance_path.name: _priority_sha256_file(acceptance_path),
            breach_path.name: _priority_sha256_file(breach_path),
        },
    }
    (checks_dir / "stage12d_optimizer_acceptance_manifest.json").write_text(
        json.dumps(acceptance_manifest),
        encoding="utf-8",
    )

    acceptance = _read_acceptance(tmp_path)
    assert not acceptance.empty
    assert acceptance.attrs["run_id"] == run_id
    assert _read_weights(tmp_path, case_name, expected_run_id=run_id).loc[0, "Ticker"] == "A"

    weights_path.write_text("Ticker,Weight,Sleeve\nB,1.0,DOMESTIC\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not sealed"):
        _read_weights(tmp_path, case_name, expected_run_id=run_id)
    summary_path.write_text(
        "run_id,case_name,portfolio,sharpe_ann,exp_return_ann,vol_ann\n"
        "run-2,baseline,LONG_ONLY,1.0,0.1,0.1\n",
        encoding="utf-8",
    )
    assert _read_acceptance(tmp_path).empty

def test_monthly_lag_matches_month_end_encoded_periods() -> None:
    policy = SimpleNamespace(lookback_periods=2, frequency="monthly")
    periods = [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    best = {period: _raw_candidate(period=period, vintage=period) for period in periods}
    lagged = _lookup_lag_candidate(
        policy=policy,
        current_period=date(2026, 3, 31),
        best_by_period=best,
        sorted_periods=periods,
    )
    assert lagged is not None and lagged.observation_date == date(2026, 1, 31)


def test_feature_daily_row_preserves_pit_carry_forward_semantics() -> None:
    pit = PitDailyRow(
        as_of_date=date(2026, 2, 2),
        as_of_date_text="2026-02-02",
        registry_key="metric|source",
        ref_area="USA",
        source_name="source",
        source_series_id="SERIES",
        frequency="monthly",
        raw_value_selected=1.0,
        observation_period_selected="2026-01-01",
        observation_date_selected="2026-01-01",
        release_date_selected="2026-02-02",
        vintage_date_selected=None,
        effective_available_date_selected="2026-02-02",
        staleness_days=0,
        max_staleness_days=45,
        source_quality_weight=1.0,
        carry_forward_allowed=1,
        carry_forward_flag=0,
        coverage_flag=1,
    )
    policy = SimpleNamespace(
        metric_key="metric",
        feature_name="level",
        ref_area="USA",
        frequency="monthly",
        regime_block="growth",
        transform_code="level",
    )
    row = _daily_row_tuple(policy, pit, _feature_state("2026-01-15", 1.0))
    assert row[-3] == 0


def test_oecd_sibling_tasks_share_earliest_bundle_window() -> None:
    spec_a = _spec(
        registry_key="a",
        source_name="oecd_sdmx",
        source_dataset="DSD_STES@DF_CLI",
        source_params={"agency_id": "OECD.SDD.STES"},
    )
    spec_b = _spec(
        registry_key="b",
        source_name="oecd_sdmx",
        source_dataset="DSD_STES@DF_CLI",
        source_params={"agency_id": "OECD.SDD.STES"},
    )
    tasks = [
        FetchTask(spec_a, date(2026, 1, 1), date(2026, 8, 1), None, date(2026, 8, 1)),
        FetchTask(spec_b, date(2025, 1, 1), date(2026, 8, 1), None, date(2026, 8, 1)),
    ]
    normalized = _normalize_oecd_bundle_windows(tasks)
    assert {task.observation_start for task in normalized} == {date(2025, 1, 1)}


def test_ads_all_vintages_rejects_unrecognized_header_schema() -> None:
    frame = pd.DataFrame({"Date": ["2026:01:01"], "unexpected": [1.0]})
    with pytest.raises(RuntimeError, match="no recognized"):
        _wide_ads_to_long(frame)


def test_cfnai_requested_month_iterator_is_complete_and_ordered() -> None:
    assert _iter_month_keys((2025, 11), (2026, 2)) == [
        (2025, 11),
        (2025, 12),
        (2026, 1),
        (2026, 2),
    ]
    with pytest.raises(ValueError, match="after end"):
        _iter_month_keys((2026, 2), (2026, 1))


def test_composite_policy_rejects_never_materialized_feature_pair() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE macro_feature_daily (metric_key TEXT, feature_name TEXT)")
    conn.execute("INSERT INTO macro_feature_daily VALUES ('metric', 'level')")
    _validate_policy_feature_pairs(
        conn,
        [SimpleNamespace(metric_key="metric", feature_name="level")],
    )
    with pytest.raises(ValueError, match="never materialized"):
        _validate_policy_feature_pairs(
            conn,
            [SimpleNamespace(metric_key="metric", feature_name="yoy")],
        )


def test_disabled_tactical_overlay_is_neutral_even_under_strict_policy() -> None:
    config_path, cfg = load_macro_raw_config(MACRO_ROOT / "config_macro_raw.yaml")
    layer_cfg = _resolve_stock_overlay_config(cfg, config_path)
    disabled = replace(
        layer_cfg,
        sector_tactical_enabled=False,
        sector_tactical_missing_policy="strict",
    )
    dt = pd.Timestamp("2026-01-02")
    score = pd.DataFrame(
        {
            "as_of_date": [dt],
            "ticker": ["A"],
            "company": ["Alpha"],
            "sector_name": ["Technology"],
            "industry_aggregate_name": ["Tech Agg"],
            "industry_name": ["Tech Ind"],
            "rating": ["Buy"],
            "base_score": [80.0],
            "base_optimizer_eligible": [1],
            "earnings_blocked_7d": [0],
            "SnapshotSource": ["source"],
            "ScoreApproach": ["score"],
            "RunId": ["run"],
        }
    )
    industry = pd.DataFrame(
        {
            "as_of_date": [dt],
            "sector_name": ["Technology"],
            "industry_aggregate_name": ["Tech Agg"],
            "industry_name": ["Tech Ind"],
            "industry_macro_fit": [1.0],
            "industry_shock_prior_score": [0.0],
            "industry_macro_coverage_flag": [1],
        }
    )
    aggregate = pd.DataFrame(
        {
            "as_of_date": [dt],
            "sector_name": ["Technology"],
            "industry_aggregate_name": ["Tech Agg"],
            "industry_aggregate_macro_fit": [1.0],
            "aggregate_macro_coverage_flag": [1],
        }
    )
    sector = pd.DataFrame(
        {
            "as_of_date": [dt],
            "sector_name": ["Technology"],
            "sector_macro_fit": [1.0],
            "sector_shock_prior_score": [0.0],
            "sector_macro_coverage_flag": [1],
        }
    )
    fit, _, _, _ = _build_overlay_frames(
        score_panel=score,
        industry_macro=industry,
        aggregate_macro=aggregate,
        sector_macro=sector,
        tactical=pd.DataFrame(),
        validation_returns=pd.DataFrame(),
        layer_cfg=disabled,
    )
    assert fit.loc[0, "sector_tactical_lift"] == pytest.approx(disabled.sector_tactical_neutral_value)
    assert fit.loc[0, "sector_tactical_lift_z"] == pytest.approx(0.0)