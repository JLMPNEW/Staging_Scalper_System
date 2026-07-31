from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

from technology.core.db import init_db
from technology.core.dedicated_parser.db_contract import (
    ensure_technology_parser_schema,
    validate_technology_parser_schema,
)
from technology.software_infrastructure.dedicated_parser_baseline import (
    load_applicability_rows,
    load_metric_registry,
    load_universe_members,
)
from technology.software_infrastructure.dedicated_parser_census import (
    build_metric_gap_census,
    build_source_scope_rows,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = (
    PROJECT_ROOT
    / "technology"
    / "software_infrastructure"
    / "data"
    / "software_infrastructure_specialized_metric_registry.yaml"
)
APPLICABILITY_PATH = (
    PROJECT_ROOT
    / "technology"
    / "software_infrastructure"
    / "data"
    / "software_infrastructure_metric_applicability.csv"
)


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    ensure_technology_parser_schema(conn)
    return conn


def seed_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            created_at, updated_at
        )
        VALUES ('sec_companyfacts_api', 'stage_4', 'SEC', 'api',
                'https://data.sec.gov', '2026-01-01', '2026-01-01')
        """
    )


def seed_company(conn: sqlite3.Connection, *, ticker: str = "TEST") -> None:
    conn.execute(
        """
        INSERT INTO dim_company(
            ticker, cik, company_name, sector, industry, subsector,
            country, currency, first_seen_at, updated_at
        )
        VALUES (?, '0000001234', 'Test Software', 'Technology', 'Software',
                'Software Infrastructure', 'US', 'USD', '2020-01-01', '2026-01-01')
        """,
        (ticker,),
    )
    company_id = int(
        conn.execute(
            "SELECT company_id FROM dim_company WHERE ticker = ?",
            (ticker,),
        ).fetchone()["company_id"]
    )
    conn.execute(
        """
        INSERT INTO dim_technology_taxonomy(
            company_id, ticker, model_family, subsector,
            calibration_cohort_id, calibration_cohort, updated_at
        )
        VALUES (?, ?, 'software_infrastructure', 'Software Infrastructure',
                'software_infra_cloud_data_devops_ai',
                'Cloud Data DevOps AI', '2026-01-01')
        """,
        (company_id, ticker),
    )
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_basis, start_date,
            membership_status, is_current_member, point_in_time_flag,
            created_at, updated_at
        )
        VALUES (?, ?, 'software_infrastructure', 'historical_research',
                '2020-01-01', 'active', 1, 1, '2026-01-01', '2026-01-01')
        """,
        (company_id, ticker),
    )


def seed_filing_and_fact(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO fact_sec_filing(
            ticker, cik, accession_number, source_id, form_type, filing_date,
            report_date, acceptance_datetime, primary_document,
            created_at, updated_at
        )
        VALUES (
            'TEST', '0000001234', '0000001234-24-000001',
            'sec_companyfacts_api', '10-K', '2024-02-15', '2023-12-31',
            '2024-02-15T16:01:02Z', 'test-20231231.htm',
            '2024-02-15', '2024-02-15'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fact_sec_xbrl_fact(
            fact_key, ticker, cik, taxonomy, concept, metric_name, unit,
            accession_number, source_id, form_type, filing_date, start_date,
            end_date, value, created_at, updated_at
        )
        VALUES (
            'test-rpo', 'TEST', '0000001234', 'us-gaap',
            'RevenueRemainingPerformanceObligation',
            'remaining_performance_obligation', 'USD',
            '0000001234-24-000001', 'sec_companyfacts_api', '10-K',
            '2024-02-15', NULL, '2023-12-31', 1000000.0,
            '2024-02-15', '2024-02-15'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fact_sec_xbrl_fact_raw(
            fact_key, ticker, cik, source_id, taxonomy, concept, unit, value,
            end_date, form_type, filing_date, accession_number,
            created_at, updated_at
        )
        VALUES (
            'test-arr', 'TEST', '0000001234', 'sec_companyfacts_api',
            'test', 'AnnualRecurringRevenue', 'USD', 750000.0,
            '2023-12-31', '10-K', '2024-02-15',
            '0000001234-24-000001', '2024-02-15', '2024-02-15'
        )
        """
    )


def test_schema_bridge_and_views_match_technology_columns() -> None:
    with connection() as conn:
        seed_source(conn)
        seed_company(conn)
        seed_filing_and_fact(conn)
        validate_technology_parser_schema(conn)
        filing = conn.execute(
            "SELECT * FROM sec_parser_filing_input WHERE ticker = 'TEST'"
        ).fetchone()
        fact = conn.execute(
            "SELECT * FROM sec_parser_financial_fact_input WHERE ticker = 'TEST'"
        ).fetchone()
        assert filing["accepted_at"] == "2024-02-15T16:01:02Z"
        assert fact["canonical_metric"] == "remaining_performance_obligation"
        assert fact["concept_name"] == "RevenueRemainingPerformanceObligation"
        assert fact["period_end"] == "2023-12-31"
        assert fact["accepted_at"] == "2024-02-15T16:01:02Z"
        legacy_object = conn.execute(
            "SELECT type FROM sqlite_master "
            "WHERE name = 'fact_sec_metric_disclosure_candidate'"
        ).fetchone()
        assert legacy_object["type"] == "view"
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_sec_metric_disclosure_candidate"
        ).fetchone()[0] == 0
        availability_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(feature_financial_metric_availability)"
            )
        }
        assert {
            "metric_value", "period_end", "source_id", "accession_number",
            "filing_date", "extraction_method", "provenance_json",
        } <= availability_columns


def test_registry_and_applicability_are_complete() -> None:
    registry = load_metric_registry(REGISTRY_PATH)
    rows = load_applicability_rows(APPLICABILITY_PATH, registry=registry)
    assert registry.model_family == "software_infrastructure"
    assert len(registry.metrics) == 12
    assert {row["metric_name"] for row in rows} == {
        metric.metric_name for metric in registry.metrics
    }
    assert {row["cohort_id"] for row in rows} == {
        "software_infra_cloud_data_devops_ai",
        "software_infra_security_identity_edge",
    }


def test_census_finds_existing_xbrl_and_parser_gap() -> None:
    registry = load_metric_registry(REGISTRY_PATH)
    with connection() as conn:
        seed_source(conn)
        seed_company(conn)
        seed_filing_and_fact(conn)
        members = load_universe_members(
            conn,
            history_start_date="2010-01-01",
            asof_date="2024-12-31",
        )
        summary, detail = build_metric_gap_census(
            conn,
            registry=registry,
            members=members,
            asof_date="2024-12-31",
        )
    summary_by_metric = {row["metric_name"]: row for row in summary}
    detail_by_metric = {row["metric_name"]: row for row in detail}
    assert summary_by_metric["remaining_performance_obligation"][
        "any_baseline_ticker_count"
    ] == 1
    assert detail_by_metric["annual_recurring_revenue"]["baseline_available_flag"] == 1
    assert detail_by_metric["net_revenue_retention"]["parser_candidate_flag"] == 1


def test_source_scope_is_hash_sealed_only_when_document_exists(
    tmp_path: Path,
) -> None:
    registry = load_metric_registry(REGISTRY_PATH)
    with connection() as conn:
        seed_source(conn)
        seed_company(conn)
        seed_filing_and_fact(conn)
        members = load_universe_members(
            conn,
            history_start_date="2010-01-01",
            asof_date="2024-12-31",
        )
        accession_dir = (
            tmp_path
            / "sec_archive_xbrl"
            / "CIK0000001234"
            / "000000123424000001"
        )
        accession_dir.mkdir(parents=True)
        document = accession_dir / "test-20231231.htm"
        document.write_text("<html>RPO</html>", encoding="utf-8")
        rows = build_source_scope_rows(
            conn,
            cache_dir=tmp_path,
            registry=registry,
            members=members,
            history_start_date="2010-01-01",
            asof_date="2024-12-31",
        )
    assert len(rows) == 1
    assert rows[0]["cache_status"] == "CACHED_HASHED"
    assert rows[0]["parser_ready_flag"] == 1
    assert len(rows[0]["content_sha256"]) == 64


def test_technology_parser_foundation_has_no_industrial_imports() -> None:
    paths = [
        PROJECT_ROOT / "technology" / "core" / "dedicated_parser" / "db_contract.py",
        PROJECT_ROOT
        / "technology"
        / "software_infrastructure"
        / "dedicated_parser_baseline.py",
        PROJECT_ROOT
        / "technology"
        / "software_infrastructure"
        / "dedicated_parser_census.py",
    ]
    imported_modules: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
    assert not any(module == "industrials" or module.startswith("industrials.") for module in imported_modules)


def test_technology_filing_inventory_covers_software_parser_forms() -> None:
    path = (
        PROJECT_ROOT
        / "technology"
        / "scripts"
        / "07_sync_technology_sec_fundamentals.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "FILING_FORMS"
            for target in node.targets
        )
    )
    filing_forms = set(ast.literal_eval(assignment.value))
    parser_only_forms = {
        "6-K", "6-K/A", "S-1", "S-1/A", "F-1", "F-1/A"
    }
    assert parser_only_forms <= filing_forms
