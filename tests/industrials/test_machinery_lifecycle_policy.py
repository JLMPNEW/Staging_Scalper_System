from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from industrials.machinery.lifecycle_policy import (
    ACCEPTED,
    COMMERCIAL_EMERGING,
    ESTABLISHED_OPERATING,
    HARD_EVENT_FIELDS,
    HARD_EVENT_HASH_FIELDS,
    LIFECYCLE_STATE_FIELDS,
    POLICY_VERSION,
    PRE_COMMERCIAL,
    REVENUE_POLICY_FIELDS,
    TRANSITION_FIELDS,
    TRANSITION_HASH_FIELDS,
    LifecyclePolicy,
    LifecycleThresholds,
    generate_lifecycle_candidates,
    parser_hard_event_candidates,
    record_sha256,
    resolve_lifecycle_state,
    validate_lifecycle_policy,
)


def _empty_policy(tmp_path: Path) -> LifecyclePolicy:
    paths = {
        "transitions": tmp_path / "transitions.csv",
        "revenue": tmp_path / "revenue.csv",
        "events": tmp_path / "events.csv",
    }
    for path, fields in (
        (paths["transitions"], TRANSITION_FIELDS),
        (paths["revenue"], REVENUE_POLICY_FIELDS),
        (paths["events"], HARD_EVENT_FIELDS),
    ):
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.DictWriter(handle, fieldnames=fields).writeheader()
    return LifecyclePolicy(
        policy_version=POLICY_VERSION,
        thresholds=LifecycleThresholds(),
        transitions=(),
        revenue_decisions=(),
        hard_events=(),
        transitions_path=paths["transitions"],
        revenue_policy_path=paths["revenue"],
        hard_events_path=paths["events"],
    )


def _transition(
    *,
    evidence_path: Path,
    valid_from: str = "2026-07-25",
) -> dict[str, str]:
    row = {
        "transition_id": "test_transition",
        "ticker": "TEST",
        "from_class": PRE_COMMERCIAL,
        "to_class": COMMERCIAL_EMERGING,
        "valid_from": valid_from,
        "evidence_asof": "2026-07-24",
        "evidence_artifact": str(evidence_path),
        "evidence_sha256": (
            __import__("hashlib").sha256(evidence_path.read_bytes()).hexdigest()
        ),
        "decision_status": ACCEPTED,
        "decision_reason": "reviewed",
        "reviewer": "analyst",
        "reviewed_at": "2026-07-25",
        "policy_version": POLICY_VERSION,
        "record_sha256": "",
    }
    row["record_sha256"] = record_sha256(
        row,
        fields=TRANSITION_HASH_FIELDS,
    )
    return row


def test_lifecycle_state_is_separate_from_calibration_cohort(
    tmp_path: Path,
) -> None:
    policy = _empty_policy(tmp_path)
    development = resolve_lifecycle_state(
        {
            "ticker": "TEST",
            "calibration_cohort": "development_stage_emerging_machinery",
            "development_stage": "development_stage",
            "capital_raise_dependence": 0.1,
            "cash_runway_years": 4.0,
            "diluted_shares_yoy_growth": 0.05,
            "avg_dollar_volume_60d": 20_000_000,
        },
        asof="2026-07-24",
        policy=policy,
    )
    assert set(LIFECYCLE_STATE_FIELDS) == set(development)
    assert development["lifecycle_class"] == PRE_COMMERCIAL
    assert development["lifecycle_investability_eligible_flag"] == "0"

    operating = resolve_lifecycle_state(
        {
            "ticker": "OPER",
            "calibration_cohort": "diversified_machinery",
            "development_stage": "operating",
        },
        asof="2026-07-24",
        policy=policy,
    )
    assert operating["lifecycle_class"] == ESTABLISHED_OPERATING
    assert operating["lifecycle_investability_eligible_flag"] == "1"


def test_ratified_transition_is_pit_and_hard_event_vetoes(
    tmp_path: Path,
) -> None:
    policy = _empty_policy(tmp_path)
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}\n", encoding="utf-8")
    transition = _transition(evidence_path=evidence)
    event = {
        "event_id": "test_event",
        "ticker": "TEST",
        "event_type": "going_concern",
        "valid_from": "2026-08-01",
        "valid_to": "",
        "evidence_artifact": str(evidence),
        "evidence_sha256": (
            __import__("hashlib").sha256(evidence.read_bytes()).hexdigest()
        ),
        "decision_status": ACCEPTED,
        "decision_reason": "reviewed",
        "reviewer": "analyst",
        "reviewed_at": "2026-08-01",
        "policy_version": POLICY_VERSION,
        "record_sha256": "",
    }
    event["record_sha256"] = record_sha256(
        event,
        fields=HARD_EVENT_HASH_FIELDS,
    )
    policy = LifecyclePolicy(
        **{
            **policy.__dict__,
            "transitions": (transition,),
            "hard_events": (event,),
        }
    )
    row = {
        "ticker": "TEST",
        "calibration_cohort": "development_stage_emerging_machinery",
        "development_stage": "development_stage",
        "capital_raise_dependence": 0.1,
        "cash_runway_years": 4.0,
        "diluted_shares_yoy_growth": 0.05,
        "avg_dollar_volume_60d": 20_000_000,
    }
    before = resolve_lifecycle_state(
        row,
        asof="2026-07-24",
        policy=policy,
    )
    assert before["lifecycle_class"] == PRE_COMMERCIAL
    promoted = resolve_lifecycle_state(
        row,
        asof="2026-07-25",
        policy=policy,
    )
    assert promoted["lifecycle_class"] == COMMERCIAL_EMERGING
    assert promoted["lifecycle_investability_eligible_flag"] == "1"
    vetoed = resolve_lifecycle_state(
        row,
        asof="2026-08-01",
        policy=policy,
    )
    assert vetoed["lifecycle_class"] == COMMERCIAL_EMERGING
    assert vetoed["lifecycle_hard_event_veto_flag"] == "1"
    assert vetoed["lifecycle_investability_eligible_flag"] == "0"


def test_validator_rejects_retroactive_ratification(tmp_path: Path) -> None:
    policy = _empty_policy(tmp_path)
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}\n", encoding="utf-8")
    transition = _transition(
        evidence_path=evidence,
        valid_from="2026-07-24",
    )
    policy = LifecyclePolicy(
        **{
            **policy.__dict__,
            "transitions": (transition,),
        }
    )
    result = validate_lifecycle_policy(policy)
    assert result["acceptance"] == "FAIL"
    assert "test_transition:valid_from_before_review" in result["issues"]


def test_validator_binds_transition_to_candidate_evidence(
    tmp_path: Path,
) -> None:
    policy = _empty_policy(tmp_path)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "artifact_family": (
                    "machinery_lifecycle_candidate_evidence"
                ),
                "policy_version": POLICY_VERSION,
                "candidate": {
                    "ticker": "OTHER",
                    "current_lifecycle_class": PRE_COMMERCIAL,
                    "suggested_lifecycle_class": COMMERCIAL_EMERGING,
                    "asof_date": "2026-07-24",
                    "candidate_status": "REVIEW_REQUIRED",
                },
            }
        ),
        encoding="utf-8",
    )
    transition = _transition(evidence_path=evidence)
    policy = LifecyclePolicy(
        **{
            **policy.__dict__,
            "transitions": (transition,),
        }
    )
    result = validate_lifecycle_policy(policy)
    assert result["acceptance"] == "FAIL"
    assert "test_transition:evidence_ticker_mismatch" in result["issues"]


def _candidate_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dim_company (
            company_id INTEGER PRIMARY KEY,
            company_name TEXT
        );
        CREATE TABLE dim_universe_membership (
            ticker TEXT,
            model_family TEXT,
            company_id INTEGER,
            membership_source_id TEXT,
            membership_basis TEXT,
            start_date TEXT,
            end_date TEXT,
            membership_status TEXT,
            confidence REAL
        );
        CREATE TABLE dim_industrials_taxonomy (
            ticker TEXT,
            model_family TEXT,
            calibration_cohort_id TEXT,
            development_stage TEXT
        );
        CREATE TABLE feature_market_technical (
            ticker TEXT,
            model_family TEXT,
            asof_date TEXT,
            avg_dollar_volume_60d REAL
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT,
            model_family TEXT,
            asof_date TEXT,
            fiscal_period_end TEXT,
            revenue_ttm_usd REAL,
            financial_confidence REAL,
            data_quality_status TEXT,
            capital_raise_dependence REAL,
            cash_runway_years REAL,
            diluted_shares_yoy_growth REAL,
            canonical_quality TEXT,
            financial_fallback_status TEXT,
            operating_cash_flow_ttm_usd REAL,
            accession_number TEXT,
            form_type TEXT
        );
        CREATE TABLE fact_financial_statement_canonical (
            ticker TEXT,
            model_family TEXT,
            canonical_metric TEXT,
            period_start TEXT,
            period_end TEXT,
            filing_date TEXT,
            accepted_at TEXT,
            accession_number TEXT,
            form_type TEXT,
            fiscal_period TEXT,
            taxonomy TEXT,
            concept_name TEXT,
            unit TEXT,
            value REAL,
            value_usd REAL,
            source_priority INTEGER,
            canonical_quality TEXT,
            source_id TEXT
        );
        CREATE TABLE sec_parser_metric_evidence_shadow (
            evidence_key TEXT,
            run_id INTEGER,
            model_family TEXT,
            ticker TEXT,
            accession_number TEXT,
            filing_date TEXT,
            accepted_at TEXT,
            period_end TEXT,
            metric_name TEXT,
            candidate_value REAL,
            confidence REAL,
            candidate_status TEXT,
            status_reason TEXT,
            evidence_text TEXT,
            source_document TEXT,
            extraction_method TEXT,
            provenance_json TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO dim_company VALUES (1, 'Test Machinery')"
    )
    conn.execute(
        """
        INSERT INTO dim_universe_membership
        VALUES ('TEST','machinery',1,'seed','pit','2020-01-01',NULL,'active',1)
        """
    )
    conn.execute(
        """
        INSERT INTO dim_industrials_taxonomy
        VALUES (
            'TEST','machinery',
            'development_stage_emerging_machinery','development_stage'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO feature_market_technical
        VALUES ('TEST','machinery','2026-07-24',20000000)
        """
    )
    conn.execute(
        """
        INSERT INTO feature_financial_statement
        VALUES (
            'TEST','machinery','2026-07-24','2025-12-31',80000000,0.9,
            'complete',0.1,4.0,0.05,'','',-1000000,'current','10-K'
        )
        """
    )
    quarters = (
        ("2023-01-01", "2023-03-31", "Q1"),
        ("2023-04-01", "2023-06-30", "Q2"),
        ("2023-07-01", "2023-09-30", "Q3"),
        ("2023-10-01", "2023-12-31", "Q4"),
        ("2024-01-01", "2024-03-31", "Q1"),
        ("2024-04-01", "2024-06-30", "Q2"),
        ("2024-07-01", "2024-09-30", "Q3"),
        ("2024-10-01", "2024-12-31", "Q4"),
        ("2025-01-01", "2025-03-31", "Q1"),
        ("2025-04-01", "2025-06-30", "Q2"),
        ("2025-07-01", "2025-09-30", "Q3"),
        ("2025-10-01", "2025-12-31", "Q4"),
    )
    for index, (period_start, period_end, fiscal_period) in enumerate(
        quarters,
        start=1,
    ):
        filing_date = (
            f"{int(period_end[:4]) + (1 if period_end[5:7] == '12' else 0)}"
            f"-{'02-15' if period_end[5:7] == '12' else '05-15'}"
        )
        conn.execute(
            """
            INSERT INTO fact_financial_statement_canonical
            VALUES (
                'TEST','machinery','revenue',?,?,?,?,?,'10-Q',?,
                'us-gaap','Revenue','USD',20000000,20000000,10,
                'mapped_xbrl','sec_companyfacts'
            )
            """,
            (
                period_start,
                period_end,
                filing_date,
                filing_date,
                f"test-{index}",
                fiscal_period,
            ),
        )
    conn.commit()
    return conn


def test_generator_uses_canonical_quarters_not_compacted_features(
    tmp_path: Path,
) -> None:
    policy = _empty_policy(tmp_path)
    conn = _candidate_connection()
    try:
        candidates = generate_lifecycle_candidates(
            conn,
            asof="2026-07-24",
            policy=policy,
        )
    finally:
        conn.close()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert int(candidate["established_revenue_quarter_streak"]) >= 8
    assert candidate["suggested_lifecycle_class"] == ESTABLISHED_OPERATING
    assert candidate["candidate_status"] == "BLOCKED_PENDING_REVIEW"
    assert candidate["candidate_reasons"] == "commercial_revenue_not_ratified"

def test_generator_preserves_validated_zero_revenue_evidence(
    tmp_path: Path,
) -> None:
    policy = _empty_policy(tmp_path)
    conn = _candidate_connection()
    try:
        conn.execute("DELETE FROM fact_financial_statement_canonical")
        conn.execute(
            """
            UPDATE feature_financial_statement
            SET revenue_ttm_usd = 0,
                financial_confidence = 0.50,
                canonical_quality = (
                    'development_stage_zero_revenue_validated_by_negative_operating_cash_flow'
                ),
                financial_fallback_status = (
                    'verified_pre_revenue_component_limited'
                ),
                operating_cash_flow_ttm_usd = -1000000
            WHERE ticker = 'TEST'
            """
        )
        conn.commit()
        candidates = generate_lifecycle_candidates(
            conn,
            asof="2026-07-24",
            policy=policy,
        )
    finally:
        conn.close()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["current_lifecycle_class"] == PRE_COMMERCIAL
    assert candidate["suggested_lifecycle_class"] == PRE_COMMERCIAL
    assert candidate["candidate_status"] == "NO_CHANGE"
    assert candidate["candidate_reasons"] == (
        "validated_precommercial_zero_revenue"
    )
    assert float(candidate["latest_revenue_ttm_usd"]) == 0.0
    assert candidate["commercial_revenue_quarter_streak"] == "0"
    evidence = json.loads(candidate["evidence_periods_json"])
    assert evidence[0]["evidence_basis"] == (
        "validated_zero_revenue_negative_operating_cash_flow"
    )


def test_parser_hard_event_candidate_is_pit_and_uses_latest_fact() -> None:
    conn = _candidate_connection()
    try:
        rows = (
            (
                "old",
                1,
                "2026-05-01",
                "20260501120000",
                1.0,
            ),
            (
                "latest",
                2,
                "2026-05-14",
                "20260514120000",
                1.0,
            ),
            (
                "future",
                3,
                "2026-08-01",
                "20260801120000",
                0.0,
            ),
        )
        for evidence_key, run_id, filing_date, accepted_at, value in rows:
            conn.execute(
                """
                INSERT INTO sec_parser_metric_evidence_shadow
                VALUES (
                    ?,?,'machinery','XOS','accession',?,?,?,
                    'going_concern_flag',?,0.97,'ACCEPTED','explicit_clause',
                    'substantial doubt','filing.htm','semantic_text','{}',
                    '2026-07-30T00:00:00Z'
                )
                """,
                (
                    evidence_key,
                    run_id,
                    filing_date,
                    accepted_at,
                    filing_date,
                    value,
                ),
            )
        conn.commit()
        candidates = parser_hard_event_candidates(
            conn,
            asof="2026-07-24",
        )
    finally:
        conn.close()
    assert len(candidates) == 1
    assert candidates[0]["evidence_key"] == "latest"

