#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
import sqlite3
from collections import Counter
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from sec_form4_config import cfg_get, load_sec_form4_config

DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\STAGING\DB\sec_insider.sqlite")

RAW_REQUIRED_TABLES = (
    "sec_ownership_submission",
    "sec_ownership_reporting_owner",
    "sec_ownership_nonderiv_trans",
)
OUTPUT_REQUIRED_TABLES = (
    "form4_buy_events_v1",
)


def default_db_path() -> Path:
    return Path(os.getenv("SEC_INSIDER_DB_PATH", str(DEFAULT_DB_PATH)))


NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = timezone.utc

DEFAULT_SCORING_PARAMS: dict[str, float | int] = {
    # Role weights
    "role_weight_top_exec": 1.50,
    "role_weight_senior_officer": 1.35,
    "role_weight_officer_or_director": 1.20,
    "role_weight_ten_percent_owner": 0.90,
    "role_weight_other": 1.00,
    # Raw event component weights
    "ownership_weight_base": 1.00,
    "ownership_weight_ratio_cap": 1.50,
    "ownership_weight_ratio_multiplier": 0.50,
    "size_weight_base": 1.00,
    "size_weight_log_cap": 14.0,
    "size_weight_log_divisor": 14.0,
    "plan_weight_sell_10b5": 0.60,
    "plan_weight_buy_10b5": 0.85,
    "plan_weight_default": 1.00,
    "direct_weight_direct": 1.10,
    "direct_weight_indirect": 0.85,
    "base_signal_buy": 1.00,
    "base_signal_sell": 0.55,
    "base_signal_other": 1.00,
    # Clustering and decay
    "cluster_threshold_two_plus": 2,
    "cluster_threshold_three_plus": 3,
    "cluster_window_5bd": 5,
    "cluster_window_10bd": 10,
    "cluster_window_20bd": 20,
    "buy_cluster_weight_2plus": 1.4,
    "buy_cluster_weight_3plus": 1.8,
    "buy_cluster_weight_default": 1.0,
    "sell_cluster_weight_2plus": 1.2,
    "sell_cluster_weight_3plus": 1.5,
    "sell_cluster_weight_default": 1.0,
    "cluster_weight_default_other_side": 1.0,
    "decay_tau_business_days": 20.0,
    "snapshot_score_window_bd": 20,
    "snapshot_distinct_window_bd": 10,
    # Snapshot rank composition
    "snapshot_net_sell_penalty": 0.60,
    "snapshot_long_rank_buy_distinct_weight": 0.35,
    "snapshot_long_rank_buy_distinct_cap": 3,
    "snapshot_long_rank_buy_cluster_weight": 0.20,
    "snapshot_long_rank_buy_cluster_cap": 2,
    "snapshot_exit_risk_buy_offset": 0.40,
    "snapshot_exit_risk_sell_distinct_weight": 0.25,
    "snapshot_exit_risk_sell_distinct_cap": 3,
    "snapshot_exit_risk_sell_cluster_weight": 0.20,
    "snapshot_exit_risk_sell_cluster_cap": 2,
    # Action bucket thresholds
    "action_buy_tier1_buy_score_min": 5.0,
    "action_buy_tier1_buy_cluster10_min": 2,
    "action_buy_tier1_sell_score_max_exclusive": 2.5,
    "action_buy_watch_long_rank_min": 3.0,
    "action_buy_watch_buy_score_min_exclusive": 0.0,
    "action_avoid_trim_exit_risk_min": 4.0,
    "action_avoid_trim_sell_cluster10_min": 2,
    "action_avoid_trim_buy_score_max_exclusive": 2.0,
    "action_sell_review_sell_score_min": 3.0,
    "action_sell_review_buy_score_max_exclusive": 2.0,
}

INT_SCORING_KEYS = {
    "cluster_threshold_two_plus",
    "cluster_threshold_three_plus",
    "cluster_window_5bd",
    "cluster_window_10bd",
    "cluster_window_20bd",
    "snapshot_score_window_bd",
    "snapshot_distinct_window_bd",
    "snapshot_long_rank_buy_distinct_cap",
    "snapshot_long_rank_buy_cluster_cap",
    "snapshot_exit_risk_sell_distinct_cap",
    "snapshot_exit_risk_sell_cluster_cap",
    "action_buy_tier1_buy_cluster10_min",
    "action_avoid_trim_sell_cluster10_min",
}

SQL_CREATE_TIER1_TABLE = """
CREATE TABLE IF NOT EXISTS form4_events_tier1 (
    event_id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key                       TEXT NOT NULL UNIQUE,
    event_fingerprint               TEXT,
    accession_number                TEXT NOT NULL,
    nonderiv_trans_sk               TEXT NOT NULL,
    is_current_truth                INTEGER NOT NULL DEFAULT 1,
    is_amendment                    INTEGER NOT NULL DEFAULT 0,
    document_type                   TEXT NOT NULL,
    filing_date                     TEXT,
    filing_date_sort                TEXT,
    accepted_ts_utc                 TEXT,
    tradable_date                   TEXT,
    tradable_session                TEXT,
    filing_lag_bd                   INTEGER,
    period_of_report                TEXT,
    date_of_original_submission     TEXT,
    issuer_cik                      TEXT,
    issuer_trading_symbol           TEXT,
    rptowner_cik                    TEXT,
    rptowner_name                   TEXT,
    rptowner_relationship           TEXT,
    rptowner_title                  TEXT,
    security_title                  TEXT,
    trans_date                      TEXT,
    trans_code                      TEXT,
    signal_side                     TEXT,
    trans_direction                 TEXT,
    trans_shares                    REAL,
    trans_price_per_share           REAL,
    trans_acquired_disp_cd          TEXT,
    shrs_ownd_folwng_trans          REAL,
    prior_shares                    REAL,
    direct_indirect_ownership       TEXT,
    nature_of_ownership             TEXT,
    trade_value_usd                 REAL,
    aff10b5one_flag                 INTEGER,
    role_weight                     REAL,
    ownership_weight                REAL,
    size_weight                     REAL,
    plan_weight                     REAL,
    direct_weight                   REAL,
    base_score                      REAL,
    raw_event_score                 REAL,
    cluster_insiders_5bd            INTEGER,
    cluster_insiders_10bd           INTEGER,
    cluster_insiders_20bd           INTEGER,
    cluster_weight                  REAL,
    event_score                     REAL,
    buy_score                       REAL,
    sell_risk_score                 REAL,
    signed_event_score              REAL,
    decayed_score                   REAL,
    net_event_score                 REAL,
    routine_flag                    INTEGER,
    opportunistic_flag              INTEGER,
    close_px                        REAL,
    adv20_usd                       REAL,
    liquidity_pass                  INTEGER,
    tradeable_alpha_score           REAL,
    source_dataset_id               TEXT
);
CREATE INDEX IF NOT EXISTS idx_form4_tier1_symbol_filing_date
    ON form4_events_tier1(issuer_trading_symbol, filing_date_sort);
CREATE INDEX IF NOT EXISTS idx_form4_tier1_issuer_filing_date
    ON form4_events_tier1(issuer_cik, filing_date_sort);
CREATE INDEX IF NOT EXISTS idx_form4_tier1_trans_code
    ON form4_events_tier1(trans_code);
CREATE INDEX IF NOT EXISTS idx_form4_tier1_signal_side
    ON form4_events_tier1(signal_side);
CREATE INDEX IF NOT EXISTS idx_form4_tier1_issuer_side_date
    ON form4_events_tier1(issuer_cik, signal_side, filing_date_sort);
"""

SQL_CREATE_SNAPSHOT_TABLE = """
CREATE TABLE IF NOT EXISTS stock_signal_snapshot_tier1 (
    as_of_date                      TEXT NOT NULL,
    issuer_cik                      TEXT NOT NULL,
    issuer_trading_symbol           TEXT,
    buy_score_20bd                  REAL,
    sell_score_20bd                 REAL,
    net_score                       REAL,
    long_rank_score                 REAL,
    exit_risk_score                 REAL,
    buy_cluster_5bd_max             INTEGER,
    buy_cluster_10bd_max            INTEGER,
    buy_cluster_20bd_max            INTEGER,
    sell_cluster_5bd_max            INTEGER,
    sell_cluster_10bd_max           INTEGER,
    sell_cluster_20bd_max           INTEGER,
    distinct_buy_insiders_10bd      INTEGER,
    distinct_sell_insiders_10bd     INTEGER,
    action_bucket                   TEXT,
    PRIMARY KEY (as_of_date, issuer_cik)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_tier1_rank
    ON stock_signal_snapshot_tier1(as_of_date, long_rank_score DESC, buy_score_20bd DESC);
CREATE INDEX IF NOT EXISTS idx_snapshot_tier1_action
    ON stock_signal_snapshot_tier1(as_of_date, action_bucket);
"""

DATE_SORT_SQL = """
CASE
    WHEN {col} IS NULL THEN NULL
    WHEN length({col}) = 10
         AND substr({col}, 5, 1) = '-'
         AND substr({col}, 8, 1) = '-'
    THEN {col}
    WHEN length({col}) = 11
         AND substr({col}, 3, 1) = '-'
         AND substr({col}, 7, 1) = '-'
    THEN substr({col}, 8, 4) || '-' ||
         CASE upper(substr({col}, 4, 3))
             WHEN 'JAN' THEN '01'
             WHEN 'FEB' THEN '02'
             WHEN 'MAR' THEN '03'
             WHEN 'APR' THEN '04'
             WHEN 'MAY' THEN '05'
             WHEN 'JUN' THEN '06'
             WHEN 'JUL' THEN '07'
             WHEN 'AUG' THEN '08'
             WHEN 'SEP' THEN '09'
             WHEN 'OCT' THEN '10'
             WHEN 'NOV' THEN '11'
             WHEN 'DEC' THEN '12'
             ELSE '00'
         END || '-' || substr({col}, 1, 2)
    ELSE NULL
END
"""

SQL_BUILD_TIER1_TEMPLATE = """
INSERT INTO form4_events_tier1 (
    event_key,
    event_fingerprint,
    accession_number,
    nonderiv_trans_sk,
    is_current_truth,
    is_amendment,
    document_type,
    filing_date,
    filing_date_sort,
    accepted_ts_utc,
    tradable_date,
    tradable_session,
    filing_lag_bd,
    period_of_report,
    date_of_original_submission,
    issuer_cik,
    issuer_trading_symbol,
    rptowner_cik,
    rptowner_name,
    rptowner_relationship,
    rptowner_title,
    security_title,
    trans_date,
    trans_code,
    signal_side,
    trans_direction,
    trans_shares,
    trans_price_per_share,
    trans_acquired_disp_cd,
    shrs_ownd_folwng_trans,
    prior_shares,
    direct_indirect_ownership,
    nature_of_ownership,
    trade_value_usd,
    aff10b5one_flag,
    role_weight,
    ownership_weight,
    size_weight,
    plan_weight,
    direct_weight,
    base_score,
    raw_event_score,
    cluster_insiders_5bd,
    cluster_insiders_10bd,
    cluster_insiders_20bd,
    cluster_weight,
    event_score,
    buy_score,
    sell_risk_score,
    signed_event_score,
    decayed_score,
    net_event_score,
    routine_flag,
    opportunistic_flag,
    close_px,
    adv20_usd,
    liquidity_pass,
    tradeable_alpha_score,
    source_dataset_id
)
WITH ro_raw AS (
    SELECT
        accession_number,
        rptowner_cik,
        rptowner_name,
        COALESCE(NULLIF(trim(rptowner_title), ''), NULLIF(trim(officer_title), '')) AS resolved_title,
        upper(COALESCE(NULLIF(trim(rptowner_relationship), ''), '')) AS rel_text,
        CASE
            WHEN COALESCE(is_officer, '') IN ('1', 'Y', 'true', 'TRUE')
                 OR upper(COALESCE(rptowner_relationship, '')) LIKE '%OFFICER%' THEN 1
            ELSE 0
        END AS is_officer_flag,
        CASE
            WHEN COALESCE(is_director, '') IN ('1', 'Y', 'true', 'TRUE')
                 OR upper(COALESCE(rptowner_relationship, '')) LIKE '%DIRECTOR%' THEN 1
            ELSE 0
        END AS is_director_flag,
        CASE
            WHEN COALESCE(is_ten_percent_owner, '') IN ('1', 'Y', 'true', 'TRUE')
                 OR upper(COALESCE(rptowner_relationship, '')) LIKE '%TENPERCENTOWNER%'
                 OR upper(COALESCE(rptowner_relationship, '')) LIKE '%10%%' THEN 1
            ELSE 0
        END AS is_ten_percent_owner_flag
    FROM sec_ownership_reporting_owner
),
ro AS (
    SELECT
        accession_number,
        rptowner_cik,
        rptowner_name,
        resolved_title,
        CASE
            WHEN (
                is_officer_flag = 1
                AND (
                    lower(COALESCE(resolved_title, '')) GLOB '*chief executive*'
                    OR lower(COALESCE(resolved_title, '')) GLOB '*ceo*'
                )
            ) THEN 'CEO'
            WHEN (
                is_officer_flag = 1
                AND (
                    lower(COALESCE(resolved_title, '')) GLOB '*chief financial*'
                    OR lower(COALESCE(resolved_title, '')) GLOB '*cfo*'
                )
            ) THEN 'CFO'
            WHEN (
                is_director_flag = 1
                AND lower(COALESCE(resolved_title, '')) GLOB '*chair*'
            ) THEN 'Chair'
            WHEN is_officer_flag = 1 THEN 'Officer'
            WHEN is_director_flag = 1 THEN 'Director'
            WHEN is_ten_percent_owner_flag = 1 THEN '10% Owner'
            WHEN rel_text <> '' THEN rel_text
            ELSE 'Other'
        END AS rptowner_relationship,
        CASE
            WHEN (
                (
                    is_officer_flag = 1
                    AND (
                        lower(COALESCE(resolved_title, '')) GLOB '*chief executive*'
                        OR lower(COALESCE(resolved_title, '')) GLOB '*ceo*'
                        OR lower(COALESCE(resolved_title, '')) GLOB '*chief financial*'
                        OR lower(COALESCE(resolved_title, '')) GLOB '*cfo*'
                    )
                )
                OR lower(COALESCE(resolved_title, '')) GLOB '*chair*'
            ) THEN {role_weight_top_exec}
            WHEN (
                is_officer_flag = 1
                AND (
                    lower(COALESCE(resolved_title, '')) GLOB '*president*'
                    OR lower(COALESCE(resolved_title, '')) GLOB '*chief operating*'
                    OR lower(COALESCE(resolved_title, '')) GLOB '*coo*'
                    OR lower(COALESCE(resolved_title, '')) GLOB '*chief business*'
                )
            ) THEN {role_weight_senior_officer}
            WHEN is_officer_flag = 1 OR is_director_flag = 1 THEN {role_weight_officer_or_director}
            WHEN is_ten_percent_owner_flag = 1 THEN {role_weight_ten_percent_owner}
            ELSE {role_weight_other}
        END AS role_weight
    FROM ro_raw
),
base AS (
    SELECT
        s.accession_number,
        n.nonderiv_trans_sk,
        s.document_type,
        s.filing_date,
        {filing_date_sort} AS filing_date_sort,
        s.accepted_ts_utc AS accepted_ts_utc,
        s.period_of_report,
        s.date_of_original_submission,
        s.issuer_cik,
        s.issuer_trading_symbol,
        ro.rptowner_cik,
        ro.rptowner_name,
        ro.rptowner_relationship,
        ro.resolved_title AS rptowner_title,
        n.security_title,
        n.transaction_date AS trans_date,
        {trans_date_sort} AS trans_date_sort,
        n.transaction_code AS trans_code,
        CASE
            WHEN COALESCE(n.transaction_code, '') = 'P' THEN 'BUY'
            WHEN COALESCE(n.transaction_code, '') = 'S' THEN 'SELL'
            ELSE 'OTHER'
        END AS signal_side,
        CASE
            WHEN COALESCE(n.transaction_code, '') = 'P' THEN 'BUY'
            WHEN COALESCE(n.transaction_code, '') = 'S' THEN 'SELL'
            ELSE 'OTHER'
        END AS trans_direction,
        n.transaction_shares AS trans_shares,
        n.transaction_price_per_share AS trans_price_per_share,
        n.transaction_acquired_disposed_code AS trans_acquired_disp_cd,
        n.shares_owned_following_transaction AS shrs_ownd_folwng_trans,
        CASE
            WHEN COALESCE(n.transaction_code, '') = 'P'
                 AND n.shares_owned_following_transaction IS NOT NULL
                 AND n.transaction_shares IS NOT NULL
            THEN MAX(n.shares_owned_following_transaction - n.transaction_shares, 0.0)
            WHEN COALESCE(n.transaction_code, '') = 'S'
                 AND n.shares_owned_following_transaction IS NOT NULL
                 AND n.transaction_shares IS NOT NULL
            THEN MAX(n.shares_owned_following_transaction + n.transaction_shares, 0.0)
            ELSE NULL
        END AS prior_shares,
        n.direct_or_indirect_ownership AS direct_indirect_ownership,
        n.nature_of_ownership,
        CASE
            WHEN n.transaction_shares IS NOT NULL
                 AND n.transaction_price_per_share IS NOT NULL
            THEN ABS(n.transaction_shares * n.transaction_price_per_share)
            ELSE NULL
        END AS trade_value_usd,
        CASE
            WHEN COALESCE(s.aff10b5one, '') IN ('1', 'Y', 'true', 'TRUE') THEN 1
            ELSE 0
        END AS aff10b5one_flag,
        COALESCE(ro.role_weight, 1.0) AS role_weight,
        CASE WHEN s.document_type = '4/A' THEN 1 ELSE 0 END AS is_amendment,
        (
            lower(trim(COALESCE(s.issuer_cik, ''))) || '|' ||
            lower(trim(COALESCE(ro.rptowner_cik, ro.rptowner_name, ''))) || '|' ||
            upper(COALESCE(n.transaction_date, '')) || '|' ||
            upper(COALESCE(n.transaction_code, '')) || '|' ||
            lower(trim(COALESCE(n.security_title, ''))) || '|' ||
            upper(COALESCE(n.direct_or_indirect_ownership, '')) || '|' ||
            upper(COALESCE(n.transaction_acquired_disposed_code, '')) || '|' ||
            COALESCE(n.nonderiv_trans_sk, '')
        ) AS event_fingerprint,
        s.source_dataset_id
    FROM sec_ownership_submission s
    JOIN sec_ownership_nonderiv_trans n
      ON s.accession_number = n.accession_number
    LEFT JOIN ro
      ON s.accession_number = ro.accession_number
    WHERE s.document_type IN ('4', '4/A')
      AND COALESCE(n.transaction_code, '') IN ('P', 'S')
      AND (
            lower(COALESCE(n.security_title, '')) LIKE '%common stock%'
            OR lower(COALESCE(n.security_title, '')) LIKE '%common shares%'
            OR lower(COALESCE(n.security_title, '')) LIKE '%ordinary share%'
            OR lower(COALESCE(n.security_title, '')) LIKE '%ordinary shares%'
            OR lower(COALESCE(n.security_title, '')) LIKE '%class a common%'
            OR lower(COALESCE(n.security_title, '')) LIKE '%class b common%'
            OR lower(COALESCE(n.security_title, '')) LIKE '%class c common%'
          )
      AND lower(COALESCE(n.security_title, '')) NOT LIKE '%preferred%'
      AND lower(COALESCE(n.security_title, '')) NOT LIKE '%warrant%'
      AND lower(COALESCE(n.security_title, '')) NOT LIKE '%option%'
      AND lower(COALESCE(n.security_title, '')) NOT LIKE '%rsu%'
      AND lower(COALESCE(n.security_title, '')) NOT LIKE '%restricted stock unit%'
      AND lower(COALESCE(n.security_title, '')) NOT LIKE '%phantom%'
      AND lower(COALESCE(n.security_title, '')) NOT LIKE '%stock appreciation%'
      AND COALESCE(n.direct_or_indirect_ownership, '') IN ('', 'D')
      AND n.transaction_shares IS NOT NULL
      AND n.transaction_shares > 0
      AND n.transaction_price_per_share IS NOT NULL
      AND n.transaction_price_per_share > 0
),
weighted AS (
    SELECT
        b.*,
        CASE
            WHEN b.prior_shares IS NOT NULL AND b.prior_shares > 0
            THEN {ownership_weight_base}
                 + MIN(
                     ABS(b.trans_shares) / MAX(b.prior_shares, 1.0),
                     {ownership_weight_ratio_cap}
                 ) * {ownership_weight_ratio_multiplier}
            ELSE {ownership_weight_base}
        END AS ownership_weight,
        CASE
            WHEN b.trade_value_usd IS NOT NULL
            THEN {size_weight_base}
                 + MIN(log(1.0 + b.trade_value_usd), {size_weight_log_cap}) / {size_weight_log_divisor}
            ELSE {size_weight_base}
        END AS size_weight,
        CASE
            WHEN b.aff10b5one_flag = 1 AND b.signal_side = 'SELL' THEN {plan_weight_sell_10b5}
            WHEN b.aff10b5one_flag = 1 AND b.signal_side = 'BUY' THEN {plan_weight_buy_10b5}
            ELSE {plan_weight_default}
        END AS plan_weight,
        CASE
            WHEN COALESCE(b.direct_indirect_ownership, '') = 'D' THEN {direct_weight_direct}
            ELSE {direct_weight_indirect}
        END AS direct_weight,
        CASE
            WHEN b.signal_side = 'BUY' THEN {base_signal_buy}
            WHEN b.signal_side = 'SELL' THEN {base_signal_sell}
            ELSE {base_signal_other}
        END AS base_signal
    FROM base b
),
ranked AS (
    SELECT
        w.*,
        (
            w.base_signal
            * w.role_weight
            * w.size_weight
            * w.ownership_weight
            * w.plan_weight
            * w.direct_weight
        ) AS raw_event_score,
        ROW_NUMBER() OVER (
            PARTITION BY w.event_fingerprint
            ORDER BY
                w.is_amendment DESC,
                w.filing_date_sort DESC,
                w.accession_number DESC
        ) AS rn
    FROM weighted w
)
SELECT
    (
        accession_number || '|' ||
        COALESCE(nonderiv_trans_sk, '') || '|' ||
        lower(trim(COALESCE(rptowner_cik, rptowner_name, '')))
    ) AS event_key,
    event_fingerprint,
    accession_number,
    nonderiv_trans_sk,
    1 AS is_current_truth,
    is_amendment,
    document_type,
    filing_date,
    filing_date_sort,
    accepted_ts_utc,
    NULL AS tradable_date,
    NULL AS tradable_session,
    CASE
        WHEN filing_date_sort IS NOT NULL AND trans_date_sort IS NOT NULL
        THEN CAST(julianday(filing_date_sort) - julianday(trans_date_sort) AS INTEGER)
        ELSE NULL
    END AS filing_lag_bd,
    period_of_report,
    date_of_original_submission,
    issuer_cik,
    issuer_trading_symbol,
    rptowner_cik,
    rptowner_name,
    rptowner_relationship,
    rptowner_title,
    security_title,
    trans_date,
    trans_code,
    signal_side,
    trans_direction,
    trans_shares,
    trans_price_per_share,
    trans_acquired_disp_cd,
    shrs_ownd_folwng_trans,
    prior_shares,
    direct_indirect_ownership,
    nature_of_ownership,
    trade_value_usd,
    aff10b5one_flag,
    role_weight,
    ownership_weight,
    size_weight,
    plan_weight,
    direct_weight,
    raw_event_score AS base_score,
    raw_event_score,
    1 AS cluster_insiders_5bd,
    1 AS cluster_insiders_10bd,
    1 AS cluster_insiders_20bd,
    1.0 AS cluster_weight,
    raw_event_score AS event_score,
    CASE WHEN signal_side = 'BUY' THEN raw_event_score ELSE 0.0 END AS buy_score,
    CASE WHEN signal_side = 'SELL' THEN raw_event_score ELSE 0.0 END AS sell_risk_score,
    CASE
        WHEN signal_side = 'BUY' THEN raw_event_score
        WHEN signal_side = 'SELL' THEN -raw_event_score
        ELSE 0.0
    END AS signed_event_score,
    raw_event_score AS decayed_score,
    CASE
        WHEN signal_side = 'BUY' THEN raw_event_score
        WHEN signal_side = 'SELL' THEN -raw_event_score
        ELSE 0.0
    END AS net_event_score,
    0 AS routine_flag,
    CASE WHEN COALESCE(aff10b5one_flag, 0) = 1 THEN 0 ELSE 1 END AS opportunistic_flag,
    NULL AS close_px,
    NULL AS adv20_usd,
    1 AS liquidity_pass,
    CASE
        WHEN signal_side = 'BUY' THEN raw_event_score
        WHEN signal_side = 'SELL' THEN -raw_event_score
        ELSE 0.0
    END AS tradeable_alpha_score,
    source_dataset_id
FROM ranked
WHERE rn = 1;
"""
SQL_DELETE_LEGACY_BUYS = "DELETE FROM form4_buy_events_v1"

SQL_INSERT_LEGACY_BUYS = """
INSERT INTO form4_buy_events_v1 (
    accession_number,
    nonderiv_trans_sk,
    document_type,
    filing_date,
    period_of_report,
    date_of_original_submission,
    issuer_cik,
    issuer_trading_symbol,
    rptowner_cik,
    rptowner_name,
    rptowner_relationship,
    rptowner_title,
    security_title,
    trans_date,
    trans_code,
    trans_shares,
    trans_price_per_share,
    trans_acquired_disp_cd,
    shrs_ownd_folwng_trans,
    prior_shares,
    direct_indirect_ownership,
    nature_of_ownership,
    trade_value_usd,
    role_weight,
    ownership_weight,
    size_weight,
    event_score,
    source_dataset_id
)
SELECT
    accession_number,
    nonderiv_trans_sk,
    document_type,
    filing_date,
    period_of_report,
    date_of_original_submission,
    issuer_cik,
    issuer_trading_symbol,
    rptowner_cik,
    rptowner_name,
    rptowner_relationship,
    rptowner_title,
    security_title,
    trans_date,
    trans_code,
    trans_shares,
    trans_price_per_share,
    trans_acquired_disp_cd,
    shrs_ownd_folwng_trans,
    prior_shares,
    direct_indirect_ownership,
    nature_of_ownership,
    trade_value_usd,
    role_weight,
    ownership_weight,
    size_weight,
    buy_score AS event_score,
    source_dataset_id
FROM form4_events_tier1
WHERE trans_code = 'P';
"""


def assert_required_tables(conn: sqlite3.Connection) -> None:
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [
        name
        for name in (*RAW_REQUIRED_TABLES, *OUTPUT_REQUIRED_TABLES)
        if name not in existing_tables
    ]
    if missing:
        raise RuntimeError(
            "SQLite schema is missing required tables: "
            f"{', '.join(missing)}. Run helper_scripts/init_sqlite_db.py and ingest first."
        )


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1].lower()
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def ensure_column(conn: sqlite3.Connection, table_name: str, col_name: str, decl: str) -> None:
    if col_name.lower() not in table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {decl}")


def ensure_source_schema(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "sec_ownership_submission", "issuer_name", "TEXT")
    ensure_column(conn, "sec_ownership_submission", "aff10b5one", "TEXT")
    ensure_column(conn, "sec_ownership_submission", "accepted_ts_utc", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "rptowner_relationship", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "rptowner_title", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "officer_title", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "is_director", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "is_officer", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "is_ten_percent_owner", "TEXT")
    ensure_column(conn, "sec_ownership_reporting_owner", "is_other", "TEXT")


def ensure_output_schema(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "form4_events_tier1", "accepted_ts_utc", "TEXT")
    ensure_column(conn, "form4_events_tier1", "tradable_date", "TEXT")
    ensure_column(conn, "form4_events_tier1", "tradable_session", "TEXT")
    ensure_column(conn, "form4_events_tier1", "filing_lag_bd", "INTEGER")
    ensure_column(conn, "form4_events_tier1", "routine_flag", "INTEGER")
    ensure_column(conn, "form4_events_tier1", "opportunistic_flag", "INTEGER")
    ensure_column(conn, "form4_events_tier1", "close_px", "REAL")
    ensure_column(conn, "form4_events_tier1", "adv20_usd", "REAL")
    ensure_column(conn, "form4_events_tier1", "liquidity_pass", "INTEGER")
    ensure_column(conn, "form4_events_tier1", "tradeable_alpha_score", "REAL")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_form4_tier1_tradable_date
            ON form4_events_tier1(tradable_date, issuer_trading_symbol)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_form4_tier1_tradeable_alpha
            ON form4_events_tier1(tradeable_alpha_score DESC)
        """
    )


def ensure_log_function(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("SELECT log(2.0)")
    except sqlite3.OperationalError:
        conn.create_function(
            "log",
            1,
            lambda x: None if x is None else math.log(float(x)),
        )


def _get_scoring_value(
    scoring_cfg: dict[str, Any],
    key: str,
    default: float | int,
) -> float | int:
    raw = scoring_cfg.get(key, default)
    if raw is None:
        raw = default
    if isinstance(raw, bool):
        raise ValueError(f"Invalid boolean for scoring.{key}: {raw!r}")
    if key in INT_SCORING_KEYS:
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid integer for scoring.{key}: {raw!r}") from exc
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid float for scoring.{key}: {raw!r}") from exc


def resolve_scoring_params(cfg: dict[str, Any]) -> dict[str, float | int]:
    scoring_cfg = cfg_get(cfg, "scoring", default={})
    if not isinstance(scoring_cfg, dict):
        scoring_cfg = {}
    params: dict[str, float | int] = {}
    for key, default in DEFAULT_SCORING_PARAMS.items():
        params[key] = _get_scoring_value(scoring_cfg, key, default)
    if float(params["size_weight_log_divisor"]) <= 0:
        raise ValueError("scoring.size_weight_log_divisor must be > 0")
    if float(params["decay_tau_business_days"]) <= 0:
        raise ValueError("scoring.decay_tau_business_days must be > 0")
    if int(params["cluster_threshold_three_plus"]) < int(params["cluster_threshold_two_plus"]):
        raise ValueError(
            "scoring.cluster_threshold_three_plus must be >= scoring.cluster_threshold_two_plus"
        )
    if int(params["cluster_window_5bd"]) <= 0:
        raise ValueError("scoring.cluster_window_5bd must be > 0")
    if int(params["cluster_window_10bd"]) <= 0:
        raise ValueError("scoring.cluster_window_10bd must be > 0")
    if int(params["cluster_window_20bd"]) <= 0:
        raise ValueError("scoring.cluster_window_20bd must be > 0")
    if not (
        int(params["cluster_window_5bd"])
        <= int(params["cluster_window_10bd"])
        <= int(params["cluster_window_20bd"])
    ):
        raise ValueError(
            "scoring cluster windows must satisfy "
            "cluster_window_5bd <= cluster_window_10bd <= cluster_window_20bd"
        )
    if int(params["snapshot_score_window_bd"]) <= 0:
        raise ValueError("scoring.snapshot_score_window_bd must be > 0")
    if int(params["snapshot_distinct_window_bd"]) <= 0:
        raise ValueError("scoring.snapshot_distinct_window_bd must be > 0")
    return params


def sql_number(value: float | int) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return format(float(value), ".15g")


def render_tier1_sql(scoring: dict[str, float | int]) -> str:
    filing_date_sort = DATE_SORT_SQL.format(col="s.filing_date")
    trans_date_sort = DATE_SORT_SQL.format(col="n.transaction_date")
    sql_params = {
        "filing_date_sort": filing_date_sort,
        "trans_date_sort": trans_date_sort,
        **{k: sql_number(v) for k, v in scoring.items()},
    }
    return SQL_BUILD_TIER1_TEMPLATE.format(
        **sql_params
    )


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def subtract_business_days(ref_date: date, days_back: int) -> date:
    cur = ref_date
    steps = 0
    while steps < max(days_back, 0):
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            steps += 1
    return cur


def business_days_between(start_date: date, end_date: date) -> int:
    if end_date <= start_date:
        return 0
    days = (end_date - start_date).days
    weeks, remainder = divmod(days, 7)
    out = weeks * 5
    for i in range(remainder):
        if (start_date.weekday() + i) % 7 < 5:
            out += 1
    return out


def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def previous_or_same_business_day(d: date) -> date:
    cur = d
    while not is_business_day(cur):
        cur -= timedelta(days=1)
    return cur


def next_business_day(d: date, *, inclusive: bool = False) -> date:
    cur = d if inclusive else (d + timedelta(days=1))
    while not is_business_day(cur):
        cur += timedelta(days=1)
    return cur


def parse_cutoff_time(raw: object, default: dt_time = dt_time(15, 45)) -> dt_time:
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        hh, mm = text.split(":", 1)
        return dt_time(int(hh), int(mm))
    except Exception:
        return default


def parse_utc_timestamp(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ)


def resolve_execution_params(cfg: dict[str, Any]) -> dict[str, Any]:
    exec_cfg = cfg_get(cfg, "execution", default={})
    if not isinstance(exec_cfg, dict):
        exec_cfg = {}
    entry_rule_raw = str(exec_cfg.get("entry_rule", "next_open")).strip().lower()
    entry_rule = (
        entry_rule_raw
        if entry_rule_raw in {"next_open", "same_close_if_before_cutoff"}
        else "next_open"
    )
    return {
        "use_acceptance_timestamp": bool(exec_cfg.get("use_acceptance_timestamp", True)),
        "entry_rule": entry_rule,
        "same_day_cutoff_et": parse_cutoff_time(exec_cfg.get("same_day_cutoff_et", "15:45")),
    }


def resolve_behavior_params(cfg: dict[str, Any]) -> dict[str, Any]:
    behavior_cfg = cfg_get(cfg, "behavior", default={})
    if not isinstance(behavior_cfg, dict):
        behavior_cfg = {}
    return {
        "routine_enabled": bool(behavior_cfg.get("routine_enabled", True)),
        "routine_min_events": max(1, int(behavior_cfg.get("routine_min_events", 4))),
        "routine_min_years": max(1, int(behavior_cfg.get("routine_min_years", 2))),
        "routine_month_concentration_min": float(
            behavior_cfg.get("routine_month_concentration_min", 0.60)
        ),
        "routine_penalty": float(behavior_cfg.get("routine_penalty", 0.75)),
        "opportunistic_boost": float(behavior_cfg.get("opportunistic_boost", 1.15)),
        "tenb5_penalty": float(behavior_cfg.get("tenb5_penalty", 0.85)),
        "filing_lag_penalty_per_bd": float(
            behavior_cfg.get("filing_lag_penalty_per_bd", 0.03)
        ),
        "filing_lag_penalty_floor": float(behavior_cfg.get("filing_lag_penalty_floor", 0.50)),
    }


def resolve_liquidity_params(cfg: dict[str, Any]) -> dict[str, Any]:
    liq_cfg = cfg_get(cfg, "liquidity", default={})
    if not isinstance(liq_cfg, dict):
        liq_cfg = {}
    return {
        "enabled": bool(liq_cfg.get("enabled", False)),
        "min_price": float(liq_cfg.get("min_price", 0.0)),
        "min_adv20_usd": float(liq_cfg.get("min_adv20_usd", 0.0)),
    }


def derive_tradable_fields(
    *,
    accepted_ts_utc: str | None,
    filing_date_sort: str | None,
    use_acceptance_timestamp: bool,
    entry_rule: str,
    same_day_cutoff_et: dt_time,
) -> tuple[str | None, str | None]:
    accepted_dt_utc = parse_utc_timestamp(accepted_ts_utc) if use_acceptance_timestamp else None
    if accepted_dt_utc is not None:
        accepted_et = accepted_dt_utc.astimezone(NY_TZ)
        accepted_date_et = accepted_et.date()
        if (
            entry_rule == "same_close_if_before_cutoff"
            and is_business_day(accepted_date_et)
            and accepted_et.time() <= same_day_cutoff_et
        ):
            return accepted_date_et.isoformat(), "same_close"
        return next_business_day(accepted_date_et, inclusive=False).isoformat(), "next_open"

    filing_dt = parse_iso_date(filing_date_sort)
    if filing_dt is None:
        return None, None
    if entry_rule == "same_close_if_before_cutoff":
        # No intraday acceptance timestamp available -> conservative next-open.
        return next_business_day(filing_dt, inclusive=False).isoformat(), "next_open"
    return next_business_day(filing_dt, inclusive=False).isoformat(), "next_open"


def apply_tradeability_fields(
    conn: sqlite3.Connection,
    as_of_date: date,
    cfg: dict[str, Any],
) -> None:
    exec_params = resolve_execution_params(cfg)
    behavior = resolve_behavior_params(cfg)
    liquidity = resolve_liquidity_params(cfg)

    owner_rows = conn.execute(
        """
        SELECT
            event_id,
            COALESCE(issuer_cik, '') AS issuer_cik,
            COALESCE(NULLIF(rptowner_cik, ''), NULLIF(rptowner_name, ''), 'UNKNOWN') AS owner_key,
            filing_date_sort
        FROM form4_events_tier1
        WHERE filing_date_sort IS NOT NULL
        """
    ).fetchall()

    owner_events: dict[tuple[str, str], list[date]] = {}
    for _, issuer_cik, owner_key, filing_date_sort in owner_rows:
        filing_dt = parse_iso_date(filing_date_sort)
        if filing_dt is None:
            continue
        key = (issuer_cik or "", owner_key or "UNKNOWN")
        owner_events.setdefault(key, []).append(filing_dt)

    routine_keys: set[tuple[str, str]] = set()
    if behavior["routine_enabled"]:
        for key, dates in owner_events.items():
            event_count = len(dates)
            if event_count < int(behavior["routine_min_events"]):
                continue
            years = {d.year for d in dates}
            if len(years) < int(behavior["routine_min_years"]):
                continue
            month_counter = Counter(d.month for d in dates)
            concentration = max(month_counter.values()) / max(event_count, 1)
            if concentration >= float(behavior["routine_month_concentration_min"]):
                routine_keys.add(key)

    updates: list[tuple] = []
    rows = conn.execute(
        """
        SELECT
            event_id,
            COALESCE(issuer_cik, '') AS issuer_cik,
            COALESCE(NULLIF(rptowner_cik, ''), NULLIF(rptowner_name, ''), 'UNKNOWN') AS owner_key,
            accepted_ts_utc,
            filing_date_sort,
            trans_date,
            signal_side,
            COALESCE(event_score, 0.0) AS event_score,
            COALESCE(sell_risk_score, 0.0) AS sell_risk_score,
            COALESCE(aff10b5one_flag, 0) AS aff10b5one_flag,
            close_px,
            adv20_usd
        FROM form4_events_tier1
        """
    ).fetchall()

    for (
        event_id,
        issuer_cik,
        owner_key,
        accepted_ts_utc,
        filing_date_sort,
        trans_date,
        signal_side,
        event_score,
        sell_risk_score,
        aff10b5one_flag,
        close_px,
        adv20_usd,
    ) in rows:
        tradable_date, tradable_session = derive_tradable_fields(
            accepted_ts_utc=accepted_ts_utc,
            filing_date_sort=filing_date_sort,
            use_acceptance_timestamp=bool(exec_params["use_acceptance_timestamp"]),
            entry_rule=str(exec_params["entry_rule"]),
            same_day_cutoff_et=exec_params["same_day_cutoff_et"],
        )
        filing_dt = parse_iso_date(filing_date_sort)
        trans_dt = parse_iso_date(trans_date)
        filing_lag_bd = (
            business_days_between(trans_dt, filing_dt)
            if (trans_dt is not None and filing_dt is not None)
            else None
        )

        key = (issuer_cik or "", owner_key or "UNKNOWN")
        routine_flag = 1 if key in routine_keys else 0
        opportunistic_flag = 1 if (routine_flag == 0 and int(aff10b5one_flag or 0) == 0) else 0

        if bool(liquidity["enabled"]):
            liquidity_pass = int(
                close_px is not None
                and float(close_px) >= float(liquidity["min_price"])
                and adv20_usd is not None
                and float(adv20_usd) >= float(liquidity["min_adv20_usd"])
            )
        else:
            liquidity_pass = 1

        if str(signal_side or "").upper() == "BUY":
            base_score = float(event_score or 0.0)
        elif str(signal_side or "").upper() == "SELL":
            base_score = -float(sell_risk_score or 0.0)
        else:
            base_score = 0.0

        score_mult = 1.0
        if int(aff10b5one_flag or 0) == 1:
            score_mult *= float(behavior["tenb5_penalty"])
        if routine_flag == 1:
            score_mult *= float(behavior["routine_penalty"])
        elif opportunistic_flag == 1:
            score_mult *= float(behavior["opportunistic_boost"])
        if filing_lag_bd is not None and filing_lag_bd > 0:
            lag_mult = max(
                float(behavior["filing_lag_penalty_floor"]),
                1.0 - (float(behavior["filing_lag_penalty_per_bd"]) * float(filing_lag_bd)),
            )
            score_mult *= lag_mult
        if liquidity_pass == 0:
            score_mult = 0.0

        tradeable_alpha_score = base_score * score_mult

        updates.append(
            (
                tradable_date,
                tradable_session,
                filing_lag_bd,
                routine_flag,
                opportunistic_flag,
                liquidity_pass,
                tradeable_alpha_score,
                int(event_id),
            )
        )

    conn.executemany(
        """
        UPDATE form4_events_tier1
        SET
            tradable_date = ?,
            tradable_session = ?,
            filing_lag_bd = ?,
            routine_flag = ?,
            opportunistic_flag = ?,
            liquidity_pass = ?,
            tradeable_alpha_score = ?
        WHERE event_id = ?
        """,
        updates,
    )


def compute_group_metrics(
    rows: list[tuple[int, date, str, float]],
    signal_side: str,
    as_of_date: date,
    scoring: dict[str, float | int],
) -> list[tuple[int, int, int, int, float, float, float, float, float, float, float]]:
    window_5 = int(scoring["cluster_window_5bd"])
    window_10 = int(scoring["cluster_window_10bd"])
    window_20 = int(scoring["cluster_window_20bd"])
    windows = (window_5, window_10, window_20)
    left = {window: 0 for window in windows}
    counters = {window: Counter() for window in windows}
    out: list[tuple[int, int, int, int, float, float, float, float, float, float, float]] = []
    two_plus = int(scoring["cluster_threshold_two_plus"])
    three_plus = int(scoring["cluster_threshold_three_plus"])
    decay_tau = float(scoring["decay_tau_business_days"])
    if decay_tau <= 0:
        raise ValueError("scoring.decay_tau_business_days must be > 0")

    for right, row in enumerate(rows):
        event_id, filing_dt, owner_key, raw_event_score = row
        for window in windows:
            start_dt = subtract_business_days(filing_dt, window)
            while left[window] < right and rows[left[window]][1] < start_dt:
                old_owner = rows[left[window]][2]
                counters[window][old_owner] -= 1
                if counters[window][old_owner] <= 0:
                    counters[window].pop(old_owner, None)
                left[window] += 1
            counters[window][owner_key] += 1

        cluster_5 = len(counters[window_5])
        cluster_10 = len(counters[window_10])
        cluster_20 = len(counters[window_20])

        if signal_side == "BUY":
            if cluster_10 >= three_plus:
                cluster_weight = float(scoring["buy_cluster_weight_3plus"])
            elif cluster_10 >= two_plus:
                cluster_weight = float(scoring["buy_cluster_weight_2plus"])
            else:
                cluster_weight = float(scoring["buy_cluster_weight_default"])
        elif signal_side == "SELL":
            if cluster_10 >= three_plus:
                cluster_weight = float(scoring["sell_cluster_weight_3plus"])
            elif cluster_10 >= two_plus:
                cluster_weight = float(scoring["sell_cluster_weight_2plus"])
            else:
                cluster_weight = float(scoring["sell_cluster_weight_default"])
        else:
            cluster_weight = float(scoring["cluster_weight_default_other_side"])

        event_score = raw_event_score * cluster_weight
        signed_event_score = event_score if signal_side == "BUY" else -event_score if signal_side == "SELL" else 0.0
        age_bd = business_days_between(filing_dt, as_of_date)
        decayed_score = event_score * math.exp(-(age_bd / decay_tau))
        buy_score = event_score if signal_side == "BUY" else 0.0
        sell_risk_score = event_score if signal_side == "SELL" else 0.0
        net_event_score = signed_event_score

        out.append(
            (
                event_id,
                cluster_5,
                cluster_10,
                cluster_20,
                cluster_weight,
                event_score,
                buy_score,
                sell_risk_score,
                signed_event_score,
                decayed_score,
                net_event_score,
            )
        )
    return out

def compute_event_metrics(
    conn: sqlite3.Connection,
    as_of_date: date,
    scoring: dict[str, float | int],
) -> None:
    conn.execute("DROP TABLE IF EXISTS _tier1_metrics")
    conn.execute(
        """
        CREATE TEMP TABLE _tier1_metrics (
            event_id                INTEGER PRIMARY KEY,
            cluster_insiders_5bd    INTEGER,
            cluster_insiders_10bd   INTEGER,
            cluster_insiders_20bd   INTEGER,
            cluster_weight          REAL,
            event_score             REAL,
            buy_score               REAL,
            sell_risk_score         REAL,
            signed_event_score      REAL,
            decayed_score           REAL,
            net_event_score         REAL
        )
        """
    )

    sql = """
    SELECT
        event_id,
        issuer_cik,
        signal_side,
        filing_date_sort,
        COALESCE(NULLIF(rptowner_cik, ''), NULLIF(rptowner_name, ''), 'UNKNOWN') AS owner_key,
        COALESCE(raw_event_score, 0.0) AS raw_event_score
    FROM form4_events_tier1
    WHERE filing_date_sort IS NOT NULL
    ORDER BY issuer_cik, signal_side, filing_date_sort, event_id
    """

    cur = conn.execute(sql)
    current_key: tuple[str, str] | None = None
    group_rows: list[tuple[int, date, str, float]] = []
    batch: list[tuple[int, int, int, int, float, float, float, float, float, float, float]] = []

    def flush_batch() -> None:
        nonlocal batch
        if not batch:
            return
        conn.executemany(
            """
            INSERT OR REPLACE INTO _tier1_metrics (
                event_id, cluster_insiders_5bd, cluster_insiders_10bd, cluster_insiders_20bd,
                cluster_weight, event_score, buy_score, sell_risk_score, signed_event_score,
                decayed_score, net_event_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        batch = []

    def flush_group(group_key: tuple[str, str] | None) -> None:
        nonlocal group_rows, batch
        if not group_rows or group_key is None:
            return
        _, signal_side = group_key
        batch.extend(compute_group_metrics(group_rows, signal_side, as_of_date, scoring))
        group_rows = []
        if len(batch) >= 50000:
            flush_batch()

    for event_id, issuer_cik, signal_side, filing_date_sort, owner_key, raw_event_score in cur:
        filing_dt = parse_iso_date(filing_date_sort)
        if filing_dt is None:
            continue
        key = (issuer_cik or "", signal_side or "")
        if current_key is None:
            current_key = key
        elif key != current_key:
            flush_group(current_key)
            current_key = key
        group_rows.append(
            (
                int(event_id),
                filing_dt,
                owner_key or "UNKNOWN",
                float(raw_event_score or 0.0),
            )
        )

    flush_group(current_key)
    flush_batch()

    conn.execute(
        """
        UPDATE form4_events_tier1
        SET
            cluster_insiders_5bd = COALESCE(
                (SELECT m.cluster_insiders_5bd FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                cluster_insiders_5bd
            ),
            cluster_insiders_10bd = COALESCE(
                (SELECT m.cluster_insiders_10bd FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                cluster_insiders_10bd
            ),
            cluster_insiders_20bd = COALESCE(
                (SELECT m.cluster_insiders_20bd FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                cluster_insiders_20bd
            ),
            cluster_weight = COALESCE(
                (SELECT m.cluster_weight FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                cluster_weight
            ),
            event_score = COALESCE(
                (SELECT m.event_score FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                event_score
            ),
            buy_score = COALESCE(
                (SELECT m.buy_score FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                buy_score
            ),
            sell_risk_score = COALESCE(
                (SELECT m.sell_risk_score FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                sell_risk_score
            ),
            signed_event_score = COALESCE(
                (SELECT m.signed_event_score FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                signed_event_score
            ),
            decayed_score = COALESCE(
                (SELECT m.decayed_score FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                decayed_score
            ),
            net_event_score = COALESCE(
                (SELECT m.net_event_score FROM _tier1_metrics m WHERE m.event_id = form4_events_tier1.event_id),
                net_event_score
            )
        """
    )
    conn.execute("DROP TABLE IF EXISTS _tier1_metrics")

def refresh_snapshot(
    conn: sqlite3.Connection,
    as_of_date: date,
    scoring: dict[str, float | int],
) -> None:
    as_of_date_iso = as_of_date.isoformat()
    start_20bd = subtract_business_days(
        as_of_date,
        int(scoring["snapshot_score_window_bd"]),
    ).isoformat()
    start_10bd = subtract_business_days(
        as_of_date,
        int(scoring["snapshot_distinct_window_bd"]),
    ).isoformat()

    conn.execute(
        "DELETE FROM stock_signal_snapshot_tier1 WHERE as_of_date = ?",
        (as_of_date_iso,),
    )
    snapshot_sql = """
        INSERT INTO stock_signal_snapshot_tier1 (
            as_of_date,
            issuer_cik,
            issuer_trading_symbol,
            buy_score_20bd,
            sell_score_20bd,
            net_score,
            long_rank_score,
            exit_risk_score,
            buy_cluster_5bd_max,
            buy_cluster_10bd_max,
            buy_cluster_20bd_max,
            sell_cluster_5bd_max,
            sell_cluster_10bd_max,
            sell_cluster_20bd_max,
            distinct_buy_insiders_10bd,
            distinct_sell_insiders_10bd,
            action_bucket
        )
        WITH params AS (
            SELECT
                ? AS as_of_date,
                ? AS start_20bd,
                ? AS start_10bd
        ),
        issuer_base AS (
            SELECT
                t.issuer_cik,
                MAX(t.issuer_trading_symbol) AS issuer_trading_symbol
            FROM form4_events_tier1 t
            WHERE COALESCE(t.issuer_cik, '') <> ''
            GROUP BY t.issuer_cik
        ),
        agg AS (
            SELECT
                t.issuer_cik,
                SUM(
                    CASE
                        WHEN t.signal_side = 'BUY'
                             AND t.filing_date_sort >= (SELECT start_20bd FROM params)
                        THEN COALESCE(t.decayed_score, 0.0)
                        ELSE 0.0
                    END
                ) AS buy_score_20bd,
                SUM(
                    CASE
                        WHEN t.signal_side = 'SELL'
                             AND t.filing_date_sort >= (SELECT start_20bd FROM params)
                        THEN COALESCE(t.decayed_score, 0.0)
                        ELSE 0.0
                    END
                ) AS sell_score_20bd,
                MAX(
                    CASE
                        WHEN t.signal_side = 'BUY'
                             AND t.filing_date_sort >= (SELECT start_20bd FROM params)
                        THEN COALESCE(t.cluster_insiders_5bd, 0)
                        ELSE 0
                    END
                ) AS buy_cluster_5bd_max,
                MAX(
                    CASE
                        WHEN t.signal_side = 'BUY'
                             AND t.filing_date_sort >= (SELECT start_20bd FROM params)
                        THEN COALESCE(t.cluster_insiders_10bd, 0)
                        ELSE 0
                    END
                ) AS buy_cluster_10bd_max,
                MAX(
                    CASE
                        WHEN t.signal_side = 'BUY'
                             AND t.filing_date_sort >= (SELECT start_20bd FROM params)
                        THEN COALESCE(t.cluster_insiders_20bd, 0)
                        ELSE 0
                    END
                ) AS buy_cluster_20bd_max,
                MAX(
                    CASE
                        WHEN t.signal_side = 'SELL'
                             AND t.filing_date_sort >= (SELECT start_20bd FROM params)
                        THEN COALESCE(t.cluster_insiders_5bd, 0)
                        ELSE 0
                    END
                ) AS sell_cluster_5bd_max,
                MAX(
                    CASE
                        WHEN t.signal_side = 'SELL'
                             AND t.filing_date_sort >= (SELECT start_20bd FROM params)
                        THEN COALESCE(t.cluster_insiders_10bd, 0)
                        ELSE 0
                    END
                ) AS sell_cluster_10bd_max,
                MAX(
                    CASE
                        WHEN t.signal_side = 'SELL'
                             AND t.filing_date_sort >= (SELECT start_20bd FROM params)
                        THEN COALESCE(t.cluster_insiders_20bd, 0)
                        ELSE 0
                    END
                ) AS sell_cluster_20bd_max
            FROM form4_events_tier1 t
            WHERE COALESCE(t.issuer_cik, '') <> ''
            GROUP BY t.issuer_cik
        ),
        distinct_10 AS (
            SELECT
                t.issuer_cik,
                COUNT(
                    DISTINCT CASE
                        WHEN t.signal_side = 'BUY'
                             AND t.filing_date_sort >= (SELECT start_10bd FROM params)
                        THEN COALESCE(NULLIF(t.rptowner_cik, ''), NULLIF(t.rptowner_name, ''), 'UNKNOWN')
                    END
                ) AS distinct_buy_insiders_10bd,
                COUNT(
                    DISTINCT CASE
                        WHEN t.signal_side = 'SELL'
                             AND t.filing_date_sort >= (SELECT start_10bd FROM params)
                        THEN COALESCE(NULLIF(t.rptowner_cik, ''), NULLIF(t.rptowner_name, ''), 'UNKNOWN')
                    END
                ) AS distinct_sell_insiders_10bd
            FROM form4_events_tier1 t
            WHERE COALESCE(t.issuer_cik, '') <> ''
            GROUP BY t.issuer_cik
        ),
        scored AS (
            SELECT
                (SELECT as_of_date FROM params) AS as_of_date,
                ib.issuer_cik,
                ib.issuer_trading_symbol,
                COALESCE(a.buy_score_20bd, 0.0) AS buy_score_20bd,
                COALESCE(a.sell_score_20bd, 0.0) AS sell_score_20bd,
                (
                    COALESCE(a.buy_score_20bd, 0.0)
                    - ({snapshot_net_sell_penalty} * COALESCE(a.sell_score_20bd, 0.0))
                ) AS net_score,
                (
                    (
                        COALESCE(a.buy_score_20bd, 0.0)
                        - ({snapshot_net_sell_penalty} * COALESCE(a.sell_score_20bd, 0.0))
                    )
                    + ({snapshot_long_rank_buy_distinct_weight} * MIN(COALESCE(d.distinct_buy_insiders_10bd, 0), {snapshot_long_rank_buy_distinct_cap}))
                    + ({snapshot_long_rank_buy_cluster_weight} * MIN(MAX(COALESCE(a.buy_cluster_10bd_max, 0) - 1, 0), {snapshot_long_rank_buy_cluster_cap}))
                ) AS long_rank_score,
                (
                    COALESCE(a.sell_score_20bd, 0.0)
                    - ({snapshot_exit_risk_buy_offset} * COALESCE(a.buy_score_20bd, 0.0))
                    + ({snapshot_exit_risk_sell_distinct_weight} * MIN(COALESCE(d.distinct_sell_insiders_10bd, 0), {snapshot_exit_risk_sell_distinct_cap}))
                    + ({snapshot_exit_risk_sell_cluster_weight} * MIN(MAX(COALESCE(a.sell_cluster_10bd_max, 0) - 1, 0), {snapshot_exit_risk_sell_cluster_cap}))
                ) AS exit_risk_score,
                COALESCE(a.buy_cluster_5bd_max, 0) AS buy_cluster_5bd_max,
                COALESCE(a.buy_cluster_10bd_max, 0) AS buy_cluster_10bd_max,
                COALESCE(a.buy_cluster_20bd_max, 0) AS buy_cluster_20bd_max,
                COALESCE(a.sell_cluster_5bd_max, 0) AS sell_cluster_5bd_max,
                COALESCE(a.sell_cluster_10bd_max, 0) AS sell_cluster_10bd_max,
                COALESCE(a.sell_cluster_20bd_max, 0) AS sell_cluster_20bd_max,
                COALESCE(d.distinct_buy_insiders_10bd, 0) AS distinct_buy_insiders_10bd,
                COALESCE(d.distinct_sell_insiders_10bd, 0) AS distinct_sell_insiders_10bd
            FROM issuer_base ib
            LEFT JOIN agg a
              ON a.issuer_cik = ib.issuer_cik
            LEFT JOIN distinct_10 d
              ON d.issuer_cik = ib.issuer_cik
        )
        SELECT
            as_of_date,
            issuer_cik,
            issuer_trading_symbol,
            buy_score_20bd,
            sell_score_20bd,
            net_score,
            long_rank_score,
            exit_risk_score,
            buy_cluster_5bd_max,
            buy_cluster_10bd_max,
            buy_cluster_20bd_max,
            sell_cluster_5bd_max,
            sell_cluster_10bd_max,
            sell_cluster_20bd_max,
            distinct_buy_insiders_10bd,
            distinct_sell_insiders_10bd,
            CASE
                WHEN buy_score_20bd >= {action_buy_tier1_buy_score_min}
                     AND buy_cluster_10bd_max >= {action_buy_tier1_buy_cluster10_min}
                     AND sell_score_20bd < {action_buy_tier1_sell_score_max_exclusive} THEN 'BUY_TIER_1'
                WHEN long_rank_score >= {action_buy_watch_long_rank_min}
                     AND buy_score_20bd > {action_buy_watch_buy_score_min_exclusive} THEN 'BUY_WATCH'
                WHEN exit_risk_score >= {action_avoid_trim_exit_risk_min}
                     AND sell_cluster_10bd_max >= {action_avoid_trim_sell_cluster10_min}
                     AND buy_score_20bd < {action_avoid_trim_buy_score_max_exclusive} THEN 'AVOID_TRIM'
                WHEN sell_score_20bd >= {action_sell_review_sell_score_min}
                     AND buy_score_20bd < {action_sell_review_buy_score_max_exclusive} THEN 'SELL_REVIEW'
                ELSE 'NEUTRAL'
            END AS action_bucket
        FROM scored
        ORDER BY long_rank_score DESC, buy_score_20bd DESC
        """
    conn.execute(
        snapshot_sql.format(**{k: sql_number(v) for k, v in scoring.items()}),
        (as_of_date_iso, start_20bd, start_10bd),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None, help="Path to SEC Form 4 YAML config.")
    parser.add_argument("--db-path", type=Path, default=None, help="SQLite DB path override.")
    parser.add_argument("--as-of-date", type=str, default=None, help="YYYY-MM-DD as-of date for decay/snapshot.")
    parser.add_argument(
        "--refresh-legacy-buy-table",
        dest="refresh_legacy_buy_table",
        action="store_true",
        help="Refresh form4_buy_events_v1 from the Tier1 table output.",
    )
    parser.add_argument(
        "--no-refresh-legacy-buy-table",
        dest="refresh_legacy_buy_table",
        action="store_false",
        help="Skip refreshing form4_buy_events_v1.",
    )
    parser.set_defaults(refresh_legacy_buy_table=None)
    args = parser.parse_args()

    _, cfg = load_sec_form4_config(args.config)
    db_path = Path(
        args.db_path
        if args.db_path is not None
        else cfg_get(cfg, "db_path", default=str(default_db_path()))
    )
    as_of_date_raw = (
        args.as_of_date
        if args.as_of_date is not None
        else cfg_get(cfg, "build", "as_of_date", default=None)
    )
    as_of_date = (
        datetime.strptime(str(as_of_date_raw), "%Y-%m-%d").date()
        if as_of_date_raw
        else date.today()
    )
    normalized_as_of_date = previous_or_same_business_day(as_of_date)
    if normalized_as_of_date != as_of_date:
        print(
            f"Adjusted weekend as_of_date {as_of_date.isoformat()} to business day {normalized_as_of_date.isoformat()}."
        )
        as_of_date = normalized_as_of_date
    refresh_legacy_buy_table = (
        args.refresh_legacy_buy_table
        if args.refresh_legacy_buy_table is not None
        else bool(cfg_get(cfg, "build", "refresh_legacy_buy_table", default=True))
    )
    scoring = resolve_scoring_params(cfg)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=120.0)
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=120000;")
        assert_required_tables(conn)
        ensure_source_schema(conn)
        ensure_log_function(conn)
        conn.executescript(SQL_CREATE_TIER1_TABLE)
        conn.executescript(SQL_CREATE_SNAPSHOT_TABLE)
        ensure_output_schema(conn)

        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM form4_events_tier1")
            conn.execute(render_tier1_sql(scoring))

            compute_event_metrics(conn, as_of_date, scoring)
            apply_tradeability_fields(conn, as_of_date, cfg)
            refresh_snapshot(conn, as_of_date, scoring)
            if refresh_legacy_buy_table:
                conn.execute(SQL_DELETE_LEGACY_BUYS)
                conn.execute(SQL_INSERT_LEGACY_BUYS)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        tier1_count = conn.execute("SELECT COUNT(*) FROM form4_events_tier1").fetchone()[0]
        buy_count = conn.execute("SELECT COUNT(*) FROM form4_buy_events_v1").fetchone()[0]
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM stock_signal_snapshot_tier1"
        ).fetchone()[0]
        max_filing = conn.execute(
            "SELECT MAX(filing_date_sort) FROM form4_events_tier1"
        ).fetchone()[0]
        print(
            "Built form4_events_tier1 with "
            f"{tier1_count:,} rows; refreshed form4_buy_events_v1 with {buy_count:,} rows; "
            f"built stock_signal_snapshot_tier1 with {snapshot_count:,} rows; "
            f"latest filing date {max_filing}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
