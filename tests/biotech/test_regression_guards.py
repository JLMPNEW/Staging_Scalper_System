from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from tests.biotech.conftest import load_script_module


def test_sec_event_worker_exception_does_not_write_parse_state(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("07_parse_sec_biotech_events.py", "sec_events_regression")
    replace_called = False

    def fail_detect(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("worker smoke failure")

    def replace_events(*_args: object, **_kwargs: object) -> None:
        nonlocal replace_called
        replace_called = True

    monkeypatch.setattr(module, "detect_events", fail_detect)
    monkeypatch.setattr(module, "replace_events", replace_events)
    filing = module.FilingText(1, "TST", "Test Co", "0001", "2026-05-08", "8-K", "", "hash", "text")

    with pytest.raises(RuntimeError, match="worker smoke failure"):
        module.parse_filing_batch(
            sqlite3.connect(":memory:"),
            [filing],
            min_confidence=0.0,
            max_per_type=1,
            max_workers=2,
            parser_signature="smoke",
        )

    assert replace_called is False


def test_forward_guidance_worker_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("19_parse_forward_guidance.py", "forward_guidance_regression")

    def fail_detect(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("guidance smoke failure")

    monkeypatch.setattr(module, "detect_guidance", fail_detect)
    filing = module.FilingText(
        company_id=1,
        ticker="TST",
        company_name="Test Co",
        accession_nodash="0001",
        filing_date="2026-05-08",
        form="10-Q",
        archive_url="",
        document_type="complete_submission_text",
        text_content="text",
        text_hash="hash",
    )

    with pytest.raises(RuntimeError, match="guidance smoke failure"):
        module.parse_guidance_records(
            [filing],
            asof_date=date(2026, 5, 8),
            min_confidence=0.0,
            max_windows_per_filing=1,
            max_workers=2,
        )


def test_score_rows_missing_risk_score_raw_uses_default() -> None:
    module = load_script_module("11_score_biotech_index.py", "score_biotech_regression")
    rows = [
        {
            "asof_date": "2026-05-08",
            "company_id": 1,
            "ticker": "TST",
            "company_name": "Test Co",
            "catalyst_score_raw": 50.0,
            "credibility_score_raw": 50.0,
            "feature_json": "{}",
        }
    ]
    config = {
        "biotech_scoring": {
            "use_investment_score": False,
            "data_quality_adjustment": {"enabled": False},
            "weights": {
                "catalyst": 0.45,
                "credibility": 0.30,
                "financial_quality": 0.15,
                "momentum": 0.10,
                "risk_penalty": 0.35,
            },
        }
    }

    scored = module.score_rows(rows, config, commercial_by_company={}, forward_by_company={})

    assert scored[0]["risk_score"] == 0.0


def test_companyfacts_fetch_reuses_supplied_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("15_sync_sec_companyfacts_history.py", "companyfacts_regression")

    class FakeHttp:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_json(self, **_kwargs: object) -> dict[str, object]:
            self.calls += 1
            return {"facts": {"ok": True}}

    fake_http = FakeHttp()
    monkeypatch.setattr(
        module,
        "parse_observations",
        lambda _payload, *, company, cutoff: [{"company_id": company.company_id, "concept": f"x:{cutoff.isoformat()}"}],
    )
    monkeypatch.setattr(
        module,
        "normalize_rows",
        lambda _observations, company_id: [{"company_id": company_id, "period_end": "2026-03-31"}],
    )
    company = module.Company(1, "TST", "1234567890", "Test Co")

    result = module.fetch_companyfacts_result(
        company,
        url_template="https://example.test/CIK{cik}.json",
        headers={},
        cache_dir=Path("unused"),
        ttl_hours=1.0,
        sleep_sec=0.0,
        timeout_sec=1.0,
        max_retries=1,
        throttle=module.HostThrottle(),
        cutoff=date(2025, 1, 1),
        latest_source_filing_date="2026-05-08",
        http=fake_http,
    )

    assert fake_http.calls == 1
    assert result.error == ""
    assert result.normalized[0]["company_id"] == 1


def test_governance_invalid_date_returns_none() -> None:
    module = load_script_module("20_build_governance_event_features.py", "governance_regression")

    assert module.parse_date("not-a-date") is None


def test_multibagger_missing_layer_helper_identifies_missing_tickers() -> None:
    module = load_script_module("21_build_multibagger_features.py", "multibagger_features_regression")
    base_rows = [{"company_id": 1, "ticker": "AAA"}, {"company_id": 2, "ticker": "BBB"}]

    assert module.missing_layer_tickers(base_rows, {1: {"asof_date": "2026-05-08"}}) == ["BBB"]
