from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.db import connect
from consumer_defensive.core.market_data import MarketDataPolicy, load_market_policy
from consumer_defensive.core.stage4 import (
    _cache_manifest_summary,
    _cached,
    _fx_rate,
    _validate_fx_chart_payload,
    bootstrap_stage4,
    sync_sec_fundamentals,
    sync_fx_rates,
)
from consumer_defensive.core.universe import load_current_universe, load_policy
from consumer_defensive.core.yahoo_prices import fetch_yahoo_job


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive" / "config.yaml"
MARKET_POLICY = (
    ROOT
    / "consumer_defensive"
    / "data"
    / "consumer_defensive_market_data_policy.yaml"
)
UNIVERSE_POLICY = (
    ROOT
    / "consumer_defensive"
    / "data"
    / "consumer_defensive_universe_policy.yaml"
)


def _fx_bundle(tmp_path: Path) -> ConfigBundle:
    source = load_config(CONFIG)
    payload = copy.deepcopy(source.payload)
    payload["fx_rates"]["cache_dir"] = str(tmp_path / "fx_cache")
    return ConfigBundle(source.path, source.base_dir, payload)


def _sec_bundle(tmp_path: Path) -> ConfigBundle:
    source = load_config(CONFIG)
    payload = copy.deepcopy(source.payload)
    payload["sec_fundamentals"]["cache_dir"] = str(tmp_path / "sec_cache")
    return ConfigBundle(source.path, source.base_dir, payload)


def _epoch(raw: str) -> int:
    value = datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=timezone.utc)
    return int(value.timestamp())


def _fx_payload(
    dates: list[str], closes: list[object], *, symbol: str = 'CLPUSD=X',
) -> dict[str, object]:
    return {'chart': {'result': [{
        'meta': {'symbol': symbol},
        'timestamp': [_epoch(raw) for raw in dates],
        'indicators': {'quote': [{'close': closes}]},
    }], 'error': None}}


def test_fx_payload_skips_null_closes_and_filters_near_boundary_rows() -> None:
    observations = _validate_fx_chart_payload(
        _fx_payload(
            ['2023-12-31', '2024-01-01', '2024-01-02', '2024-01-08'],
            [0.0009, None, 0.0011, 0.0012],
        ),
        expected_symbol='CLPUSD=X',
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 7),
    )

    assert [
        (str(row.rate_date), row.rate) for row in observations
    ] == [('2024-01-02', 0.0011)]


@pytest.mark.parametrize(
    'bad_close', ['0.001', True, 0.0, -0.001, float('nan'), 10 ** 1000],
)
def test_fx_payload_rejects_non_null_malformed_closes_even_near_boundary(
    bad_close: object,
) -> None:
    with pytest.raises(ValueError, match='finite and positive'):
        _validate_fx_chart_payload(
            _fx_payload(
                ['2023-12-31', '2024-01-02'],
                [bad_close, 0.001],
            ),
            expected_symbol='CLPUSD=X',
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 7),
        )


@pytest.mark.parametrize('bad_timestamp', [True, '1', 1.5, float('nan'), 10 ** 1000])
def test_fx_payload_rejects_malformed_timestamps(bad_timestamp: object) -> None:
    payload = _fx_payload(['2024-01-02'], [0.001])
    payload['chart']['result'][0]['timestamp'][0] = bad_timestamp

    with pytest.raises(ValueError, match='must be a finite integer'):
        _validate_fx_chart_payload(
            payload,
            expected_symbol='CLPUSD=X',
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 7),
        )


def test_fx_payload_requires_a_usable_in_window_observation() -> None:
    with pytest.raises(ValueError, match='no usable observations in requested range'):
        _validate_fx_chart_payload(
            _fx_payload(
                ['2023-12-31', '2024-01-01', '2024-01-08'],
                [0.0009, None, 0.0011],
            ),
            expected_symbol='CLPUSD=X',
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 7),
        )


def test_fx_payload_rejects_material_range_mismatch_even_with_in_window_data() -> None:
    with pytest.raises(ValueError, match='materially outside requested range'):
        _validate_fx_chart_payload(
            _fx_payload(['2023-12-01', '2024-01-02'], [0.0009, 0.001]),
            expected_symbol='CLPUSD=X',
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 7),
        )


def test_fx_payload_rejects_excess_near_boundary_rows() -> None:
    with pytest.raises(ValueError, match='too many observations outside requested range'):
        _validate_fx_chart_payload(
            _fx_payload(
                ['2023-12-30', '2023-12-31', '2024-01-02', '2024-01-08'],
                [0.0008, 0.0009, 0.001, 0.0011],
            ),
            expected_symbol='CLPUSD=X',
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 7),
        )


def test_fx_payload_rejects_multiple_observations_on_one_utc_date() -> None:
    payload = _fx_payload(['2024-01-02', '2024-01-03'], [0.001, 0.0011])
    result = payload['chart']['result'][0]
    result['timestamp'][1] = result['timestamp'][0] + 3600

    with pytest.raises(ValueError, match='unique increasing UTC dates'):
        _validate_fx_chart_payload(
            payload,
            expected_symbol='CLPUSD=X',
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 7),
        )


def test_cache_manifest_deduplicates_identical_paths_and_rejects_conflicts() -> None:
    record = {"path": "submissions/one.json", "bytes": 10, "sha256": "a" * 64}
    summary = _cache_manifest_summary([record, dict(record)])
    assert summary["files"] == 1
    assert summary["bytes"] == 10
    assert summary["entries"] == [record]

    conflicting = {**record, "sha256": "b" * 64}
    with pytest.raises(ValueError, match="Conflicting cache-manifest observations"):
        _cache_manifest_summary([record, conflicting])


def test_stage4_cache_only_missing_key_never_calls_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSUMER_DEFENSIVE_CACHE_ONLY", "1")
    called = False

    def forbidden_fetch(_: str) -> bytes:
        nonlocal called
        called = True
        raise AssertionError("network callback must not run in cache-only mode")

    with pytest.raises(FileNotFoundError, match="cache entry missing"):
        _cached(forbidden_fetch, "https://invalid.example", tmp_path / "missing.json")
    assert called is False


def test_yahoo_cache_only_missing_key_never_calls_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = load_market_policy(MARKET_POLICY)
    payload = copy.deepcopy(source.payload)
    payload["yahoo"]["cache_dir"] = str(tmp_path / "yahoo_cache")
    policy = MarketDataPolicy(path=source.path, payload=payload)
    monkeypatch.setenv("CONSUMER_DEFENSIVE_CACHE_ONLY", "true")
    called = False

    def forbidden_fetch(*args: object, **kwargs: object) -> tuple[int, str]:
        nonlocal called
        called = True
        raise AssertionError("network callback must not run in cache-only mode")

    with pytest.raises(FileNotFoundError, match="cache entry missing"):
        fetch_yahoo_job(
            "KO",
            policy=policy,
            start=date(2024, 1, 1),
            end=date(2024, 1, 7),
            force_refresh=False,
            fetcher=forbidden_fetch,
        )
    assert called is False


def test_sec_cache_only_without_exact_seal_fails_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _sec_bundle(tmp_path)
    monkeypatch.setenv("CONSUMER_DEFENSIVE_CACHE_ONLY", "1")
    called = False

    def forbidden_fetch(_: str) -> bytes:
        nonlocal called
        called = True
        raise AssertionError("network callback must not run in cache-only mode")

    with connect(tmp_path / "sec_missing.sqlite") as conn:
        bootstrap_stage4(conn, bundle)
        load_current_universe(conn, load_policy(UNIVERSE_POLICY))
        with pytest.raises(RuntimeError, match='immutable snapshot and reconciliation'):
            sync_sec_fundamentals(
                conn,bundle,tickers=['KO','PEP'],as_of='2024-12-31',
                fetch=forbidden_fetch,
            )
        assert called is False
        assert conn.execute(
            'SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw'
        ).fetchone()[0] == 0


def test_sec_cache_only_reports_missing_filing_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _sec_bundle(tmp_path)
    accession = "0000000000-24-000001"
    submissions = {
        "cik": "21344",
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "filingDate": ["2024-02-20"],
                "acceptanceDateTime": ["2024-02-20T16:30:00Z"],
                "reportDate": ["2023-12-31"],
                "form": ["10-K"],
                "primaryDocument": ["ko-20231231.htm"],
            },
            "files": [],
        }
    }
    companyfacts = {
        "cik": "21344",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                                "val": 1000,
                                "accn": accession,
                                "form": "10-K",
                                "filed": "2024-02-20",
                            }
                        ]
                    }
                }
            }
        }
    }

    def initial_fetch(url: str) -> bytes:
        if "companyfacts" in url:
            return json.dumps(companyfacts).encode()
        if "submissions" in url:
            return json.dumps(submissions).encode()
        if "Archives" in url:
            return b"<html><body>annual filing</body></html>"
        raise AssertionError(url)

    with connect(tmp_path / "sec_document.sqlite") as conn:
        bootstrap_stage4(conn, bundle)
        load_current_universe(conn, load_policy(UNIVERSE_POLICY))
        conn.execute(
            "DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'"
        )
        conn.commit()
        first = sync_sec_fundamentals(
            conn,
            bundle,
            as_of="2024-12-31",
            fetch=initial_fetch,
        )
        assert first["failures"] == []
        snapshot = conn.execute('''SELECT seal_relative_path,cache_manifest_json
            FROM consumer_defensive_sec_cache_snapshot
            WHERE asof_date='2024-12-31' ''').fetchone()
        sealed_entry = next(
            row for row in json.loads(str(snapshot[1]))
            if str(row['logical_path']).startswith('filings/')
        )
        (tmp_path / 'sec_cache' / str(snapshot[0]) /
         str(sealed_entry['object_path'])).unlink()

        monkeypatch.setenv("CONSUMER_DEFENSIVE_CACHE_ONLY", "yes")
        called = False

        def forbidden_fetch(_: str) -> bytes:
            nonlocal called
            called = True
            raise AssertionError("network callback must not run in cache-only mode")

        with pytest.raises(RuntimeError, match='seal failed verification'):
            sync_sec_fundamentals(
                conn,bundle,as_of='2024-12-31',fetch=forbidden_fetch,
            )
        assert called is False


def test_fx_sync_quarantines_spike_ignores_physical_units_and_preserves_unknowns(
    tmp_path: Path,
) -> None:
    bundle = _fx_bundle(tmp_path)
    values = [0.001] * 6 + [0.2]
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "CLPUSD=X"},
                    "timestamp": [_epoch(f"2024-01-{day:02d}") for day in range(1, 8)],
                    "indicators": {"quote": [{"close": values}]},
                }
            ]
        }
    }
    calls = 0

    def fetch(_: str) -> bytes:
        nonlocal calls
        calls += 1
        return json.dumps(payload).encode()

    with connect(tmp_path / "fx.sqlite") as conn:
        bootstrap_stage4(conn, bundle)
        for position, unit in enumerate(("CLP", "GAL", "XYZ"), start=1):
            conn.execute(
                """INSERT INTO fact_sec_xbrl_fact_raw(
                       ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,unit,
                       period_start,period_end,filed_date,accepted_at,form_type,frame,dimensions_json,
                       source_id,source_detail,created_at
                   ) VALUES(
                       'TEST',NULL,NULL,'us-gaap',?,'1',1,?,NULL,'2023-12-31','2024-01-01',
                       '2024-01-01T00:00:00Z','10-K',NULL,'{}','sec_companyfacts','unit-test',
                       '2024-01-01T00:00:00Z'
                   )""",
                (f"Concept{position}", unit),
            )

        result = sync_fx_rates(
            conn,
            bundle,
            start="2024-01-01",
            end="2024-01-07",
            fetch=fetch,
        )
        assert calls == 1
        assert result["currencies"] == ["CLP"]
        assert result["ignored_non_monetary_units"] == ["GAL"]
        assert result["unknown_three_letter_units"] == ["XYZ"]
        assert result["rows_written"] == 7
        assert result["quarantined_rows"] == 1
        assert result["cache_manifest"]["files"] == 1

        rows = conn.execute(
            "SELECT rate_date,quality_status FROM fact_fx_rate ORDER BY rate_date"
        ).fetchall()
        assert [str(row["quality_status"]) for row in rows[:-1]] == ["usable"] * 6
        assert tuple(rows[-1]) == ("2024-01-07", "quarantined")
        assert _fx_rate(conn, "CLP", "2024-01-01", "2024-01-07", True) == pytest.approx(0.001)
        assert _fx_rate(conn, "CLP", None, "2024-01-07", False) == pytest.approx(0.001)


def test_fx_partial_refresh_uses_prior_context_for_identical_quality_decisions(
    tmp_path: Path,
) -> None:
    bundle = _fx_bundle(tmp_path)
    values = [0.001] * 10 + [0.2, 0.001]
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "CLPUSD=X"},
                    "timestamp": [
                        _epoch(f"2024-01-{day:02d}") for day in range(1, 13)
                    ],
                    "indicators": {"quote": [{"close": values}]},
                }
            ]
        }
    }

    def run(path: Path, start: str) -> list[tuple[str, str, str]]:
        with connect(path) as conn:
            bootstrap_stage4(conn, bundle)
            conn.execute(
                """INSERT INTO fact_sec_xbrl_fact_raw(
                       ticker,cik,accession_number,taxonomy,concept,value_text,
                       numeric_value,unit,period_start,period_end,filed_date,
                       accepted_at,form_type,frame,dimensions_json,source_id,
                       source_detail,created_at
                   ) VALUES(
                       'TEST',NULL,NULL,'us-gaap','Revenue','1',1,'CLP',
                       '2023-01-01','2023-12-31','2024-01-01',
                       '2024-01-01T00:00:00Z','10-K',NULL,'{}',
                       'sec_companyfacts','unit-test','2024-01-01T00:00:00Z'
                   )"""
            )
            sync_fx_rates(
                conn,
                bundle,
                start=start,
                end="2024-01-12",
                fetch=lambda _: json.dumps(payload).encode(),
            )
            return [
                (str(row[0]), str(row[1]), str(row[2]))
                for row in conn.execute(
                    """SELECT rate_date,quality_status,quality_reason
                       FROM fact_fx_rate WHERE rate_date>='2024-01-09'
                       ORDER BY rate_date"""
                )
            ]

    full = run(tmp_path / "fx_full.sqlite", "2024-01-01")
    partial = run(tmp_path / "fx_partial.sqlite", "2024-01-09")

    assert partial == full
    assert full[2][0:2] == ("2024-01-11", "quarantined")


def _insert_clp_requirement(conn) -> None:
    conn.execute(
        """INSERT INTO fact_sec_xbrl_fact_raw(
               ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,unit,
               period_start,period_end,filed_date,accepted_at,form_type,frame,
               dimensions_json,source_id,source_detail,created_at
           ) VALUES(
               'TEST',NULL,NULL,'us-gaap','Revenue','1',1,'CLP',
               '2023-01-01','2023-12-31','2024-01-01',
               '2024-01-01T00:00:00Z','10-K',NULL,'{}',
               'sec_companyfacts','unit-test','2024-01-01T00:00:00Z'
           )"""
    )


@pytest.mark.parametrize(
    'mutator',
    [
        lambda result: result.pop('meta'),
        lambda result: result['meta'].update({'symbol': 'EURUSD=X'}),
        lambda result: result.update({'timestamp': result['timestamp'][:-1]}),
        lambda result: result['indicators']['quote'][0].update({'close': [float('nan')]}),
        lambda result: result['indicators']['quote'][0].update({'close': [0.0]}),
        lambda result: result.update({'timestamp': [_epoch('2024-02-01')]}),
    ],
    ids=['missing-symbol', 'wrong-symbol', 'length', 'non-finite', 'non-positive', 'range'],
)
def test_invalid_fx_payload_preserves_last_good_cache_and_database(
    tmp_path: Path, mutator,
) -> None:
    bundle = _fx_bundle(tmp_path)
    path = tmp_path / 'fx-invalid.sqlite'
    cache_path = (
        tmp_path / 'fx_cache' / 'CLPUSD_X_2024-01-01_2024-01-07.json'
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    last_good = b'{"last":"good"}'
    cache_path.write_bytes(last_good)
    result = {
        'meta': {'symbol': 'CLPUSD=X'},
        'timestamp': [_epoch('2024-01-03')],
        'indicators': {'quote': [{'close': [0.001]}]},
    }
    mutator(result)
    payload = {'chart': {'result': [result], 'error': None}}
    with connect(path) as conn:
        bootstrap_stage4(conn, bundle)
        _insert_clp_requirement(conn)
        conn.execute(
            """INSERT INTO fact_fx_rate(
                   base_currency,quote_currency,rate_date,source_id,rate,raw_rate,
                   quality_status,quality_reason,created_at)
                   VALUES('CLP','USD','2024-01-03','yahoo_fx_rates',0.002,0.002,
                      'usable','last-good','2024-01-03')"""
        )
        sync = sync_fx_rates(
            conn, bundle, start='2024-01-01', end='2024-01-07',
            force_refresh=True, fetch=lambda _: json.dumps(payload).encode(),
        )
        assert sync['failures']
        assert cache_path.read_bytes() == last_good
        row = conn.execute(
            "SELECT rate,quality_reason FROM fact_fx_rate WHERE base_currency='CLP'"
        ).fetchone()
        assert tuple(row) == (0.002, 'last-good')


def test_corrupt_fx_cache_repairs_online_but_fails_in_cache_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _fx_bundle(tmp_path)
    bundle.payload['fx_rates']['start_date'] = '2024-01-01'
    cache_path = tmp_path / 'fx_cache' / 'CLPUSD_X_2024-01-01_2024-01-07.json'
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b'{corrupt')
    payload = {'chart': {'result': [{
        'meta': {'symbol': 'CLPUSD=X'},
        'timestamp': [_epoch('2024-01-03')],
        'indicators': {'quote': [{'close': [0.001]}]},
    }], 'error': None}}
    with connect(tmp_path / 'fx-repair.sqlite') as conn:
        bootstrap_stage4(conn, bundle)
        _insert_clp_requirement(conn)
        repaired = sync_fx_rates(
            conn, bundle, start='2024-01-01', end='2024-01-07',
            fetch=lambda _: json.dumps(payload).encode(),
        )
        assert repaired['failures'] == []
        assert json.loads(cache_path.read_bytes()) == payload
        cache_path.write_bytes(b'{corrupt-again')
        monkeypatch.setenv('CONSUMER_DEFENSIVE_CACHE_ONLY', '1')
        replay = sync_fx_rates(
            conn, bundle, start='2024-01-01', end='2024-01-07',
            fetch=lambda _: (_ for _ in ()).throw(AssertionError('no network')),
        )
        assert 'JSONDecodeError' in replay['failures'][0]['error']
