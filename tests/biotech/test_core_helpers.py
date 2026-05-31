from __future__ import annotations

from datetime import date

from biotech_index.core.biotech_taxonomy import classify_biotech_cohort
from biotech_index.core.config import normalize_string_list
from biotech_index.core.http_cache import CachedHttpClient
from biotech_index.core.market_policy import select_latest_rows_by_source_priority


def test_normalize_string_list_splits_cli_delimiters_and_drops_empty_values() -> None:
    assert normalize_string_list(" yahoo_adjusted, interactive_brokers | manual ; ", ["default"]) == [
        "yahoo_adjusted",
        "interactive_brokers",
        "manual",
    ]
    assert normalize_string_list([" keep ", None, "", "review"], ["default"]) == ["keep", "review"]
    assert normalize_string_list(None, ["keep", "review"]) == ["keep", "review"]


def test_market_source_selection_ignores_future_rows_and_normalizes_sources() -> None:
    rows = [
        {"company_id": 1, "asof_date": "2026-05-09", "source": "yahoo_adjusted", "value": 99},
        {"company_id": 1, "asof_date": "2026-05-08", "source": " interactive_brokers ", "value": 10},
        {"company_id": 2, "asof_date": "2026-05-08", "source": "YAHOO_ADJUSTED", "value": 20},
        {"company_id": 2, "asof_date": "2026-05-08", "source": "interactive_brokers", "value": 11},
    ]

    selected = select_latest_rows_by_source_priority(
        rows,
        asof_date=date(2026, 5, 8),
        source_priority=["Yahoo_Adjusted", "interactive_brokers"],
        max_staleness_days=0,
    )

    assert selected[1]["value"] == 10
    assert selected[2]["value"] == 20


def test_taxonomy_going_concern_overlay_uses_shared_status_sets() -> None:
    for status in ("going_concern_confirmed", "substantial_doubt", "hard", "warning"):
        classification = classify_biotech_cohort(
            payload={
                "ctgov": {"verified_qualifying_active_trial_count": 1},
                "financial_survival": {"data_quality": "high", "going_concern_status": status},
                "sec_and_liquidity": {},
            },
            commercial={},
            forward_guidance={},
            diagnostics={},
        )

        assert "going_concern" in classification.overlays


def test_cached_json_null_cache_is_refetched(tmp_path, monkeypatch) -> None:
    client = CachedHttpClient(cache_dir=tmp_path, sleep_sec=0.0, timeout_sec=1.0, max_retries=1)
    url = "https://example.test/data"
    path = client.cache_path("json", url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("null", encoding="utf-8")
    calls = 0

    def fake_get_text(*, url: str, params: object, headers: dict[str, str]) -> str:
        nonlocal calls
        calls += 1
        return '{"ok": true}'

    try:
        monkeypatch.setattr(client, "_get_text", fake_get_text)

        assert client.fetch_json(namespace="json", url=url, ttl_hours=24.0) == {"ok": True}
        assert calls == 1
        assert path.read_text(encoding="utf-8") == '{"ok": true}'
    finally:
        client.close()
