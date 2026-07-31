from __future__ import annotations

from datetime import date

from portfolio_layer.risk import yahoo


def test_fetch_adjclose_uses_second_host_when_first_tail_is_stale(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_fetch(
        ticker: str,
        *,
        start: date,
        end: date,
        url_template: str,
        user_agent: str,
        timeout_sec: float,
        max_retries: int,
    ) -> tuple[
        list[tuple[str, float]],
        list[dict[str, str]],
        str,
    ]:
        del ticker, start, end, user_agent, timeout_sec, max_retries
        calls.append(url_template)
        last = "2026-07-23" if url_template == "query1" else "2026-07-24"
        return [(last, 100.0)], [], "ok"

    monkeypatch.setattr(yahoo, "_fetch_yahoo_adjclose", fake_fetch)

    rows, _splits, status, provider, _source = (
        yahoo.fetch_adjclose_with_splits(
            "XBI",
            start=date(2026, 7, 20),
            end=date(2026, 7, 24),
            url_templates=["query1", "query2"],
            user_agent="test",
            timeout_sec=1,
            max_retries=0,
        )
    )

    assert calls == ["query1", "query2"]
    assert rows == [("2026-07-24", 100.0)]
    assert status == "ok"
    assert provider == "yahoo_query2"


def test_fetch_adjclose_retains_best_partial_series(monkeypatch) -> None:
    def fake_fetch(
        ticker: str,
        *,
        start: date,
        end: date,
        url_template: str,
        user_agent: str,
        timeout_sec: float,
        max_retries: int,
    ) -> tuple[
        list[tuple[str, float]],
        list[dict[str, str]],
        str,
    ]:
        del ticker, start, end, user_agent, timeout_sec, max_retries
        if url_template == "query1":
            return [("2026-07-22", 99.0)], [], "ok"
        return [("2026-07-23", 100.0)], [], "ok"

    monkeypatch.setattr(yahoo, "_fetch_yahoo_adjclose", fake_fetch)

    rows, _splits, status, provider, _source = (
        yahoo.fetch_adjclose_with_splits(
            "XBI",
            start=date(2026, 7, 20),
            end=date(2026, 7, 24),
            url_templates=["query1", "query2"],
            user_agent="test",
            timeout_sec=1,
            max_retries=0,
        )
    )

    assert rows == [("2026-07-23", 100.0)]
    assert status == "ok"
    assert provider == "yahoo_query2"
