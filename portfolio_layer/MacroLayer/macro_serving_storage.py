#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from typing import Any

from macro_raw_config import utc_now_iso


DDL = """
CREATE TABLE IF NOT EXISTS macro_serving_run (
    serving_run_id              TEXT PRIMARY KEY,
    build_step                  TEXT NOT NULL,
    raw_ingest_run_id           TEXT,
    as_of_start_date            TEXT,
    as_of_end_date              TEXT,
    metric_count                INTEGER NOT NULL DEFAULT 0,
    rows_written                INTEGER NOT NULL DEFAULT 0,
    status                      TEXT NOT NULL DEFAULT 'running',
    started_at_utc              TEXT NOT NULL,
    completed_at_utc            TEXT,
    notes                       TEXT
);

CREATE TABLE IF NOT EXISTS macro_calendar_daily (
    as_of_date                  TEXT PRIMARY KEY,
    weekday                     INTEGER NOT NULL,
    month_end_flag              INTEGER NOT NULL DEFAULT 0,
    quarter_end_flag            INTEGER NOT NULL DEFAULT 0,
    year_end_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_observation_daily_pit (
    as_of_date                  TEXT NOT NULL,
    metric_key                  TEXT NOT NULL,
    registry_key                TEXT,
    ref_area                    TEXT,
    source_name                 TEXT,
    source_series_id            TEXT,
    frequency                   TEXT,
    value_selected              REAL,
    observation_period_selected TEXT,
    observation_date_selected   TEXT,
    release_date_selected       TEXT,
    vintage_date_selected       TEXT,
    effective_available_date_selected TEXT,
    staleness_days              INTEGER,
    max_staleness_days          INTEGER,
    source_quality_weight       REAL,
    carry_forward_allowed       INTEGER NOT NULL DEFAULT 1,
    carry_forward_flag          INTEGER NOT NULL DEFAULT 0,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (as_of_date, metric_key)
);

CREATE TABLE IF NOT EXISTS macro_metric_latest (
    metric_key                  TEXT PRIMARY KEY,
    as_of_date                  TEXT NOT NULL,
    registry_key                TEXT,
    ref_area                    TEXT,
    source_name                 TEXT,
    source_series_id            TEXT,
    frequency                   TEXT,
    value_selected              REAL,
    observation_period_selected TEXT,
    observation_date_selected   TEXT,
    release_date_selected       TEXT,
    vintage_date_selected       TEXT,
    effective_available_date_selected TEXT,
    staleness_days              INTEGER,
    max_staleness_days          INTEGER,
    source_quality_weight       REAL,
    carry_forward_allowed       INTEGER NOT NULL DEFAULT 1,
    carry_forward_flag          INTEGER NOT NULL DEFAULT 0,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_country_coverage_daily (
    as_of_date                  TEXT NOT NULL,
    ticker                      TEXT NOT NULL,
    ref_area                    TEXT NOT NULL,
    country_class               TEXT,
    expected_metric_count       INTEGER NOT NULL DEFAULT 0,
    available_metric_count      INTEGER NOT NULL DEFAULT 0,
    required_metric_count       INTEGER NOT NULL DEFAULT 0,
    available_required_count    INTEGER NOT NULL DEFAULT 0,
    stale_metric_count          INTEGER NOT NULL DEFAULT 0,
    coverage_ratio              REAL,
    required_coverage_ratio     REAL,
    source_quality_score        REAL,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TABLE IF NOT EXISTS macro_feature_event (
    as_of_date                  TEXT NOT NULL,
    metric_key                  TEXT NOT NULL,
    feature_name                TEXT NOT NULL,
    ref_area                    TEXT,
    frequency                   TEXT,
    regime_block                TEXT,
    transform_code              TEXT,
    raw_value_selected          REAL,
    transformed_value           REAL,
    sign_adjusted_value         REAL,
    zscore_value                REAL,
    percentile_value            REAL,
    standardized_value          REAL,
    registry_key                TEXT,
    source_name                 TEXT,
    source_series_id            TEXT,
    observation_period_selected TEXT,
    observation_date_selected   TEXT,
    release_date_selected       TEXT,
    vintage_date_selected       TEXT,
    effective_available_date_selected TEXT,
    staleness_days              INTEGER,
    max_staleness_days          INTEGER,
    source_quality_weight       REAL,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (as_of_date, metric_key, feature_name)
);

CREATE TABLE IF NOT EXISTS macro_feature_daily (
    as_of_date                  TEXT NOT NULL,
    metric_key                  TEXT NOT NULL,
    feature_name                TEXT NOT NULL,
    feature_event_as_of_date    TEXT,
    ref_area                    TEXT,
    frequency                   TEXT,
    regime_block                TEXT,
    transform_code              TEXT,
    raw_value_selected          REAL,
    transformed_value           REAL,
    sign_adjusted_value         REAL,
    zscore_value                REAL,
    percentile_value            REAL,
    standardized_value          REAL,
    registry_key                TEXT,
    source_name                 TEXT,
    source_series_id            TEXT,
    observation_period_selected TEXT,
    observation_date_selected   TEXT,
    release_date_selected       TEXT,
    vintage_date_selected       TEXT,
    effective_available_date_selected TEXT,
    staleness_days              INTEGER,
    max_staleness_days          INTEGER,
    source_quality_weight       REAL,
    carry_forward_allowed       INTEGER NOT NULL DEFAULT 1,
    carry_forward_flag          INTEGER NOT NULL DEFAULT 0,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (as_of_date, metric_key, feature_name)
);

CREATE TABLE IF NOT EXISTS macro_composite_daily (
    as_of_date                  TEXT NOT NULL,
    composite_key               TEXT NOT NULL,
    composite_value_raw         REAL,
    composite_value_smoothed    REAL,
    expected_component_count    INTEGER NOT NULL DEFAULT 0,
    available_component_count   INTEGER NOT NULL DEFAULT 0,
    required_component_count    INTEGER NOT NULL DEFAULT 0,
    available_required_count    INTEGER NOT NULL DEFAULT 0,
    coverage_ratio              REAL,
    required_coverage_ratio     REAL,
    effective_weight_sum        REAL,
    smoothing_window_days       INTEGER NOT NULL DEFAULT 1,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (as_of_date, composite_key)
);

CREATE TABLE IF NOT EXISTS macro_composite_component_daily (
    as_of_date                  TEXT NOT NULL,
    composite_key               TEXT NOT NULL,
    metric_key                  TEXT NOT NULL,
    feature_name                TEXT NOT NULL,
    ref_area                    TEXT,
    regime_block                TEXT,
    feature_event_as_of_date    TEXT,
    standardized_value          REAL,
    base_weight                 REAL,
    effective_weight            REAL,
    normalized_weight           REAL,
    contribution_value          REAL,
    source_quality_weight       REAL,
    carry_forward_flag          INTEGER NOT NULL DEFAULT 0,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    required_flag               INTEGER NOT NULL DEFAULT 0,
    included_flag               INTEGER NOT NULL DEFAULT 0,
    exclusion_reason            TEXT,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (as_of_date, composite_key, metric_key, feature_name)
);

CREATE TABLE IF NOT EXISTS macro_probability_calibration (
    calibration_as_of_date      TEXT NOT NULL,
    probability_key             TEXT NOT NULL,
    source_composite_key        TEXT NOT NULL,
    target_composite_key        TEXT NOT NULL,
    target_start_month_offset   INTEGER NOT NULL DEFAULT 0,
    target_window_months        INTEGER NOT NULL DEFAULT 1,
    label_threshold             REAL NOT NULL DEFAULT 0.0,
    training_sample_count       INTEGER NOT NULL DEFAULT 0,
    positive_sample_count       INTEGER NOT NULL DEFAULT 0,
    negative_sample_count       INTEGER NOT NULL DEFAULT 0,
    positive_rate               REAL,
    predictor_mean              REAL,
    predictor_std               REAL,
    intercept_value             REAL,
    slope_value                 REAL,
    slope_clipped_flag          INTEGER NOT NULL DEFAULT 0,
    calibration_ready_flag      INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (calibration_as_of_date, probability_key)
);

CREATE TABLE IF NOT EXISTS macro_probability_diagnostics (
    calibration_as_of_date      TEXT NOT NULL,
    probability_key             TEXT NOT NULL,
    source_composite_key        TEXT NOT NULL,
    train_brier_score           REAL,
    train_log_loss              REAL,
    train_auc                   REAL,
    train_probability_p05       REAL,
    train_probability_p50       REAL,
    train_probability_p95       REAL,
    saturation_low_share        REAL,
    saturation_high_share       REAL,
    coefficient_delta_intercept REAL,
    coefficient_delta_slope     REAL,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (calibration_as_of_date, probability_key)
);

CREATE TABLE IF NOT EXISTS macro_probabilities_daily (
    as_of_date                  TEXT NOT NULL,
    probability_key             TEXT NOT NULL,
    source_composite_key        TEXT NOT NULL,
    source_composite_value      REAL,
    probability_value           REAL,
    calibration_as_of_date      TEXT,
    target_composite_key        TEXT NOT NULL,
    target_start_month_offset   INTEGER NOT NULL DEFAULT 0,
    target_window_months        INTEGER NOT NULL DEFAULT 1,
    label_threshold             REAL NOT NULL DEFAULT 0.0,
    training_sample_count       INTEGER NOT NULL DEFAULT 0,
    positive_rate               REAL,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (as_of_date, probability_key)
);

CREATE TABLE IF NOT EXISTS macro_probability_v2_target (
    model_version               TEXT NOT NULL,
    probability_key             TEXT NOT NULL,
    predictor_as_of_date        TEXT NOT NULL,
    target_period_start         TEXT NOT NULL,
    target_period_end           TEXT NOT NULL,
    target_value                REAL,
    label_value                 INTEGER,
    label_available_date        TEXT,
    label_source                TEXT NOT NULL,
    label_threshold             REAL NOT NULL,
    predictor_complete_flag     INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (model_version, probability_key, predictor_as_of_date)
);

CREATE TABLE IF NOT EXISTS macro_probability_v2_model (
    model_version               TEXT NOT NULL,
    calibration_as_of_date      TEXT NOT NULL,
    probability_key             TEXT NOT NULL,
    target_name                 TEXT NOT NULL,
    target_horizon              TEXT NOT NULL,
    predictor_names_json        TEXT NOT NULL,
    mandatory_predictors_json   TEXT NOT NULL,
    predictor_mean_json         TEXT,
    predictor_std_json          TEXT,
    coefficients_json           TEXT,
    intercept_value             REAL,
    ridge_penalty               REAL NOT NULL,
    training_sample_count       INTEGER NOT NULL DEFAULT 0,
    positive_sample_count       INTEGER NOT NULL DEFAULT 0,
    negative_sample_count       INTEGER NOT NULL DEFAULT 0,
    positive_rate               REAL,
    max_label_available_date    TEXT,
    calibration_ready_flag      INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (model_version, calibration_as_of_date, probability_key)
);

CREATE TABLE IF NOT EXISTS macro_probability_v2_daily (
    model_version               TEXT NOT NULL,
    as_of_date                  TEXT NOT NULL,
    probability_key             TEXT NOT NULL,
    probability_value           REAL,
    calibration_as_of_date      TEXT,
    target_period_start         TEXT,
    target_period_end           TEXT,
    training_sample_count       INTEGER NOT NULL DEFAULT 0,
    positive_rate               REAL,
    predictor_coverage_ratio    REAL,
    coverage_flag               INTEGER NOT NULL DEFAULT 0,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (model_version, as_of_date, probability_key)
);

CREATE TABLE IF NOT EXISTS macro_probability_v2_diagnostics (
    model_version               TEXT NOT NULL,
    diagnostic_as_of_date       TEXT NOT NULL,
    probability_key             TEXT NOT NULL,
    oos_sample_count            INTEGER NOT NULL DEFAULT 0,
    positive_sample_count       INTEGER NOT NULL DEFAULT 0,
    negative_sample_count       INTEGER NOT NULL DEFAULT 0,
    oos_brier_score             REAL,
    climatology_brier_score     REAL,
    brier_skill_score           REAL,
    oos_log_loss                REAL,
    oos_auc                     REAL,
    calibration_intercept       REAL,
    calibration_slope           REAL,
    evidence_status             TEXT NOT NULL,
    evidence_reason             TEXT NOT NULL,
    updated_at_utc              TEXT NOT NULL,
    PRIMARY KEY (model_version, diagnostic_as_of_date, probability_key)
);

CREATE TABLE IF NOT EXISTS macro_regime_v2_daily (
    model_version                           TEXT NOT NULL,
    as_of_date                              TEXT NOT NULL,
    p_g_now                                 REAL,
    p_g_lead                                REAL,
    p_pi_now                                REAL,
    p_pi_lead                               REAL,
    p_current_expansion_disinflation        REAL,
    p_current_heating_up                    REAL,
    p_current_slow_growth                   REAL,
    p_current_stagflation                   REAL,
    p_next_expansion_disinflation           REAL,
    p_next_heating_up                       REAL,
    p_next_slow_growth                      REAL,
    p_next_stagflation                      REAL,
    current_regime                          TEXT,
    next_regime                             TEXT,
    current_regime_probability              REAL,
    next_regime_probability                 REAL,
    current_regime_confidence               REAL,
    next_regime_confidence                  REAL,
    energy_shock_score                      REAL,
    energy_shock_flag                       INTEGER NOT NULL DEFAULT 0,
    shadow_only_flag                        INTEGER NOT NULL DEFAULT 1,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (model_version, as_of_date)
);

CREATE TABLE IF NOT EXISTS macro_regime_v2_smoothed_daily (
    model_version                           TEXT NOT NULL,
    as_of_date                              TEXT NOT NULL,
    p_smoothed_current_expansion_disinflation REAL,
    p_smoothed_current_heating_up           REAL,
    p_smoothed_current_slow_growth          REAL,
    p_smoothed_current_stagflation          REAL,
    p_smoothed_next_3m_expansion_disinflation REAL,
    p_smoothed_next_3m_heating_up           REAL,
    p_smoothed_next_3m_slow_growth          REAL,
    p_smoothed_next_3m_stagflation          REAL,
    raw_current_regime                      TEXT,
    smoothed_current_regime                 TEXT,
    raw_next_regime                         TEXT,
    smoothed_next_regime                    TEXT,
    raw_current_regime_probability          REAL,
    smoothed_current_regime_probability     REAL,
    raw_next_regime_probability             REAL,
    smoothed_next_regime_probability        REAL,
    raw_regime_confidence                   REAL,
    smoothed_regime_confidence              REAL,
    smoothed_transition_bias                TEXT,
    smoothed_transition_bias_strength       REAL,
    raw_to_smoothed_shift_l1                REAL,
    next_raw_to_smoothed_shift_l1           REAL,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (model_version, as_of_date)
);

CREATE TABLE IF NOT EXISTS macro_transition_v2_matrix (
    model_version                           TEXT NOT NULL,
    as_of_date                              TEXT NOT NULL,
    from_regime                             TEXT NOT NULL,
    to_regime                               TEXT NOT NULL,
    prior_transition_probability            REAL,
    empirical_transition_probability        REAL,
    transition_probability                  REAL,
    empirical_transition_count              INTEGER NOT NULL DEFAULT 0,
    total_from_count                        INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (model_version, as_of_date, from_regime, to_regime)
);

CREATE TABLE IF NOT EXISTS macro_transition_v2_diagnostics (
    model_version                           TEXT NOT NULL,
    as_of_date                              TEXT NOT NULL,
    transition_count_before                 INTEGER NOT NULL DEFAULT 0,
    raw_current_regime                      TEXT,
    predicted_current_regime                TEXT,
    smoothed_current_regime                 TEXT,
    raw_next_regime                         TEXT,
    smoothed_next_regime                    TEXT,
    raw_current_regime_probability          REAL,
    predicted_current_regime_probability    REAL,
    smoothed_current_regime_probability     REAL,
    raw_next_regime_probability             REAL,
    smoothed_next_regime_probability        REAL,
    raw_regime_confidence                   REAL,
    smoothed_regime_confidence              REAL,
    raw_to_smoothed_shift_l1                REAL,
    next_raw_to_smoothed_shift_l1           REAL,
    raw_flip_flag                           INTEGER NOT NULL DEFAULT 0,
    smoothed_flip_flag                      INTEGER NOT NULL DEFAULT 0,
    raw_smoothed_agreement_flag             INTEGER NOT NULL DEFAULT 0,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (model_version, as_of_date)
);

CREATE TABLE IF NOT EXISTS macro_regime_v2_decision_daily (
    model_version                           TEXT NOT NULL,
    as_of_date                              TEXT NOT NULL,
    decision_date_flag                      INTEGER NOT NULL DEFAULT 0,
    smoothed_current_regime                 TEXT,
    smoothed_next_regime                    TEXT,
    active_current_regime                   TEXT,
    active_next_regime                      TEXT,
    current_top_probability                 REAL,
    next_top_probability                    REAL,
    current_confidence                      REAL,
    next_confidence                         REAL,
    current_switch_margin                   REAL,
    next_switch_margin                      REAL,
    current_switch_flag                     INTEGER NOT NULL DEFAULT 0,
    next_switch_flag                        INTEGER NOT NULL DEFAULT 0,
    regime_switch_flag                      INTEGER NOT NULL DEFAULT 0,
    current_pending_regime                  TEXT,
    next_pending_regime                     TEXT,
    current_pending_count                   INTEGER NOT NULL DEFAULT 0,
    next_pending_count                      INTEGER NOT NULL DEFAULT 0,
    regime_switch_pending_flag              INTEGER NOT NULL DEFAULT 0,
    regime_override_reason                  TEXT,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (model_version, as_of_date)
);

CREATE TABLE IF NOT EXISTS macro_regime_v2_promotion_evidence (
    model_version                           TEXT NOT NULL,
    evidence_as_of_date                     TEXT NOT NULL,
    probability_key                         TEXT NOT NULL,
    common_oos_sample_count                 INTEGER NOT NULL DEFAULT 0,
    positive_sample_count                   INTEGER NOT NULL DEFAULT 0,
    negative_sample_count                   INTEGER NOT NULL DEFAULT 0,
    v1_brier_score                          REAL,
    v2_brier_score                          REAL,
    v2_brier_skill_score                    REAL,
    brier_improvement_vs_v1                 REAL,
    v1_auc                                  REAL,
    v2_auc                                  REAL,
    v2_calibration_intercept                REAL,
    v2_calibration_slope                    REAL,
    cell_status                             TEXT NOT NULL,
    cell_reason                             TEXT NOT NULL,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (model_version, evidence_as_of_date, probability_key)
);

CREATE TABLE IF NOT EXISTS macro_regime_v2_promotion_summary (
    model_version                           TEXT NOT NULL,
    evidence_as_of_date                     TEXT NOT NULL,
    acceptance                              TEXT NOT NULL,
    validated_cell_count                    INTEGER NOT NULL DEFAULT 0,
    required_cell_count                     INTEGER NOT NULL DEFAULT 0,
    common_decision_day_count               INTEGER NOT NULL DEFAULT 0,
    regime_disagreement_fraction            REAL,
    v1_switch_count                         INTEGER NOT NULL DEFAULT 0,
    v2_switch_count                         INTEGER NOT NULL DEFAULT 0,
    current_candidate_confident_flag        INTEGER NOT NULL DEFAULT 0,
    artifact_manifest_path                  TEXT,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (model_version, evidence_as_of_date)
);

CREATE TABLE IF NOT EXISTS macro_regime_raw_daily (
    as_of_date                              TEXT PRIMARY KEY,
    p_g_now                                REAL,
    p_g_lead                               REAL,
    p_pi_now                               REAL,
    p_pi_lead                              REAL,
    p_current_expansion_disinflation       REAL,
    p_current_heating_up                   REAL,
    p_current_slow_growth                  REAL,
    p_current_stagflation                  REAL,
    p_next_3m_expansion_disinflation       REAL,
    p_next_3m_heating_up                   REAL,
    p_next_3m_slow_growth                  REAL,
    p_next_3m_stagflation                  REAL,
    current_regime                         TEXT,
    next_regime                            TEXT,
    current_regime_probability             REAL,
    next_regime_probability                REAL,
    regime_confidence                      REAL,
    transition_bias                        TEXT,
    transition_bias_strength               REAL,
    coverage_flag                          INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_regime_smoothed_daily (
    as_of_date                              TEXT PRIMARY KEY,
    p_smoothed_current_expansion_disinflation REAL,
    p_smoothed_current_heating_up           REAL,
    p_smoothed_current_slow_growth          REAL,
    p_smoothed_current_stagflation          REAL,
    p_smoothed_next_3m_expansion_disinflation REAL,
    p_smoothed_next_3m_heating_up           REAL,
    p_smoothed_next_3m_slow_growth          REAL,
    p_smoothed_next_3m_stagflation          REAL,
    raw_current_regime                      TEXT,
    smoothed_current_regime                 TEXT,
    raw_next_regime                         TEXT,
    smoothed_next_regime                    TEXT,
    raw_current_regime_probability          REAL,
    smoothed_current_regime_probability     REAL,
    raw_next_regime_probability             REAL,
    smoothed_next_regime_probability        REAL,
    raw_regime_confidence                   REAL,
    smoothed_regime_confidence              REAL,
    smoothed_transition_bias                TEXT,
    smoothed_transition_bias_strength       REAL,
    raw_to_smoothed_shift_l1                REAL,
    next_raw_to_smoothed_shift_l1           REAL,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_transition_matrix (
    as_of_date                              TEXT NOT NULL,
    from_regime                             TEXT NOT NULL,
    to_regime                               TEXT NOT NULL,
    prior_transition_probability            REAL,
    empirical_transition_probability        REAL,
    transition_probability                  REAL,
    empirical_transition_count              INTEGER NOT NULL DEFAULT 0,
    total_from_count                        INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, from_regime, to_regime)
);

CREATE TABLE IF NOT EXISTS macro_transition_diagnostics (
    as_of_date                              TEXT PRIMARY KEY,
    transition_count_before                 INTEGER NOT NULL DEFAULT 0,
    raw_current_regime                      TEXT,
    predicted_current_regime               TEXT,
    smoothed_current_regime                 TEXT,
    raw_next_regime                         TEXT,
    smoothed_next_regime                    TEXT,
    raw_current_regime_probability          REAL,
    predicted_current_regime_probability    REAL,
    smoothed_current_regime_probability     REAL,
    raw_next_regime_probability             REAL,
    smoothed_next_regime_probability        REAL,
    raw_regime_confidence                   REAL,
    smoothed_regime_confidence              REAL,
    raw_to_smoothed_shift_l1                REAL,
    next_raw_to_smoothed_shift_l1           REAL,
    raw_flip_flag                           INTEGER NOT NULL DEFAULT 0,
    smoothed_flip_flag                      INTEGER NOT NULL DEFAULT 0,
    raw_smoothed_agreement_flag             INTEGER NOT NULL DEFAULT 0,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_regime_decision_daily (
    as_of_date                              TEXT PRIMARY KEY,
    decision_date_flag                      INTEGER NOT NULL DEFAULT 0,
    smoothed_current_regime                 TEXT,
    smoothed_next_regime                    TEXT,
    active_current_regime                   TEXT,
    active_next_regime                      TEXT,
    current_top_probability                 REAL,
    next_top_probability                    REAL,
    current_confidence                      REAL,
    next_confidence                         REAL,
    current_switch_margin                   REAL,
    next_switch_margin                      REAL,
    current_switch_flag                     INTEGER NOT NULL DEFAULT 0,
    next_switch_flag                        INTEGER NOT NULL DEFAULT 0,
    regime_switch_flag                      INTEGER NOT NULL DEFAULT 0,
    current_pending_regime                  TEXT,
    next_pending_regime                     TEXT,
    current_pending_count                   INTEGER NOT NULL DEFAULT 0,
    next_pending_count                      INTEGER NOT NULL DEFAULT 0,
    regime_switch_pending_flag              INTEGER NOT NULL DEFAULT 0,
    regime_override_reason                  TEXT,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sector_macro_fit_daily (
    as_of_date                              TEXT NOT NULL,
    sector_name                             TEXT NOT NULL,
    active_current_regime                   TEXT,
    active_next_regime                      TEXT,
    regime_prior_score                      REAL,
    shock_prior_score                       REAL,
    prior_score                             REAL,
    empirical_score                         REAL,
    empirical_weight                        REAL,
    level_fit_score                         REAL,
    final_score                             REAL,
    basket_return                           REAL,
    universe_return                         REAL,
    excess_return                           REAL,
    member_count                            INTEGER NOT NULL DEFAULT 0,
    effective_history_weeks                 REAL,
    oil_shock_value                         REAL,
    commodity_shock_value                   REAL,
    dollar_shock_value                      REAL,
    real_yield_shock_value                  REAL,
    credit_shock_value                      REAL,
    shock_composite_value                   REAL,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, sector_name)
);

CREATE TABLE IF NOT EXISTS industry_aggregate_macro_fit_daily (
    as_of_date                              TEXT NOT NULL,
    sector_name                             TEXT NOT NULL,
    industry_aggregate_name                 TEXT NOT NULL,
    active_current_regime                   TEXT,
    active_next_regime                      TEXT,
    regime_prior_score                      REAL,
    shock_prior_score                       REAL,
    prior_score                             REAL,
    empirical_score                         REAL,
    empirical_weight                        REAL,
    level_fit_score                         REAL,
    final_score                             REAL,
    basket_return                           REAL,
    universe_return                         REAL,
    excess_return                           REAL,
    member_count                            INTEGER NOT NULL DEFAULT 0,
    effective_history_weeks                 REAL,
    oil_shock_value                         REAL,
    commodity_shock_value                   REAL,
    dollar_shock_value                      REAL,
    real_yield_shock_value                  REAL,
    credit_shock_value                      REAL,
    shock_composite_value                   REAL,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, sector_name, industry_aggregate_name)
);

CREATE TABLE IF NOT EXISTS industry_macro_fit_daily (
    as_of_date                              TEXT NOT NULL,
    sector_name                             TEXT NOT NULL,
    industry_aggregate_name                 TEXT NOT NULL,
    industry_name                           TEXT NOT NULL,
    active_current_regime                   TEXT,
    active_next_regime                      TEXT,
    regime_prior_score                      REAL,
    shock_prior_score                       REAL,
    prior_score                             REAL,
    empirical_score                         REAL,
    empirical_weight                        REAL,
    level_fit_score                         REAL,
    industry_aggregate_component_score      REAL,
    sector_component_score                  REAL,
    final_score                             REAL,
    basket_return                           REAL,
    universe_return                         REAL,
    excess_return                           REAL,
    member_count                            INTEGER NOT NULL DEFAULT 0,
    effective_history_weeks                 REAL,
    oil_shock_value                         REAL,
    commodity_shock_value                   REAL,
    dollar_shock_value                      REAL,
    real_yield_shock_value                  REAL,
    credit_shock_value                      REAL,
    shock_composite_value                   REAL,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, sector_name, industry_aggregate_name, industry_name)
);

CREATE TABLE IF NOT EXISTS country_macro_fit_daily (
    as_of_date                              TEXT NOT NULL,
    ticker                                  TEXT NOT NULL,
    ref_area                                TEXT NOT NULL,
    country_name                            TEXT,
    country_class                           TEXT,
    region                                  TEXT,
    market_class                            TEXT,
    active_current_regime                   TEXT,
    active_next_regime                      TEXT,
    global_regime_fit                       REAL,
    local_macro_fit                         REAL,
    external_shock_fit                      REAL,
    growth_now_score                        REAL,
    growth_lead_score                       REAL,
    inflation_score                         REAL,
    local_external_score                    REAL,
    global_shock_score                      REAL,
    country_macro_fit                       REAL,
    confidence_adjusted_fit                 REAL,
    feature_count                           INTEGER NOT NULL DEFAULT 0,
    available_feature_count                 INTEGER NOT NULL DEFAULT 0,
    local_feature_coverage_ratio            REAL,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TABLE IF NOT EXISTS country_confidence_daily (
    as_of_date                              TEXT NOT NULL,
    ticker                                  TEXT NOT NULL,
    ref_area                                TEXT NOT NULL,
    country_class                           TEXT,
    expected_metric_count                   INTEGER NOT NULL DEFAULT 0,
    available_metric_count                  INTEGER NOT NULL DEFAULT 0,
    required_metric_count                   INTEGER NOT NULL DEFAULT 0,
    available_required_count                INTEGER NOT NULL DEFAULT 0,
    stale_metric_count                      INTEGER NOT NULL DEFAULT 0,
    coverage_ratio                          REAL,
    required_coverage_ratio                 REAL,
    source_quality_score                    REAL,
    class_confidence                        REAL,
    coverage_confidence                     REAL,
    required_confidence                     REAL,
    freshness_confidence                    REAL,
    source_confidence                       REAL,
    local_feature_confidence                REAL,
    fallback_penalty                        REAL,
    country_confidence                      REAL,
    confidence_reason                       TEXT,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TABLE IF NOT EXISTS country_macro_rank_daily (
    as_of_date                              TEXT NOT NULL,
    ticker                                  TEXT NOT NULL,
    ref_area                                TEXT NOT NULL,
    country_class                           TEXT,
    country_macro_fit                       REAL,
    country_confidence                      REAL,
    confidence_adjusted_fit                 REAL,
    country_rank                            INTEGER,
    country_percentile                      REAL,
    eligible_flag                           INTEGER NOT NULL DEFAULT 0,
    rank_reason                             TEXT,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TABLE IF NOT EXISTS stock_macro_fit_daily (
    as_of_date                              TEXT NOT NULL,
    ticker                                  TEXT NOT NULL,
    company                                 TEXT,
    sector_name                             TEXT NOT NULL,
    industry_aggregate_name                 TEXT NOT NULL,
    industry_name                           TEXT NOT NULL,
    rating                                  TEXT,
    base_score                              REAL,
    base_stock_z                            REAL,
    industry_macro_fit                      REAL,
    industry_aggregate_macro_fit            REAL,
    sector_macro_fit                        REAL,
    sector_tactical_lift                    REAL,
    sector_tactical_lift_z                  REAL,
    shock_fit                               REAL,
    macro_stock_fit_raw                     REAL,
    macro_stock_fit_z                       REAL,
    macro_favored_flag                      INTEGER NOT NULL DEFAULT 0,
    macro_adverse_flag                      INTEGER NOT NULL DEFAULT 0,
    base_optimizer_eligible                 INTEGER NOT NULL DEFAULT 1,
    earnings_blocked_7d                     INTEGER NOT NULL DEFAULT 0,
    snapshot_source                         TEXT,
    score_approach                          TEXT,
    run_id                                  TEXT,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TABLE IF NOT EXISTS stock_selection_score_daily (
    as_of_date                              TEXT NOT NULL,
    ticker                                  TEXT NOT NULL,
    sector_name                             TEXT NOT NULL,
    industry_aggregate_name                 TEXT NOT NULL,
    industry_name                           TEXT NOT NULL,
    base_stock_z                            REAL,
    macro_stock_fit_z                       REAL,
    sector_tactical_lift_z                  REAL,
    selection_score                         REAL,
    selection_rank                          INTEGER,
    selection_percentile                    REAL,
    macro_favored_flag                      INTEGER NOT NULL DEFAULT 0,
    macro_adverse_flag                      INTEGER NOT NULL DEFAULT 0,
    base_optimizer_eligible                 INTEGER NOT NULL DEFAULT 1,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TABLE IF NOT EXISTS stock_weight_score_daily (
    as_of_date                              TEXT NOT NULL,
    ticker                                  TEXT NOT NULL,
    sector_name                             TEXT NOT NULL,
    industry_aggregate_name                 TEXT NOT NULL,
    industry_name                           TEXT NOT NULL,
    base_stock_z                            REAL,
    macro_stock_fit_z                       REAL,
    sector_tactical_lift_z                  REAL,
    weight_score                            REAL,
    weight_rank                             INTEGER,
    weight_percentile                       REAL,
    macro_favored_flag                      INTEGER NOT NULL DEFAULT 0,
    macro_adverse_flag                      INTEGER NOT NULL DEFAULT 0,
    base_optimizer_eligible                 INTEGER NOT NULL DEFAULT 1,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TABLE IF NOT EXISTS portfolio_inputs_daily (
    as_of_date                              TEXT NOT NULL,
    ticker                                  TEXT NOT NULL,
    asset_type                              TEXT NOT NULL,
    sleeve                                  TEXT NOT NULL,
    company                                 TEXT,
    market_name                             TEXT,
    sector_name                             TEXT,
    industry_aggregate_name                 TEXT,
    industry_name                           TEXT,
    rating                                  TEXT,
    region                                  TEXT,
    country_class                           TEXT,
    base_final_score                        REAL,
    final_score                             REAL,
    selection_score                         REAL,
    weight_score                            REAL,
    score_pct                               REAL,
    state                                   TEXT,
    entry_score                             REAL,
    expected_return_score                   REAL,
    base_optimizer_eligible                 INTEGER NOT NULL DEFAULT 1,
    earnings_blocked_7d                     INTEGER NOT NULL DEFAULT 0,
    macro_overlay_enabled                   INTEGER NOT NULL DEFAULT 1,
    stock_macro_coverage_flag               INTEGER NOT NULL DEFAULT 0,
    country_macro_coverage_flag             INTEGER NOT NULL DEFAULT 0,
    macro_stock_fit_z                       REAL,
    industry_macro_fit                      REAL,
    industry_aggregate_macro_fit            REAL,
    sector_macro_fit                        REAL,
    sector_tactical_lift_z                  REAL,
    shock_fit                               REAL,
    tactical_z                              REAL,
    country_macro_fit_z                     REAL,
    country_confidence                      REAL,
    foreign_fused_alpha                     REAL,
    agreement_z                             REAL,
    optimizer_score_source                  TEXT,
    source_snapshot                         TEXT,
    run_id                                  TEXT,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker, asset_type)
);

CREATE TABLE IF NOT EXISTS portfolio_allocation_summary (
    as_of_date                              TEXT NOT NULL,
    stock_count                             INTEGER NOT NULL DEFAULT 0,
    stock_eligible_count                    INTEGER NOT NULL DEFAULT 0,
    foreign_count                           INTEGER NOT NULL DEFAULT 0,
    foreign_eligible_count                  INTEGER NOT NULL DEFAULT 0,
    foreign_positive_count                  INTEGER NOT NULL DEFAULT 0,
    macro_overlay_enabled                   INTEGER NOT NULL DEFAULT 1,
    avg_stock_selection_score               REAL,
    avg_stock_weight_score                  REAL,
    avg_foreign_fused_alpha                 REAL,
    max_foreign_fused_alpha                 REAL,
    stock_output_csv                        TEXT,
    foreign_output_csv                      TEXT,
    combined_output_csv                     TEXT,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date)
);

CREATE TABLE IF NOT EXISTS stock_industry_target_daily (
    as_of_date                              TEXT NOT NULL,
    sector_name                             TEXT NOT NULL,
    industry_aggregate_name                 TEXT NOT NULL,
    industry_name                           TEXT NOT NULL,
    member_count                            INTEGER NOT NULL DEFAULT 0,
    eligible_member_count                   INTEGER NOT NULL DEFAULT 0,
    top_member_count                        INTEGER NOT NULL DEFAULT 0,
    industry_macro_fit                      REAL,
    opportunity_score                       REAL,
    macro_component                         REAL,
    opportunity_component                   REAL,
    raw_target_score                        REAL,
    target_weight                           REAL,
    min_weight                              REAL,
    max_weight                              REAL,
    target_rank                             INTEGER,
    target_percentile                       REAL,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, sector_name, industry_aggregate_name, industry_name)
);

CREATE TABLE IF NOT EXISTS stock_sector_target_daily (
    as_of_date                              TEXT NOT NULL,
    sector_name                             TEXT NOT NULL,
    industry_count                          INTEGER NOT NULL DEFAULT 0,
    targetable_industry_count               INTEGER NOT NULL DEFAULT 0,
    eligible_member_count                   INTEGER NOT NULL DEFAULT 0,
    avg_industry_macro_fit                  REAL,
    avg_opportunity_score                   REAL,
    target_weight                           REAL,
    min_weight                              REAL,
    max_weight                              REAL,
    target_rank                             INTEGER,
    target_percentile                       REAL,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, sector_name)
);

CREATE TABLE IF NOT EXISTS stock_sleeve_target_summary (
    as_of_date                              TEXT NOT NULL,
    industry_count                          INTEGER NOT NULL DEFAULT 0,
    targetable_industry_count               INTEGER NOT NULL DEFAULT 0,
    sector_count                            INTEGER NOT NULL DEFAULT 0,
    eligible_stock_count                    INTEGER NOT NULL DEFAULT 0,
    target_weight_sum                       REAL,
    max_industry_target_weight              REAL,
    max_sector_target_weight                REAL,
    effective_industry_count                REAL,
    industry_output_csv                     TEXT,
    sector_output_csv                       TEXT,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date)
);

CREATE TABLE IF NOT EXISTS foreign_sleeve_budget_daily (
    as_of_date                              TEXT NOT NULL,
    active_flag                             INTEGER NOT NULL DEFAULT 0,
    foreign_budget                          REAL,
    min_budget                              REAL,
    max_budget                              REAL,
    activation_score                        REAL,
    activation_score_threshold              REAL,
    full_budget_score_threshold             REAL,
    foreign_candidate_count                 INTEGER NOT NULL DEFAULT 0,
    eligible_candidate_count                INTEGER NOT NULL DEFAULT 0,
    selected_candidate_count                INTEGER NOT NULL DEFAULT 0,
    positive_candidate_count                INTEGER NOT NULL DEFAULT 0,
    avg_selected_confidence                 REAL,
    max_foreign_fused_alpha                 REAL,
    activation_reason                       TEXT,
    output_csv                              TEXT,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date)
);

CREATE TABLE IF NOT EXISTS foreign_sleeve_candidate_daily (
    as_of_date                              TEXT NOT NULL,
    ticker                                  TEXT NOT NULL,
    market_name                             TEXT,
    region                                  TEXT,
    country_class                           TEXT,
    source_state                            TEXT,
    country_confidence                      REAL,
    tactical_z                              REAL,
    country_macro_fit_z                     REAL,
    foreign_fused_alpha                     REAL,
    candidate_score                         REAL,
    sleeve_weight                           REAL,
    portfolio_weight_at_budget              REAL,
    candidate_rank                          INTEGER,
    candidate_percentile                    REAL,
    eligible_flag                           INTEGER NOT NULL DEFAULT 0,
    selected_flag                           INTEGER NOT NULL DEFAULT 0,
    active_flag                             INTEGER NOT NULL DEFAULT 0,
    rejection_reason                        TEXT,
    coverage_flag                           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc                          TEXT NOT NULL,
    PRIMARY KEY (as_of_date, ticker)
);

CREATE TRIGGER IF NOT EXISTS trg_industry_aggregate_macro_fit_daily_nn_insert
BEFORE INSERT ON industry_aggregate_macro_fit_daily
WHEN NEW.sector_name IS NULL OR NEW.industry_aggregate_name IS NULL
BEGIN
    SELECT RAISE(ABORT, 'industry_aggregate_macro_fit_daily natural-key columns cannot be NULL');
END;

CREATE TRIGGER IF NOT EXISTS trg_industry_aggregate_macro_fit_daily_nn_update
BEFORE UPDATE ON industry_aggregate_macro_fit_daily
WHEN NEW.sector_name IS NULL OR NEW.industry_aggregate_name IS NULL
BEGIN
    SELECT RAISE(ABORT, 'industry_aggregate_macro_fit_daily natural-key columns cannot be NULL');
END;

CREATE TRIGGER IF NOT EXISTS trg_industry_macro_fit_daily_nn_insert
BEFORE INSERT ON industry_macro_fit_daily
WHEN NEW.sector_name IS NULL OR NEW.industry_aggregate_name IS NULL OR NEW.industry_name IS NULL
BEGIN
    SELECT RAISE(ABORT, 'industry_macro_fit_daily natural-key columns cannot be NULL');
END;

CREATE TRIGGER IF NOT EXISTS trg_industry_macro_fit_daily_nn_update
BEFORE UPDATE ON industry_macro_fit_daily
WHEN NEW.sector_name IS NULL OR NEW.industry_aggregate_name IS NULL OR NEW.industry_name IS NULL
BEGIN
    SELECT RAISE(ABORT, 'industry_macro_fit_daily natural-key columns cannot be NULL');
END;

CREATE INDEX IF NOT EXISTS idx_macro_pit_metric_date
    ON macro_observation_daily_pit(metric_key, as_of_date);

CREATE INDEX IF NOT EXISTS idx_macro_pit_ref_area_date
    ON macro_observation_daily_pit(ref_area, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_pit_date_coverage
    ON macro_observation_daily_pit(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_country_cov_date
    ON macro_country_coverage_daily(as_of_date, ref_area, country_class);

CREATE INDEX IF NOT EXISTS idx_macro_feature_event_metric_date
    ON macro_feature_event(metric_key, as_of_date);

CREATE INDEX IF NOT EXISTS idx_macro_feature_daily_metric_date
    ON macro_feature_daily(metric_key, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_feature_daily_date
    ON macro_feature_daily(as_of_date, feature_name, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_composite_daily_key_date
    ON macro_composite_daily(composite_key, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_composite_daily_date
    ON macro_composite_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_composite_component_key_date
    ON macro_composite_component_daily(composite_key, as_of_date, included_flag);

CREATE INDEX IF NOT EXISTS idx_macro_composite_component_metric_date
    ON macro_composite_component_daily(metric_key, as_of_date, composite_key);

CREATE INDEX IF NOT EXISTS idx_macro_probability_calibration_key_date
    ON macro_probability_calibration(probability_key, calibration_as_of_date, calibration_ready_flag);

CREATE INDEX IF NOT EXISTS idx_macro_probability_diagnostics_key_date
    ON macro_probability_diagnostics(probability_key, calibration_as_of_date);

CREATE INDEX IF NOT EXISTS idx_macro_probabilities_daily_key_date
    ON macro_probabilities_daily(probability_key, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_probabilities_daily_date
    ON macro_probabilities_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_probability_v2_target_key_date
    ON macro_probability_v2_target(model_version, probability_key, predictor_as_of_date);

CREATE INDEX IF NOT EXISTS idx_macro_probability_v2_model_key_date
    ON macro_probability_v2_model(model_version, probability_key, calibration_as_of_date, calibration_ready_flag);

CREATE INDEX IF NOT EXISTS idx_macro_probability_v2_daily_key_date
    ON macro_probability_v2_daily(model_version, probability_key, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_probability_v2_diagnostics_key_date
    ON macro_probability_v2_diagnostics(model_version, probability_key, diagnostic_as_of_date);

CREATE INDEX IF NOT EXISTS idx_macro_regime_v2_daily_date
    ON macro_regime_v2_daily(model_version, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_regime_v2_smoothed_date
    ON macro_regime_v2_smoothed_daily(model_version, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_transition_v2_matrix_date
    ON macro_transition_v2_matrix(model_version, as_of_date);

CREATE INDEX IF NOT EXISTS idx_macro_transition_v2_diagnostics_date
    ON macro_transition_v2_diagnostics(model_version, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_regime_v2_decision_date
    ON macro_regime_v2_decision_daily(model_version, as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_regime_v2_promotion_evidence_date
    ON macro_regime_v2_promotion_evidence(model_version, evidence_as_of_date, cell_status);

CREATE INDEX IF NOT EXISTS idx_macro_regime_v2_promotion_summary_date
    ON macro_regime_v2_promotion_summary(model_version, evidence_as_of_date, acceptance);

CREATE INDEX IF NOT EXISTS idx_macro_regime_raw_daily_date
    ON macro_regime_raw_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_regime_raw_daily_current_next
    ON macro_regime_raw_daily(current_regime, next_regime, as_of_date);

CREATE INDEX IF NOT EXISTS idx_macro_regime_smoothed_daily_date
    ON macro_regime_smoothed_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_regime_smoothed_daily_current_next
    ON macro_regime_smoothed_daily(smoothed_current_regime, smoothed_next_regime, as_of_date);

CREATE INDEX IF NOT EXISTS idx_macro_transition_matrix_date
    ON macro_transition_matrix(as_of_date, from_regime, to_regime);

CREATE INDEX IF NOT EXISTS idx_macro_transition_diagnostics_date
    ON macro_transition_diagnostics(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_macro_regime_decision_daily_date
    ON macro_regime_decision_daily(as_of_date, coverage_flag, decision_date_flag);

CREATE INDEX IF NOT EXISTS idx_macro_regime_decision_daily_active
    ON macro_regime_decision_daily(active_current_regime, active_next_regime, as_of_date);

CREATE INDEX IF NOT EXISTS idx_sector_macro_fit_daily_date
    ON sector_macro_fit_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_sector_macro_fit_daily_score
    ON sector_macro_fit_daily(as_of_date, final_score);

CREATE INDEX IF NOT EXISTS idx_industry_aggregate_macro_fit_daily_date
    ON industry_aggregate_macro_fit_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_industry_aggregate_macro_fit_daily_score
    ON industry_aggregate_macro_fit_daily(as_of_date, final_score);

CREATE INDEX IF NOT EXISTS idx_industry_macro_fit_daily_date
    ON industry_macro_fit_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_industry_macro_fit_daily_score
    ON industry_macro_fit_daily(as_of_date, final_score);

CREATE INDEX IF NOT EXISTS idx_country_macro_fit_daily_date
    ON country_macro_fit_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_country_macro_fit_daily_score
    ON country_macro_fit_daily(as_of_date, confidence_adjusted_fit);

CREATE INDEX IF NOT EXISTS idx_country_confidence_daily_date
    ON country_confidence_daily(as_of_date, coverage_flag);

CREATE INDEX IF NOT EXISTS idx_country_macro_rank_daily_date
    ON country_macro_rank_daily(as_of_date, eligible_flag, country_rank);

CREATE INDEX IF NOT EXISTS idx_stock_macro_fit_daily_date
    ON stock_macro_fit_daily(as_of_date, coverage_flag, base_optimizer_eligible);

CREATE INDEX IF NOT EXISTS idx_stock_macro_fit_daily_macro
    ON stock_macro_fit_daily(as_of_date, macro_stock_fit_z);

CREATE INDEX IF NOT EXISTS idx_stock_selection_score_daily_rank
    ON stock_selection_score_daily(as_of_date, coverage_flag, selection_rank);

CREATE INDEX IF NOT EXISTS idx_stock_selection_score_daily_score
    ON stock_selection_score_daily(as_of_date, selection_score);

CREATE INDEX IF NOT EXISTS idx_stock_weight_score_daily_rank
    ON stock_weight_score_daily(as_of_date, coverage_flag, weight_rank);

CREATE INDEX IF NOT EXISTS idx_stock_weight_score_daily_score
    ON stock_weight_score_daily(as_of_date, weight_score);

CREATE INDEX IF NOT EXISTS idx_portfolio_inputs_daily_asset
    ON portfolio_inputs_daily(as_of_date, asset_type, sleeve, state);

CREATE INDEX IF NOT EXISTS idx_portfolio_inputs_daily_score
    ON portfolio_inputs_daily(as_of_date, asset_type, final_score);

CREATE INDEX IF NOT EXISTS idx_portfolio_allocation_summary_date
    ON portfolio_allocation_summary(as_of_date);

CREATE INDEX IF NOT EXISTS idx_stock_industry_target_daily_date
    ON stock_industry_target_daily(as_of_date, coverage_flag, target_rank);

CREATE INDEX IF NOT EXISTS idx_stock_industry_target_daily_weight
    ON stock_industry_target_daily(as_of_date, target_weight);

CREATE INDEX IF NOT EXISTS idx_stock_sector_target_daily_date
    ON stock_sector_target_daily(as_of_date, coverage_flag, target_rank);

CREATE INDEX IF NOT EXISTS idx_stock_sleeve_target_summary_date
    ON stock_sleeve_target_summary(as_of_date);

CREATE INDEX IF NOT EXISTS idx_foreign_sleeve_budget_daily_date
    ON foreign_sleeve_budget_daily(as_of_date, active_flag);

CREATE INDEX IF NOT EXISTS idx_foreign_sleeve_candidate_daily_rank
    ON foreign_sleeve_candidate_daily(as_of_date, selected_flag, candidate_rank);

CREATE INDEX IF NOT EXISTS idx_foreign_sleeve_candidate_daily_score
    ON foreign_sleeve_candidate_daily(as_of_date, foreign_fused_alpha);
"""

_CLEARABLE_TABLES = {
    "macro_calendar_daily",
    "macro_metric_latest",
    "macro_feature_event",
    "macro_feature_daily",
    "macro_composite_daily",
    "macro_composite_component_daily",
    "macro_probability_calibration",
    "macro_probability_diagnostics",
    "macro_probabilities_daily",
    "macro_probability_v2_target",
    "macro_probability_v2_model",
    "macro_probability_v2_daily",
    "macro_probability_v2_diagnostics",
    "macro_regime_v2_daily",
    "macro_regime_v2_smoothed_daily",
    "macro_transition_v2_matrix",
    "macro_transition_v2_diagnostics",
    "macro_regime_v2_decision_daily",
    "macro_regime_v2_promotion_evidence",
    "macro_regime_v2_promotion_summary",
    "macro_regime_raw_daily",
    "macro_regime_smoothed_daily",
    "macro_transition_matrix",
    "macro_transition_diagnostics",
    "macro_regime_decision_daily",
    "sector_macro_fit_daily",
    "industry_aggregate_macro_fit_daily",
    "industry_macro_fit_daily",
    "country_macro_fit_daily",
    "country_confidence_daily",
    "country_macro_rank_daily",
    "stock_macro_fit_daily",
    "stock_selection_score_daily",
    "stock_weight_score_daily",
    "portfolio_inputs_daily",
    "portfolio_allocation_summary",
    "stock_industry_target_daily",
    "stock_sector_target_daily",
    "stock_sleeve_target_summary",
    "foreign_sleeve_budget_daily",
    "foreign_sleeve_candidate_daily",
}
_FEATURE_RANGE_TABLES = {"macro_feature_event", "macro_feature_daily"}
_COMPOSITE_RANGE_TABLES = {"macro_composite_daily", "macro_composite_component_daily"}
_PROBABILITY_RANGE_TABLES = {
    "macro_probability_calibration",
    "macro_probability_diagnostics",
    "macro_probabilities_daily",
}
_PROBABILITY_DATE_COLUMNS = {"calibration_as_of_date", "as_of_date"}
_REGIME_RANGE_TABLES = {
    "macro_regime_raw_daily",
    "macro_regime_smoothed_daily",
    "macro_transition_matrix",
    "macro_transition_diagnostics",
    "macro_regime_decision_daily",
}
_INDUSTRY_MACRO_RANGE_TABLES = {
    "sector_macro_fit_daily",
    "industry_aggregate_macro_fit_daily",
    "industry_macro_fit_daily",
}
_COUNTRY_MACRO_RANGE_TABLES = {
    "country_macro_fit_daily",
    "country_confidence_daily",
    "country_macro_rank_daily",
}
_STOCK_MACRO_RANGE_TABLES = {
    "stock_macro_fit_daily",
    "stock_selection_score_daily",
    "stock_weight_score_daily",
}
_PORTFOLIO_INPUT_RANGE_TABLES = {
    "portfolio_inputs_daily",
    "portfolio_allocation_summary",
}
_STOCK_SLEEVE_TARGET_RANGE_TABLES = {
    "stock_industry_target_daily",
    "stock_sector_target_daily",
    "stock_sleeve_target_summary",
}
_FOREIGN_SLEEVE_BUDGET_RANGE_TABLES = {
    "foreign_sleeve_budget_daily",
    "foreign_sleeve_candidate_daily",
}


def _validate_identifier(value: str, *, allowed: set[str], kind: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(f"Unsupported {kind}: {value!r}")
    return text


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def start_serving_run(
    conn: sqlite3.Connection,
    *,
    serving_run_id: str,
    build_step: str,
    raw_ingest_run_id: str | None,
    as_of_start_date: str | None,
    as_of_end_date: str | None,
    metric_count: int,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO macro_serving_run (
            serving_run_id, build_step, raw_ingest_run_id, as_of_start_date, as_of_end_date,
            metric_count, started_at_utc, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            serving_run_id,
            build_step,
            raw_ingest_run_id,
            as_of_start_date,
            as_of_end_date,
            metric_count,
            utc_now_iso(),
            notes,
        ),
    )
    conn.commit()


def finish_serving_run(
    conn: sqlite3.Connection,
    *,
    serving_run_id: str,
    status: str,
    rows_written: int,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE macro_serving_run
        SET status = ?, rows_written = ?, completed_at_utc = ?, notes = COALESCE(?, notes)
        WHERE serving_run_id = ?
        """,
        (status, rows_written, utc_now_iso(), notes, serving_run_id),
    )
    conn.commit()


def clear_table(conn: sqlite3.Connection, table_name: str) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_CLEARABLE_TABLES, kind="table_name")
    conn.execute(f"DELETE FROM {table_name_safe}")
    conn.commit()


def clear_pit_range(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    metric_keys: list[str] | None = None,
) -> None:
    if metric_keys:
        placeholders = ",".join("?" for _ in metric_keys)
        conn.execute(
            f"""
            DELETE FROM macro_observation_daily_pit
            WHERE as_of_date BETWEEN ? AND ?
              AND metric_key IN ({placeholders})
            """,
            (start_date, end_date, *metric_keys),
        )
    else:
        conn.execute(
            """
            DELETE FROM macro_observation_daily_pit
            WHERE as_of_date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        )
    conn.commit()


def clear_country_coverage_range(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> None:
    conn.execute(
        """
        DELETE FROM macro_country_coverage_daily
        WHERE as_of_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )
    conn.commit()


def clear_feature_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
    metric_keys: list[str] | None = None,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_FEATURE_RANGE_TABLES, kind="table_name")
    if metric_keys:
        placeholders = ",".join("?" for _ in metric_keys)
        conn.execute(
            f"""
            DELETE FROM {table_name_safe}
            WHERE as_of_date BETWEEN ? AND ?
              AND metric_key IN ({placeholders})
            """,
            (start_date, end_date, *metric_keys),
        )
    else:
        conn.execute(
            f"""
            DELETE FROM {table_name_safe}
            WHERE as_of_date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        )
    conn.commit()


def clear_composite_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
    composite_keys: list[str] | None = None,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_COMPOSITE_RANGE_TABLES, kind="table_name")
    if composite_keys:
        placeholders = ",".join("?" for _ in composite_keys)
        conn.execute(
            f"""
            DELETE FROM {table_name_safe}
            WHERE as_of_date BETWEEN ? AND ?
              AND composite_key IN ({placeholders})
            """,
            (start_date, end_date, *composite_keys),
        )
    else:
        conn.execute(
            f"""
            DELETE FROM {table_name_safe}
            WHERE as_of_date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        )
    conn.commit()


def clear_probability_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    date_column: str,
    start_date: str,
    end_date: str,
    probability_keys: list[str] | None = None,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_PROBABILITY_RANGE_TABLES, kind="table_name")
    date_column_safe = _validate_identifier(date_column, allowed=_PROBABILITY_DATE_COLUMNS, kind="date_column")
    if probability_keys:
        placeholders = ",".join("?" for _ in probability_keys)
        conn.execute(
            f"""
            DELETE FROM {table_name_safe}
            WHERE {date_column_safe} BETWEEN ? AND ?
              AND probability_key IN ({placeholders})
            """,
            (start_date, end_date, *probability_keys),
        )
    else:
        conn.execute(
            f"""
            DELETE FROM {table_name_safe}
            WHERE {date_column_safe} BETWEEN ? AND ?
            """,
            (start_date, end_date),
        )
    conn.commit()


def clear_regime_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_REGIME_RANGE_TABLES, kind="table_name")
    conn.execute(
        f"""
        DELETE FROM {table_name_safe}
        WHERE as_of_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )
    conn.commit()


def clear_industry_macro_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_INDUSTRY_MACRO_RANGE_TABLES, kind="table_name")
    conn.execute(
        f"""
        DELETE FROM {table_name_safe}
        WHERE as_of_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )
    conn.commit()


def clear_country_macro_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_COUNTRY_MACRO_RANGE_TABLES, kind="table_name")
    conn.execute(
        f"""
        DELETE FROM {table_name_safe}
        WHERE as_of_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )
    conn.commit()


def clear_stock_macro_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_STOCK_MACRO_RANGE_TABLES, kind="table_name")
    conn.execute(
        f"""
        DELETE FROM {table_name_safe}
        WHERE as_of_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )
    conn.commit()


def clear_portfolio_input_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_PORTFOLIO_INPUT_RANGE_TABLES, kind="table_name")
    conn.execute(
        f"""
        DELETE FROM {table_name_safe}
        WHERE as_of_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )
    conn.commit()


def clear_stock_sleeve_target_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_STOCK_SLEEVE_TARGET_RANGE_TABLES, kind="table_name")
    conn.execute(
        f"""
        DELETE FROM {table_name_safe}
        WHERE as_of_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )
    conn.commit()


def clear_foreign_sleeve_budget_range(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    start_date: str,
    end_date: str,
) -> None:
    table_name_safe = _validate_identifier(table_name, allowed=_FOREIGN_SLEEVE_BUDGET_RANGE_TABLES, kind="table_name")
    conn.execute(
        f"""
        DELETE FROM {table_name_safe}
        WHERE as_of_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    )
    conn.commit()


def insert_many(
    conn: sqlite3.Connection,
    sql: str,
    rows: list[tuple[Any, ...]],
    *,
    chunk_size: int | None = None,
) -> int:
    if not rows:
        return 0
    if chunk_size is None or int(chunk_size) <= 0 or len(rows) <= int(chunk_size):
        conn.executemany(sql, rows)
        conn.commit()
        return len(rows)
    total = 0
    for start in range(0, len(rows), int(chunk_size)):
        chunk = rows[start : start + int(chunk_size)]
        conn.executemany(sql, chunk)
        conn.commit()
        total += len(chunk)
    return total
