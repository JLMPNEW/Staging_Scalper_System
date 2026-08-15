from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from dedicated_parser.contracts import (
    DocumentRef,
    FilingRef,
    MetricEvidence,
    MetricRequest,
    NormalizedFact,
    WorkItem,
    file_sha256,
)
from industrials.transportation.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    _metric_contracts,
    _text_patterns,
    applicable_parser_metrics,
    extract_metric_evidence,
    get_registry,
    map_normalized_facts,
    postprocess_metric_evidence,
    select_tickers,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRANSPORTATION_ROOT = PROJECT_ROOT / "industrials" / "transportation"
FINAL_SCOPE = TRANSPORTATION_ROOT / "data" / "transportation_dedicated_parser_scope.csv"
SUPPORT_SCOPE = TRANSPORTATION_ROOT / "data" / "transportation_dedicated_parser_support_scope.csv"
ADAPTER = "industrials.transportation.dedicated_parser_adapter:extract_metric_evidence"


def _document(path: Path, text: str) -> DocumentRef:
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    return DocumentRef(
        name=path.name,
        path=str(path),
        content_sha256=file_sha256(path),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        is_primary=True,
    )


def _item(
    *,
    ticker: str,
    metric_ids: tuple[str, ...],
    document: DocumentRef | None = None,
) -> WorkItem:
    return WorkItem(
        model_family="transportation",
        adapter_path=ADAPTER,
        adapter_version=ADAPTER_VERSION,
        filing=FilingRef(
            ticker=ticker,
            cik="0000000001",
            accession_number="0000000001-26-000001",
            form_type="10-K",
            filing_date="2026-02-15",
            accepted_at="2026-02-15T21:00:00Z",
            report_date="2025-12-31",
            primary_document=(document.name if document is not None else "test.htm"),
            source_id="sec_archive_xbrl",
        ),
        documents=(() if document is None else (document,)),
        requested_metrics=tuple(MetricRequest(metric_id) for metric_id in metric_ids),
        enable_arelle=False,
        enable_edgartools=False,
    )


def _applicable_ticker_by_metric() -> dict[str, str]:
    output: dict[str, str] = {}
    for path, metric_field in (
        (FINAL_SCOPE, "metric_id"),
        (SUPPORT_SCOPE, "support_metric_id"),
    ):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                metric_id = str(row[metric_field])
                ticker = str(row["ticker"])
                if (
                    row["applicability_status"] == "APPLICABLE"
                    and metric_id in _metric_contracts()
                    and metric_id in applicable_parser_metrics(ticker)
                ):
                    output.setdefault(metric_id, ticker)
    return output


def _nonapplicable_ticker_by_metric() -> dict[str, str]:
    output: dict[str, str] = {}
    for path, metric_field in (
        (FINAL_SCOPE, "metric_id"),
        (SUPPORT_SCOPE, "support_metric_id"),
    ):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                metric_id = str(row[metric_field])
                if row["applicability_status"] == "NOT_APPLICABLE" and metric_id in _metric_contracts():
                    output.setdefault(metric_id, str(row["ticker"]))
    return output


def test_registry_freezes_77_final_and_7_supporting_search_metrics() -> None:
    registry = get_registry()
    source = {row.metric_name for row in registry.source_metrics}
    supporting = {row.metric_name for row in registry.supporting_metrics}
    assert registry.model_family == "transportation"
    assert len(source) == 77
    assert len(supporting) == 7
    assert len(registry.parser_metrics) == 84
    assert source.isdisjoint(supporting)
    assert {row["metric_id"] for row in _metric_contracts().values() if row["source_lane"] == "DP"} == source
    assert {row["metric_id"] for row in _metric_contracts().values() if row["source_lane"] == "DP-S"} == supporting
    assert "cash_runway_years" not in source
    assert "fuel_efficiency_per_capacity_unit" not in source
    assert registry.production_mappings == {}
    assert Path(registry.review_policy_path).is_file()
    assert {
        "10-K405",
        "10-QT",
        "ARS",
        "DEF 14A",
        "DEFM14A",
        "FWP",
        "S-4",
        "424B5",
    } <= set(registry.supported_forms)


def test_selector_includes_historical_membership_started_by_asof() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE dim_universe_membership(
            ticker TEXT,
            model_family TEXT,
            start_date TEXT,
            end_date TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO dim_universe_membership VALUES (?, ?, ?, ?)",
        [
            ("ACTIVE", "transportation", "2019-01-01", None),
            ("OLD", "transportation", "2018-01-01", "2020-01-01"),
            ("FUTURE", "transportation", "2027-01-01", None),
            ("OTHER", "defense", "2010-01-01", None),
        ],
    )
    assert select_tickers(conn, "2026-07-26") == ["ACTIVE", "OLD"]


def test_applicability_is_archetype_specific_and_fail_closed() -> None:
    assert "passenger_load_factor" in applicable_parser_metrics("AAL")
    assert "airline_fuel_consumed" in applicable_parser_metrics("AAL")
    assert "traffic_growth" in applicable_parser_metrics("AAWW")
    assert "passenger_load_factor" not in applicable_parser_metrics("AAWW")
    assert "airline_fuel_consumed" not in applicable_parser_metrics("AAWW")
    assert applicable_parser_metrics("UNKNOWN") == frozenset()


@pytest.mark.parametrize(
    ("metric_id", "ticker"),
    sorted(_applicable_ticker_by_metric().items()),
)
def test_every_search_metric_has_positive_offline_discovery_fixture(
    tmp_path: Path,
    metric_id: str,
    ticker: str,
) -> None:
    contract = _metric_contracts()[metric_id]
    alias = contract["search_aliases"].split("|")[0]
    value_text = (
        "December 31, 2027"
        if metric_id == "milestone_target_date"
        else "1"
        if contract["bounds_policy"]
        in {
            "boolean",
            "nonnegative_integer",
            "ordinal_0_5",
        }
        else "12.5%"
        if contract["bounds_policy"].startswith("ratio") or contract["bounds_policy"] == "growth_ratio"
        else "$12.5 million"
        if "currency" in contract["unit_contract"]
        else "12.5"
    )
    document = _document(
        tmp_path / f"{metric_id}.htm",
        f"<p>Our {alias} was {value_text} for the year ended 2025.</p>",
    )
    evidence = extract_metric_evidence(_item(ticker=ticker, metric_ids=(metric_id,), document=document))
    assert any(row.metric_name == metric_id for row in evidence)
    assert all(row.status in {"REVIEW_REQUIRED", "REJECTED_POLICY"} for row in evidence)


def test_one_document_is_semantically_parsed_once_for_many_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import industrials.transportation.dedicated_parser_adapter as adapter

    original = adapter.parse_semantic_document
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "parse_semantic_document", counted)
    document = _document(
        tmp_path / "airline.htm",
        """
        <p>Our traffic growth was 7.0%.</p>
        <p>Our capacity growth was 5.0%.</p>
        <p>Our passenger load factor was 84.0%.</p>
        """,
    )
    evidence = extract_metric_evidence(
        _item(
            ticker="AAL",
            metric_ids=(
                "traffic_growth",
                "capacity_growth",
                "passenger_load_factor",
            ),
            document=document,
        )
    )
    assert calls == 1
    assert {row.metric_name for row in evidence} == {
        "traffic_growth",
        "capacity_growth",
        "passenger_load_factor",
    }


@pytest.mark.parametrize(
    ("metric_id", "ticker"),
    sorted(_nonapplicable_ticker_by_metric().items()),
)
def test_every_search_metric_has_prohibited_cross_archetype_fixture(
    metric_id: str,
    ticker: str,
) -> None:
    item = _item(ticker=ticker, metric_ids=(metric_id,))
    candidate = MetricEvidence(
        metric_name=metric_id,
        concept_name="SyntheticCrossArchetypeCandidate",
        value=1.0,
        unit=_metric_contracts()[metric_id]["unit_contract"],
        period_start="",
        period_end="2025-12-31",
        scope="consolidated",
        confidence=0.95,
        status="ACCEPTED",
        reason="fixture",
        evidence_text="Synthetic prohibited cross-archetype candidate.",
        source_document="test.htm",
        extraction_method="fixture",
    )
    output = postprocess_metric_evidence(item, (candidate,))
    assert len(output) == 1
    assert output[0].status == "REJECTED_POLICY"
    assert output[0].reason == ("ticker_metric_not_applicable_in_sealed_scope")


def test_postprocessor_rejects_cross_archetype_candidate() -> None:
    item = _item(ticker="AAWW", metric_ids=("passenger_load_factor",))
    candidate = MetricEvidence(
        metric_name="passenger_load_factor",
        concept_name="PassengerLoadFactor",
        value=0.84,
        unit="ratio",
        period_start="",
        period_end="2025-12-31",
        scope="consolidated",
        confidence=0.95,
        status="ACCEPTED",
        reason="fixture",
        evidence_text="Passenger load factor was 84%.",
        source_document="test.htm",
        extraction_method="fixture",
    )
    output = postprocess_metric_evidence(item, (candidate,))
    assert len(output) == 1
    assert output[0].status == "REJECTED_POLICY"
    assert output[0].reason == ("ticker_metric_not_applicable_in_sealed_scope")


def test_normalized_fact_mapping_keeps_extension_in_review() -> None:
    item = _item(ticker="AAL", metric_ids=("passenger_load_factor",))
    fact = NormalizedFact(
        taxonomy="aal",
        concept_name="PassengerLoadFactor",
        value_text="0.84",
        numeric_value=0.84,
        unit="ratio",
        period_start="2025-01-01",
        period_end="2025-12-31",
        context_id="D2025",
        dimensions_json="{}",
        scope="consolidated",
        source_document="aal-20251231.htm",
        provider="arelle",
    )
    evidence = map_normalized_facts(item, (fact,))
    assert len(evidence) == 1
    assert evidence[0].metric_name == "passenger_load_factor"
    assert evidence[0].status == "REVIEW_REQUIRED"
    assert evidence[0].value == pytest.approx(0.84)


def test_every_search_metric_has_patterns_and_applicable_identity() -> None:
    applicable = _applicable_ticker_by_metric()
    nonapplicable = _nonapplicable_ticker_by_metric()
    assert set(applicable) == set(_metric_contracts())
    assert set(nonapplicable) == set(_metric_contracts())
    assert len(applicable) == 84
    for metric_id in applicable:
        assert _text_patterns(metric_id)
        request = get_registry().request(metric_id)
        assert request is not None
        assert request.concept_patterns


def _accepted_passenger_load_factor(**overrides) -> MetricEvidence:
    values = {
        "metric_name": "passenger_load_factor",
        "concept_name": "PassengerLoadFactor",
        "value": 0.84,
        "unit": "ratio",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "scope": "consolidated",
        "confidence": 0.95,
        "status": "ACCEPTED",
        "reason": "fixture",
        "evidence_text": "Passenger load factor was 84%.",
        "source_document": "test.htm",
        "extraction_method": "fixture",
    }
    values.update(overrides)
    return MetricEvidence(**values)


def test_postprocessor_rejects_invalid_unit_contract() -> None:
    output = postprocess_metric_evidence(
        _item(ticker="AAL", metric_ids=("passenger_load_factor",)),
        (_accepted_passenger_load_factor(unit="USD"),),
    )
    assert len(output) == 1
    assert output[0].status == "REJECTED_POLICY"
    assert output[0].reason == "unit_contract_mismatch"


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"period_end": ""}, "missing_period_end"),
        ({"period_end": "not-a-date"}, "invalid_period_end"),
        (
            {"period_start": "2026-01-01", "period_end": "2025-12-31"},
            "period_start_after_period_end",
        ),
        (
            {"period_start": "2026-01-01", "period_end": "2026-12-31"},
            "period_end_after_filing_availability",
        ),
    ],
)
def test_postprocessor_rejects_invalid_period_contract(
    overrides: dict[str, str],
    expected_reason: str,
) -> None:
    output = postprocess_metric_evidence(
        _item(ticker="AAL", metric_ids=("passenger_load_factor",)),
        (_accepted_passenger_load_factor(**overrides),),
    )
    assert len(output) == 1
    assert output[0].status == "REJECTED_POLICY"
    assert output[0].reason == expected_reason


def test_acceptance_requires_explicit_consolidated_issuer_scope() -> None:
    output = postprocess_metric_evidence(
        _item(ticker="AAL", metric_ids=("passenger_load_factor",)),
        (_accepted_passenger_load_factor(scope="unknown"),),
    )
    assert len(output) == 1
    assert output[0].status == "REVIEW_REQUIRED"
    assert output[0].reason == "explicit_issuer_scope_required_for_acceptance"


def test_conflicting_accepted_values_are_routed_to_review() -> None:
    candidates = (
        _accepted_passenger_load_factor(value=0.82),
        _accepted_passenger_load_factor(value=0.84),
    )
    output = postprocess_metric_evidence(
        _item(ticker="AAL", metric_ids=("passenger_load_factor",)),
        candidates,
    )
    assert len(output) == 2
    assert {row.status for row in output} == {"REVIEW_REQUIRED"}
    assert {row.reason for row in output} == {"conflicting_values_require_review"}


def test_postprocessing_fingerprint_is_input_order_independent() -> None:
    item = _item(ticker="AAL", metric_ids=("passenger_load_factor",))
    candidates = (
        _accepted_passenger_load_factor(
            value=0.82,
            concept_name="IssuerPassengerLoadFactor",
        ),
        _accepted_passenger_load_factor(
            value=0.84,
            concept_name="PassengerLoadFactor",
        ),
        _accepted_passenger_load_factor(
            value=0.84,
            concept_name="PassengerLoadFactor",
        ),
    )
    forward = postprocess_metric_evidence(item, candidates)
    reverse = postprocess_metric_evidence(item, tuple(reversed(candidates)))
    assert forward == reverse


def test_parser_failure_is_not_reclassified_as_policy_rejection() -> None:
    failure = _accepted_passenger_load_factor(
        value=None,
        unit="",
        period_end="",
        scope="unknown",
        status="PARSER_FAILURE",
        reason="document_read_failed:OSError",
    )
    output = postprocess_metric_evidence(
        _item(ticker="AAL", metric_ids=("passenger_load_factor",)),
        (failure,),
    )
    assert output == (failure,)
