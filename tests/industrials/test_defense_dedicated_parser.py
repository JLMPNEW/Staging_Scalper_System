from __future__ import annotations

import hashlib
import json
import runpy
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from dedicated_parser.contracts import (
    DocumentRef,
    FilingRef,
    MetricRequest,
    NormalizedFact,
    WorkItem,
)
from industrials.defense.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    extract_metric_evidence,
    get_registry,
    map_normalized_facts,
    select_tickers,
)
from dedicated_parser.storage import connect_database, utc_now
from dedicated_parser.recovery import (
    _baseline_rows,
    _freshness_fallback_rows,
)


def _filing() -> FilingRef:
    return FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-05-01",
        accepted_at="2026-05-01T12:00:00Z",
        report_date="2026-03-31",
        primary_document="test.htm",
        source_id="sec_submissions",
        company_currency="USD",
    )


def _work_item(
    *,
    documents: tuple[DocumentRef, ...] = (),
) -> WorkItem:
    return WorkItem(
        model_family="defense",
        adapter_path=("industrials.defense.dedicated_parser_adapter:extract_metric_evidence"),
        adapter_version=ADAPTER_VERSION,
        filing=_filing(),
        documents=documents,
        requested_metrics=tuple(
            MetricRequest(metric)
            for metric in (
                "orders",
                "funded_backlog",
                "reported_backlog",
                "remaining_performance_obligation",
                "rpo_current",
            )
        ),
    )


def _fact(
    *,
    concept_name: str,
    value: float,
    scope: str = "consolidated",
    taxonomy: str = "us-gaap",
    period_start: str = "",
    metadata: str = "{}",
) -> NormalizedFact:
    return NormalizedFact(
        taxonomy=taxonomy,
        concept_name=concept_name,
        value_text=str(value),
        numeric_value=value,
        unit="USD",
        period_start=period_start,
        period_end="2026-03-31",
        context_id="ctx",
        dimensions_json=("{}" if scope == "consolidated" else '{"segment":"x"}'),
        scope=scope,
        source_document="test.htm",
        provider="test",
        concept_metadata_json=metadata,
    )


def test_defense_event_document_selection_prefers_research_exhibits() -> None:
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    select_documents = namespace["archive_document_candidates"]
    index_payload = {
        "directory": {
            "item": [
                {"name": "issuer-20260724.htm"},
                {"name": "issuer-20260724_htm.xml"},
                {"name": "ex99-1_earnings_release.htm"},
                {"name": "unrelated.xml"},
            ]
        }
    }

    targeted = select_documents(
        index_payload,
        primary_document="issuer-20260724.htm",
        max_documents=0,
        research_targeted=True,
        event_filing=True,
        event_exhibits_only=True,
    )
    default = select_documents(
        index_payload,
        primary_document="issuer-20260724.htm",
        max_documents=0,
        research_targeted=True,
        event_filing=True,
    )

    assert targeted == ["ex99-1_earnings_release.htm"]
    assert default == [
        "issuer-20260724.htm",
        "ex99-1_earnings_release.htm",
        "issuer-20260724_htm.xml",
        "unrelated.xml",
    ]


def test_defense_event_document_selection_falls_back_to_primary() -> None:
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    selected = namespace["archive_document_candidates"](
        {
            "directory": {
                "item": [
                    {"name": "issuer-20260724.htm"},
                    {"name": "issuer-20260724_htm.xml"},
                    {
                        "name": "material_contract.pdf",
                        "type": "EX-10.1",
                    },
                ]
            }
        },
        primary_document="issuer-20260724.htm",
        max_documents=0,
        include_pdf=True,
        research_targeted=True,
        event_filing=True,
        event_exhibits_only=True,
    )

    assert selected == ["issuer-20260724.htm"]


def test_defense_event_document_selection_uses_exhibit_metadata() -> None:
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    selected = namespace["archive_document_candidates"](
        {
            "directory": {
                "item": [
                    {"name": "issuer-20260724.htm"},
                    {
                        "name": "attachment.pdf",
                        "type": "EX-99.1",
                        "description": "Earnings release",
                    },
                ]
            }
        },
        primary_document="issuer-20260724.htm",
        max_documents=0,
        include_pdf=True,
        research_targeted=True,
        event_filing=True,
        event_exhibits_only=True,
    )

    assert selected == ["attachment.pdf"]


def test_defense_registry_has_isolated_production_contract() -> None:
    registry = get_registry()
    assert registry.model_family == "defense"
    assert {request.metric_name for request in registry.source_metrics} == {
        "orders",
        "funded_backlog",
        "reported_backlog",
        "remaining_performance_obligation",
        "rpo_current",
    }
    assert set(registry.production_mappings) == {request.metric_name for request in registry.source_metrics}
    assert "8-K" in registry.supported_forms
    assert Path(registry.review_policy_path).parts[-3:] == (
        "defense",
        "review_policies",
        "dedicated_parser_review_policy.csv",
    )


def test_defense_selector_includes_current_and_exited_members() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE dim_universe_membership(
            ticker TEXT, model_family TEXT, start_date TEXT, end_date TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO dim_universe_membership VALUES (?, ?, ?, ?)",
        [
            ("ACTIVE", "defense", "2019-01-01", None),
            ("DEAD-DEL2020", "defense", "2018-01-01", "2020-01-01"),
            ("FUTURE", "defense", "2027-01-01", None),
            ("OTHER", "machinery", "2018-01-01", None),
        ],
    )
    assert select_tickers(conn, "2026-07-24") == [
        "ACTIVE",
        "DEAD-DEL2020",
    ]


def test_standard_consolidated_rpo_is_accepted_and_segment_is_rejected() -> None:
    evidence = map_normalized_facts(
        _work_item(),
        (
            _fact(
                concept_name="RevenueRemainingPerformanceObligation",
                value=500_000_000.0,
            ),
            _fact(
                concept_name="RevenueRemainingPerformanceObligation",
                value=200_000_000.0,
                scope="segment",
            ),
        ),
    )
    assert len(evidence) == 2
    accepted = [row for row in evidence if row.status == "ACCEPTED"]
    rejected = [row for row in evidence if row.status == "REJECTED_POLICY"]
    assert len(accepted) == 1
    assert accepted[0].metric_name == "remaining_performance_obligation"
    assert accepted[0].value == 500_000_000.0
    assert len(rejected) == 1
    assert rejected[0].reason == "dimensional_or_segment_fact_not_consolidated"


def test_timing_percentage_derives_current_rpo_from_unique_total() -> None:
    metadata = '{"namespace_uri":"https://fasb.org/us-gaap/2026","is_extension":false}'
    axis = "us-gaap:RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionStartDateAxis"
    total = _fact(
        concept_name="RevenueRemainingPerformanceObligation",
        value=500_000_000.0,
        metadata=metadata,
    )
    percentage = NormalizedFact(
        taxonomy="us-gaap",
        concept_name="RevenueRemainingPerformanceObligationPercentage",
        value_text="0.60",
        numeric_value=0.60,
        unit="pure",
        period_start="",
        period_end="2026-03-31",
        context_id="timing-percentage",
        dimensions_json=f'{{"{axis}":"2026-04-01"}}',
        scope="dimensional",
        source_document="test.htm",
        provider="test",
        concept_metadata_json=metadata,
    )
    evidence = map_normalized_facts(
        _work_item(),
        (total, percentage),
    )
    current = next(row for row in evidence if row.metric_name == "rpo_current")
    assert current.status == "ACCEPTED"
    assert current.value == 300_000_000.0
    assert current.reason == "standard_timing_percentage_times_consolidated_total_rpo"
    assert current.provenance["explicit_percentage"] == 0.60
    assert current.provenance["timing_delta_days"] == 1


def test_timing_schedule_derives_only_strict_twelve_month_bucket() -> None:
    metadata = '{"namespace_uri":"https://fasb.org/us-gaap/2026","is_extension":false}'
    axis = "us-gaap:RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionStartDateAxis"

    def timing_fact(member: str, value: float) -> NormalizedFact:
        return NormalizedFact(
            taxonomy="us-gaap",
            concept_name="RevenueRemainingPerformanceObligation",
            value_text=str(value),
            numeric_value=value,
            unit="USD",
            period_start="",
            period_end="2026-03-31",
            context_id=f"timing-{member}",
            dimensions_json=f'{{"{axis}":"{member}"}}',
            scope="dimensional",
            source_document="test.htm",
            provider="test",
            concept_metadata_json=metadata,
        )

    evidence = map_normalized_facts(
        _work_item(),
        (
            timing_fact("2026-04-01", 300_000_000.0),
            timing_fact("2027-04-01", 200_000_000.0),
        ),
    )
    accepted = {row.metric_name: row for row in evidence if row.status == "ACCEPTED"}
    assert accepted["remaining_performance_obligation"].value == (500_000_000.0)
    assert accepted["rpo_current"].value == 300_000_000.0

    invalid = map_normalized_facts(
        _work_item(),
        (
            timing_fact("2025-04-01", 300_000_000.0),
            timing_fact("2026-04-01", 200_000_000.0),
        ),
    )
    current = next(row for row in invalid if row.metric_name == "rpo_current")
    assert current.status == "REJECTED_POLICY"
    assert current.reason == "timing_dimension_current_bucket_not_twelve_months"


def test_timing_percentage_with_segment_dimension_is_not_accepted() -> None:
    metadata = '{"namespace_uri":"https://fasb.org/us-gaap/2026","is_extension":false}'
    axis = "us-gaap:RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionStartDateAxis"
    percentage = NormalizedFact(
        taxonomy="us-gaap",
        concept_name="RevenueRemainingPerformanceObligationPercentage",
        value_text="0.60",
        numeric_value=0.60,
        unit="pure",
        period_start="",
        period_end="2026-03-31",
        context_id="timing-segment-percentage",
        dimensions_json=(f'{{"{axis}":"2026-04-01","us-gaap:StatementBusinessSegmentsAxis":"test:DefenseMember"}}'),
        scope="dimensional",
        source_document="test.htm",
        provider="test",
        concept_metadata_json=metadata,
    )
    evidence = map_normalized_facts(
        _work_item(),
        (
            _fact(
                concept_name="RevenueRemainingPerformanceObligation",
                value=500_000_000.0,
                metadata=metadata,
            ),
            percentage,
        ),
    )
    assert not any(row.metric_name == "rpo_current" and row.status == "ACCEPTED" for row in evidence)


def test_rpo_amount_requires_monetary_unit() -> None:
    metadata = '{"namespace_uri":"https://fasb.org/us-gaap/2026","is_extension":false}'
    evidence = map_normalized_facts(
        _work_item(),
        (
            NormalizedFact(
                taxonomy="us-gaap",
                concept_name="RevenueRemainingPerformanceObligationCurrent",
                value_text="0.70",
                numeric_value=0.70,
                unit="pure",
                period_start="",
                period_end="2026-03-31",
                context_id="unitless-current-rpo",
                dimensions_json="{}",
                scope="consolidated",
                source_document="test.htm",
                provider="test",
                concept_metadata_json=metadata,
            ),
        ),
    )
    assert len(evidence) == 1
    assert evidence[0].status == "REJECTED_POLICY"
    assert evidence[0].reason == "rpo_amount_requires_monetary_unit"


def test_metric_freshness_fallback_is_bounded_and_pit_valid() -> None:
    rows = [
        {
            "period_start": "",
            "period_end": "2025-12-31",
            "filing_date": "2026-03-01",
            "accepted_at": "2026-03-01T12:00:00Z",
        },
        {
            "period_start": "",
            "period_end": "2025-09-30",
            "filing_date": "2025-11-01",
            "accepted_at": "2025-11-01T12:00:00Z",
        },
        {
            "period_start": "",
            "period_end": "2026-03-31",
            "filing_date": "2025-11-01",
            "accepted_at": "2025-11-01T12:00:00Z",
        },
    ]
    selected, age_days = _freshness_fallback_rows(
        rows,
        metric_name="reported_backlog",
        asof_date="2026-07-24",
        max_age_days=457,
    )
    assert [row["period_end"] for row in selected] == ["2025-12-31"]
    assert age_days == 205

    orders, _ = _freshness_fallback_rows(
        [
            {
                "period_start": "2025-10-01",
                "period_end": "2025-12-31",
                "filing_date": "2026-03-01",
                "accepted_at": "2026-03-01T12:00:00Z",
            }
        ],
        metric_name="orders",
        asof_date="2026-07-24",
        max_age_days=457,
    )
    assert orders == []


def test_extension_orders_require_duration_and_exact_semantics() -> None:
    evidence = map_normalized_facts(
        _work_item(),
        (
            _fact(
                concept_name="TotalBookings",
                value=300_000_000.0,
                taxonomy="test",
                period_start="2026-01-01",
            ),
            _fact(
                concept_name="TotalBookings",
                value=400_000_000.0,
                taxonomy="test",
            ),
        ),
    )
    by_value = {row.value: row for row in evidence}
    assert by_value[300_000_000.0].status == "ACCEPTED"
    assert by_value[400_000_000.0].status == "REVIEW_REQUIRED"
    assert by_value[400_000_000.0].reason == "metric_period_type_mismatch"


def test_prose_policy_rejects_contract_ceiling_and_accepts_explicit_total(
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "test.htm"
    document_path.write_text(
        """
        <html>
          <p>As of March 31, 2026, our total backlog was $1.2 billion.</p>
          <p>The total contract backlog ceiling was $9.0 billion
             as of March 31, 2026.</p>
        </html>
        """,
        encoding="utf-8",
    )
    payload = document_path.read_bytes()
    document = DocumentRef(
        name=document_path.name,
        path=str(document_path),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        file_size=len(payload),
        modified_ns=document_path.stat().st_mtime_ns,
        is_primary=True,
    )
    evidence = extract_metric_evidence(_work_item(documents=(document,)))
    accepted = [row for row in evidence if row.metric_name == "reported_backlog" and row.status == "ACCEPTED"]
    rejected = [row for row in evidence if row.status == "REJECTED_POLICY"]
    assert [(row.value, row.period_end) for row in accepted] == [(1_200_000_000.0, "2026-03-31")]
    assert any("ceiling" in row.reason for row in rejected)


def test_prose_policy_rejects_transaction_target_backlog(
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "transaction-target.htm"
    document_path.write_text(
        """
        <html>
          <p>
            Amounts are presented for the last twelve months ended
            September 30, 2025 on a combined basis. As of September 30,
            2025, Lanteris had a total backlog of $685 million. Metrics
            based on data available to Intuitive Machines have not been
            audited by Intuitive Machines or its auditors and are subject
            to change in connection with the closing of the transaction.
          </p>
        </html>
        """,
        encoding="utf-8",
    )
    payload = document_path.read_bytes()
    document = DocumentRef(
        name=document_path.name,
        path=str(document_path),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        file_size=len(payload),
        modified_ns=document_path.stat().st_mtime_ns,
        is_primary=True,
    )

    evidence = extract_metric_evidence(_work_item(documents=(document,)))

    assert len(evidence) == 1
    assert evidence[0].metric_name == "reported_backlog"
    assert evidence[0].status == "REJECTED_POLICY"
    assert evidence[0].reason == "transaction_target_or_pro_forma_value_not_issuer_consolidated"


def test_comparison_requires_explicit_historical_adapter_version() -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08f_compare_defense_specialized_metrics.py"
        )
    )

    args = script["parse_args"](
        [
            "--asof",
            "2026-07-24",
            "--run-id",
            "50",
            "--expected-adapter-version",
            "defense_specialized_metrics_v1.2",
        ]
    )

    assert args.expected_adapter_version == "defense_specialized_metrics_v1.2"


def test_defense_shadow_defaults_to_pair_level_adjudication() -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08d_run_defense_dedicated_parser_shadow.py"
        )
    )

    args = script["parse_args"]([])
    explicit = script["parse_args"](["--write-evidence-adjudication-skeleton"])

    assert args.write_evidence_adjudication_skeleton is False
    assert explicit.write_evidence_adjudication_skeleton is True


def test_full_universe_comparison_never_drops_historical_tickers(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08f_compare_defense_specialized_metrics.py"
        )
    )
    db_path = tmp_path / "comparison.sqlite"
    with connect_database(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE dim_universe_membership (
                ticker TEXT, model_family TEXT, start_date TEXT, end_date TEXT
            );
            CREATE TABLE dim_company (
                ticker TEXT, company_name TEXT, cik TEXT
            );
            CREATE TABLE dim_industrials_taxonomy (
                ticker TEXT, model_family TEXT, calibration_cohort TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO dim_universe_membership VALUES (?, 'defense', ?, ?)",
            [
                ("ACTIVE", "2019-01-01", None),
                ("DEAD-DEL2020", "2018-01-01", "2020-01-01"),
            ],
        )
        conn.executemany(
            "INSERT INTO dim_company VALUES (?, ?, ?)",
            [
                ("ACTIVE", "Active Defense", "0000000001"),
                ("DEAD-DEL2020", "Historical Defense", "0000000002"),
            ],
        )
        conn.executemany(
            "INSERT INTO dim_industrials_taxonomy VALUES (?, 'defense', ?)",
            [
                ("ACTIVE", "defense_primes_and_services"),
                ("DEAD-DEL2020", "defense_primes_and_services"),
            ],
        )
        now = utc_now()
        conn.execute(
            """
            INSERT INTO sec_parser_run(
                run_id, model_family, asof_date, parser_release,
                adapter_version, mode, worker_count, started_at, completed_at,
                status, planned_work_count, completed_work_count,
                failed_work_count
            )
            VALUES (
                1, 'defense', '2026-07-24', 'test',
                ?, 'shadow', 1, ?, ?, 'COMPLETED', 2, 2, 0
            )
            """,
            (ADAPTER_VERSION, now, now),
        )
        metrics = tuple(request.metric_name for request in get_registry().source_metrics)
        rows = []
        for ticker in ("ACTIVE", "DEAD-DEL2020"):
            for metric in metrics:
                rows.append(
                    (
                        1,
                        "defense",
                        ticker,
                        metric,
                        "2026-07-24",
                        "NOT_DISCLOSED",
                        None,
                        "2026-03-31",
                        "NOT_FOUND_IN_SEARCHED_DOCUMENTS",
                        "NOT_DISCLOSED",
                        0,
                        0,
                        0,
                        0,
                        0,
                        1,
                        1,
                        0,
                        0,
                        "[]",
                        "no_matching_fact_or_disclosure_candidate_found",
                        now,
                    )
                )
        conn.executemany(
            """
            INSERT INTO sec_parser_recovery_assessment(
                run_id, model_family, ticker, metric_name, asof_date,
                baseline_status, baseline_value, anchor_period_end,
                recovery_class, predicted_status, accepted_current_count,
                accepted_historical_count, review_required_count,
                rejected_count, parser_failure_count, searched_filing_count,
                searched_document_count, failed_filing_count,
                missing_cache_filing_count, evidence_keys_json,
                status_reason, created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?
            )
            """,
            rows,
        )
        comparison, summary = script["build_comparison"](
            conn,
            run_id=1,
            asof_date="2026-07-24",
            metric_names=metrics,
        )
        assert len(comparison) == 10
        assert summary["ticker_count"] == 2
        assert summary["active_ticker_count"] == 1
        assert summary["historical_ticker_count"] == 1
        assert summary["missing_assessment_pair_count"] == 0
        assert summary["acceptance"] == "PASS"
        assert summary["metric_coverage"]["orders"] == {
            "denominator": 2,
            "denominator_type": "full_universe_raw",
            "baseline_covered": 0,
            "shadow_covered": 0,
            "net_coverage_delta": 0,
            "active_shadow_covered": 0,
            "historical_shadow_covered": 0,
            "shadow_covered_match_mode_counts": {},
        }

        conn.execute(
            """
            DELETE FROM sec_parser_recovery_assessment
            WHERE ticker = 'DEAD-DEL2020' AND metric_name = 'orders'
            """
        )
        _, failed_summary = script["build_comparison"](
            conn,
            run_id=1,
            asof_date="2026-07-24",
            metric_names=metrics,
        )
        assert failed_summary["missing_assessment_pair_count"] == 1
        assert failed_summary["acceptance"] == "FAIL"


def test_recovery_baseline_uses_legacy_financial_feature_values() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE feature_financial_metric_availability (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            metric_name TEXT, availability_status TEXT, metric_value REAL,
            period_end TEXT, status_reason TEXT
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            source_id TEXT, financial_confidence REAL,
            fiscal_period_end TEXT, orders REAL, funded_backlog REAL,
            reported_backlog REAL,
            remaining_performance_obligation REAL, rpo_current REAL
        );
        INSERT INTO feature_financial_statement VALUES (
            'TEST', 'defense', '2026-07-24', 'sec_companyfacts', 0.9,
            '2026-03-31', NULL, NULL, NULL, 500000000.0, NULL
        );
        """
    )
    baseline = _baseline_rows(
        conn,
        registry=get_registry(),
        model_family="defense",
        asof_date="2026-07-24",
        tickers=["TEST"],
    )
    assert baseline[("TEST", "remaining_performance_obligation")]["availability_status"] == "REPORTED"
    assert baseline[("TEST", "remaining_performance_obligation")]["metric_value"] == 500_000_000.0
    assert baseline[("TEST", "orders")]["availability_status"] == ("NOT_DISCLOSED")


def test_defense_promotion_readiness_is_fail_closed(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08e_promote_defense_dedicated_parser.py"
        )
    )
    corpus_path = tmp_path / "defense_v1.json"
    corpus_path.write_text(
        '{"corpus_id":"test","expectations":[]}\n',
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.csv"
    policy_path.write_text(
        "enabled,model_family,reviewed_by,reviewed_at\n",
        encoding="utf-8",
    )
    script["_validate_production_readiness"].__globals__["DEFENSE_GOLDEN_CORPUS"] = corpus_path
    registry = SimpleNamespace(review_policy_path=str(policy_path))

    with pytest.raises(ValueError, match="nonempty adjudicated"):
        script["_validate_production_readiness"](registry)

    corpus_path.write_text(
        '{"corpus_id":"test","expectations":[{"id":"reviewed"}]}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="attributed"):
        script["_validate_production_readiness"](registry)

    policy_path.write_text(
        "enabled,model_family,reviewed_by,reviewed_at\n1,defense,analyst,2026-07-25T12:00:00Z\n",
        encoding="utf-8",
    )
    script["_validate_production_readiness"](registry)


def test_defense_exhaustive_hydration_command_is_unlimited_and_cache_only(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08d_run_defense_dedicated_parser_shadow.py"
        )
    )
    command = script["build_hydration_command"](
        config_path=Path("industrials/config.yaml"),
        db_path=Path("industrials.sqlite"),
        asof_date="2026-07-24",
        tickers=["LMT", "RTX"],
        output_csv=tmp_path / "hydration.csv",
        exhaustive=True,
        cache_workers=4,
        accession_scope_csv=tmp_path / "scope.csv",
    )
    assert "--archive-cache-only" in command
    assert "--archive-scan-all-documents" in command
    assert command[command.index("--archive-max-filings-per-ticker") + 1] == "0"
    assert command[command.index("--archive-max-documents-per-filing") + 1] == "0"
    assert "--force-archive" not in command
    assert "--allow-partial" not in command
    assert command[command.index("--archive-cache-workers") + 1] == "4"
    assert "--archive-document-keywords" in command
    assert command[command.index("--archive-accession-scope-csv") + 1] == str(tmp_path / "scope.csv")


def test_evidence_review_requires_failed_hydration_ticker_revalidation(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08g_build_defense_evidence_review_package.py"
        )
    )
    hydration = {
        "before": {"missing_cache_accessions": 2},
    }
    sync_csv = tmp_path / "sync.csv"
    sync_csv.write_text(
        "ticker,cik,status,error\nHEI,0000046619,cache_hydration_failed,race\nLMT,0000936468,cache_hydrated,\n",
        encoding="utf-8",
    )
    validation_csv = tmp_path / "validation.csv"
    validation_csv.write_text(
        "ticker,cik,status,error\nHEI-A,0000046619,cache_hydrated,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="lack successful"):
        script["_validate_hydration_audit"](
            hydration,
            sync_csv=sync_csv,
            validation_csv=validation_csv,
        )

    validation_csv.write_text(
        "ticker,cik,status,error\nHEI,0000046619,cache_hydrated,\nHEI-A,0000046619,cache_hydrated,\n",
        encoding="utf-8",
    )
    result = script["_validate_hydration_audit"](
        hydration,
        sync_csv=sync_csv,
        validation_csv=validation_csv,
    )
    assert result["status"] == "PASS_WITH_SUPPLEMENTAL_VALIDATION"
    assert result["original_failed_tickers"] == ["HEI"]


def test_evidence_review_stderr_audit_allows_only_known_warning(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08g_build_defense_evidence_review_package.py"
        )
    )
    stderr_log = tmp_path / "parser.stderr.log"
    stderr_log.write_text(
        "Subheader 'COMPANY DATA' not found in header '] IRS NUMBER'\n"
        "Subheader 'COMPANY DATA' not found in header '] IRS NUMBER'\n",
        encoding="utf-8",
    )
    result = script["_audit_parser_stderr"](stderr_log)
    assert result["status"] == "PASS_KNOWN_WARNINGS"
    assert result["known_warning_count"] == 2
    assert result["unknown_line_count"] == 0

    stderr_log.write_text("unexpected parser warning\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unclassified"):
        script["_audit_parser_stderr"](stderr_log)


def test_evidence_review_event_catalog_audit_is_hash_and_scope_gated(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08g_build_defense_evidence_review_package.py"
        )
    )
    catalog = tmp_path / "event_catalog.csv"
    catalog.write_text(
        "ticker,status,catalog_end_date,cataloged_filing_count,"
        "missing_history_cache_count\n"
        "AAA,cataloged,2026-07-24,3,0\n"
        "BBB,cataloged,2026-07-24,0,0\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
    hydration = {
        "asof_date": "2026-07-24",
        "catalog": {
            "status": "COMPLETED",
            "output_csv": str(catalog),
            "output_csv_sha256": digest,
            "ticker_count": 2,
            "forms": ["8-K", "8-K/A"],
            "start_date": "2018-01-01",
        },
    }
    audit = script["_validate_event_catalog"](hydration)
    assert audit["status"] == "PASS"
    assert audit["cataloged_filing_count"] == 3
    assert audit["tickers_with_event_filings"] == 1

    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            "AAA,cataloged,2026-07-24,3,0",
            "AAA,cataloged,2026-07-24,4,0",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hash"):
        script["_validate_event_catalog"](hydration)


def test_archive_cache_only_scan_does_not_write_financial_facts(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    should_stop = namespace["should_stop_archive_document_scan"]
    grouped = namespace["group_cache_hydration_items"](
        [
            {"ticker": "HEI", "cik": "0000046619"},
            {"ticker": "HEI-A", "cik": "0000046619"},
            {"ticker": "MOG-A", "cik": "0000067887"},
        ]
    )
    assert [[row["ticker"] for row in group] for group in grouped] == [
        ["HEI", "HEI-A"],
        ["MOG-A"],
    ]
    assert should_stop(
        model_family="defense",
        form_type="10-K",
        mapped_estimate=1,
        special_metric_count=0,
        parse_all_documents=False,
    )
    assert not should_stop(
        model_family="defense",
        form_type="10-K",
        mapped_estimate=1,
        special_metric_count=0,
        parse_all_documents=False,
        scan_all_documents=True,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE fact_sec_filing(
            ticker TEXT, source_id TEXT, accession_number TEXT,
            form_type TEXT, filing_date TEXT, accepted_at TEXT,
            report_date TEXT, fiscal_year INTEGER, fiscal_period TEXT,
            primary_document TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fact_sec_filing VALUES (
            'TEST', 'sec_submissions', '0000000001-26-000001',
            '10-K', '2026-03-01', '2026-03-01T12:00:00Z',
            '2025-12-31', 2025, 'FY', 'primary.htm'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fact_sec_filing VALUES (
            'TEST', 'sec_submissions', '0000000001-25-000002',
            '10-Q', '2025-11-01', '2025-11-01T12:00:00Z',
            '2025-09-30', 2025, 'Q3', 'older.htm'
        )
        """
    )
    index_payload = {
        "directory": {
            "item": [
                {"name": "primary.htm"},
                {"name": "exhibit99.htm"},
            ]
        }
    }
    fetched_documents: list[str] = []

    def fake_json(*args: object, **kwargs: object) -> tuple[object, ...]:
        return 200, index_payload, "{}", "network"

    def fake_text(
        url: str,
        *args: object,
        **kwargs: object,
    ) -> tuple[object, ...]:
        fetched_documents.append(url)
        return 200, "<html><p>No metric</p></html>", "network"

    sync = namespace["sync_archive_xbrl"]
    sync.__globals__["load_or_fetch_json"] = fake_json
    sync.__globals__["load_or_fetch_text"] = fake_text
    staged, mapped, requests = sync(
        conn,
        ticker="TEST",
        cik="0000000001",
        source_id="sec_companyfacts",
        submissions_source_id="sec_submissions",
        model_family="defense",
        cache_dir=tmp_path,
        force=False,
        user_agent="test@example.com",
        timeout_sec=1.0,
        max_retries=1,
        sleep_sec=0.0,
        concept_map={},
        start_date="2000-01-01",
        index_url_template=("https://example.test/{cik_int}/{accession_nodash}/index.json"),
        document_url_template=("https://example.test/{cik_int}/{accession_nodash}/{document_name}"),
        max_filings=0,
        supplemental_forms=set(),
        max_supplemental_filings=-1,
        max_documents=0,
        ingestion_run_id=0,
        scan_all_documents=True,
        cache_only=True,
        accession_filter={"0000000001-26-000001"},
    )
    assert staged == 0
    assert mapped == 0
    assert requests == 4
    assert len(fetched_documents) == 3


def test_cached_event_filing_catalog_is_pit_bounded_and_alias_safe(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    cache_dir = tmp_path / "sec"
    root_cache = namespace["cache_path"](
        cache_dir,
        source_id="sec_submissions",
        cik="0000000001",
    )
    root_cache.parent.mkdir(parents=True)
    root_cache.write_text(
        json.dumps(
            {
                "cik": 1,
                "filings": {
                    "recent": {
                        "accessionNumber": [
                            "0000000001-17-000001",
                            "0000000001-18-000002",
                            "0000000001-26-000003",
                            "0000000001-26-000004",
                            "0000000001-26-000005",
                        ],
                        "filingDate": [
                            "2017-12-31",
                            "2018-01-02",
                            "2026-07-24",
                            "2026-07-25",
                            "2026-07-24",
                        ],
                        "acceptanceDateTime": [
                            "2017-12-31T12:00:00Z",
                            "2018-01-02T12:00:00Z",
                            "2026-07-24T12:00:00Z",
                            "2026-07-25T12:00:00Z",
                            "2026-07-24T13:00:00Z",
                        ],
                        "reportDate": [
                            "2017-12-30",
                            "2018-01-01",
                            "2026-07-23",
                            "2026-07-24",
                            "2026-06-30",
                        ],
                        "form": ["8-K", "8-K", "8-K/A", "8-K", "10-Q"],
                        "primaryDocument": [
                            "old.htm",
                            "first.htm",
                            "amendment.htm",
                            "future.htm",
                            "quarter.htm",
                        ],
                    },
                    "files": [
                        {
                            "name": "CIK0000000001-submissions-001.json",
                            "filingFrom": "2000-01-01",
                            "filingTo": "2017-12-31",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE fact_sec_filing(
            ticker TEXT NOT NULL,
            cik TEXT NOT NULL,
            source_id TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            form_type TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            report_date TEXT NOT NULL,
            fiscal_year INTEGER,
            fiscal_period TEXT NOT NULL,
            primary_document TEXT NOT NULL,
            filing_url TEXT NOT NULL,
            source_detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(ticker, accession_number, source_id)
        );
        CREATE TABLE fact_sec_xbrl_fact(marker TEXT);
        CREATE TABLE fact_sec_xbrl_fact_raw(marker TEXT);
        INSERT INTO fact_sec_xbrl_fact VALUES ('canonical-sentinel');
        INSERT INTO fact_sec_xbrl_fact_raw VALUES ('raw-sentinel');
        """
    )
    rows = namespace["catalog_cached_submission_filings"](
        conn,
        items=[
            {"ticker": "TEST", "cik": "0000000001"},
            {"ticker": "TEST-A", "cik": "0000000001"},
        ],
        source_id="sec_submissions",
        cache_dir=cache_dir,
        allowed_forms={"8-K", "8-K/A"},
        start_date="2018-01-01",
        end_date="2026-07-24",
        max_history_files=0,
    )
    assert [row["status"] for row in rows] == ["cataloged", "cataloged"]
    assert [row["cataloged_filing_count"] for row in rows] == [2, 2]
    filings = conn.execute(
        """
        SELECT ticker, accession_number, form_type, filing_date, source_detail
        FROM fact_sec_filing
        ORDER BY ticker, filing_date, accession_number
        """
    ).fetchall()
    assert [tuple(row) for row in filings] == [
        (
            "TEST",
            "0000000001-18-000002",
            "8-K",
            "2018-01-02",
            "sec_submissions_recent_cache_catalog",
        ),
        (
            "TEST",
            "0000000001-26-000003",
            "8-K/A",
            "2026-07-24",
            "sec_submissions_recent_cache_catalog",
        ),
        (
            "TEST-A",
            "0000000001-18-000002",
            "8-K",
            "2018-01-02",
            "sec_submissions_recent_cache_catalog",
        ),
        (
            "TEST-A",
            "0000000001-26-000003",
            "8-K/A",
            "2026-07-24",
            "sec_submissions_recent_cache_catalog",
        ),
    ]
    assert conn.execute("SELECT marker FROM fact_sec_xbrl_fact").fetchone()[0] == "canonical-sentinel"
    assert conn.execute("SELECT marker FROM fact_sec_xbrl_fact_raw").fetchone()[0] == "raw-sentinel"


def test_cached_event_filing_catalog_rejects_cik_mismatch(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    cache_dir = tmp_path / "sec"
    root_cache = namespace["cache_path"](
        cache_dir,
        source_id="sec_submissions",
        cik="0000000001",
    )
    root_cache.parent.mkdir(parents=True)
    root_cache.write_text(
        '{"cik":2,"filings":{"recent":{"form":[]}}}',
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    rows = namespace["catalog_cached_submission_filings"](
        conn,
        items=[{"ticker": "TEST", "cik": "0000000001"}],
        source_id="sec_submissions",
        cache_dir=cache_dir,
        allowed_forms={"8-K"},
        start_date="2018-01-01",
        end_date="2026-07-24",
        max_history_files=0,
    )
    assert rows[0]["status"] == "catalog_failed"
    assert "cached_submission_cik_mismatch" in rows[0]["error"]


def test_complete_review_package_includes_no_evidence_pairs(
    tmp_path: Path,
) -> None:
    script = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "industrials"
            / "defense"
            / "scripts"
            / "08g_build_defense_evidence_review_package.py"
        )
    )
    db_path = tmp_path / "review.sqlite"
    with connect_database(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE dim_universe_membership(
                ticker TEXT, model_family TEXT, start_date TEXT, end_date TEXT
            );
            CREATE TABLE dim_company(
                ticker TEXT, company_name TEXT, cik TEXT
            );
            CREATE TABLE dim_industrials_taxonomy(
                ticker TEXT, model_family TEXT, calibration_cohort TEXT
            );
            INSERT INTO dim_universe_membership
            VALUES ('TEST', 'defense', '2019-01-01', NULL);
            INSERT INTO dim_company
            VALUES ('TEST', 'Test Defense', '0000000001');
            INSERT INTO dim_industrials_taxonomy
            VALUES ('TEST', 'defense', 'defense_primes_and_services');
            """
        )
        now = utc_now()
        conn.execute(
            """
            INSERT INTO sec_parser_run(
                run_id, model_family, asof_date, parser_release,
                adapter_version, mode, worker_count, started_at,
                completed_at, status
            )
            VALUES (
                1, 'defense', '2026-07-24', 'test',
                ?, 'shadow', 1, ?, ?, 'COMPLETED'
            )
            """,
            (ADAPTER_VERSION, now, now),
        )
        metrics = tuple(request.metric_name for request in get_registry().source_metrics)
        conn.executemany(
            """
            INSERT INTO sec_parser_recovery_assessment(
                run_id, model_family, ticker, metric_name, asof_date,
                baseline_status, baseline_value, anchor_period_end,
                recovery_class, predicted_status, evidence_keys_json,
                status_reason, created_at
            )
            VALUES (
                1, 'defense', 'TEST', ?, '2026-07-24',
                'NOT_DISCLOSED', NULL, '2026-03-31',
                'NOT_FOUND_IN_SEARCHED_DOCUMENTS', 'NOT_DISCLOSED',
                '[]', 'no evidence', ?
            )
            """,
            [(metric, now) for metric in metrics],
        )
        rows, summary = script["build_review_package"](
            conn,
            run_id=1,
            asof_date="2026-07-24",
            metric_names=metrics,
        )
    assert len(rows) == 5
    assert {row["record_type"] for row in rows} == {"assessment_no_evidence"}
    assert summary["expected_assessment_pairs"] == 5
    assert summary["assessment_pair_count"] == 5
    assert summary["no_evidence_pair_count"] == 5
    assert summary["no_evidence_priority_counts"] == {"4_no_evidence_or_structural": 5}
    assert summary["high_priority_no_evidence_pairs"] == []
    assert summary["review_reconciliation_counts"] == [
        {
            "record_type": "assessment_no_evidence",
            "recovery_class": "NOT_FOUND_IN_SEARCHED_DOCUMENTS",
            "review_priority": "4_no_evidence_or_structural",
            "row_count": 5,
        }
    ]
