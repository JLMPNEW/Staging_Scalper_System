from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from future_only_evidence.canonical_domain import validate_market_source_provenance
from future_only_evidence.protocol import file_sha256


def _csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _case(tmp_path: Path, *, extra_asset: bool = False):
    assets = [
        {
            "ticker": "AAA",
            "asset_id": "asset-aaa",
            "provider_id": "provider",
            "dataset_id": "dataset",
            "exchange_mic": "XNYS",
            "currency": "USD",
            "effective_from": "2020-01-01",
            "effective_to": "",
        },
        {
            "ticker": "XLP",
            "asset_id": "asset-xlp",
            "provider_id": "provider",
            "dataset_id": "dataset",
            "exchange_mic": "ARCX",
            "currency": "USD",
            "effective_from": "2020-01-01",
            "effective_to": "",
        },
    ]
    if extra_asset:
        assets.append(
            {
                "ticker": "BBB",
                "asset_id": "asset-bbb",
                "provider_id": "provider",
                "dataset_id": "dataset",
                "exchange_mic": "XNYS",
                "currency": "USD",
                "effective_from": "2020-01-01",
                "effective_to": "",
            }
        )
    asset_fields = list(assets[0])
    bars = [
        {
            **{key: row[key] for key in (
                "ticker",
                "asset_id",
                "provider_id",
                "dataset_id",
                "exchange_mic",
                "currency",
            )},
            "source_observation_id": f"obs-{row['ticker']}",
            "execution_at_utc": "2026-09-01T13:30:00+00:00",
        }
        for row in assets
        if row["ticker"] != "BBB"
    ]
    paths = {
        "asset_master": _csv(tmp_path / "assets.csv", asset_fields, assets),
        "total_return_bars": _csv(tmp_path / "bars.csv", list(bars[0]), bars),
        "corporate_actions": _csv(
            tmp_path / "actions.csv",
            [
                "ticker",
                "asset_id",
                "action_id",
                "action_type",
                "terminal_event_status",
                "terminal_event_reason",
                "effective_at_utc",
                "source_observation_id",
            ],
            [],
        ),
        "terminal_events": _csv(
            tmp_path / "terminals.csv",
            [
                "ticker",
                "terminal_execution_at_utc",
                "terminal_event_status",
                "terminal_event_reason",
            ],
            [],
        ),
    }
    source_hashes = {role: file_sha256(path) for role, path in paths.items()}
    attestation = {
        "market_data_export_attestation_pass": True,
        "family": "consumer_defensive",
        "source_sha256": source_hashes,
        "provider_id": "provider",
        "dataset_id": "dataset",
        "asset_ids": {row["ticker"]: row["asset_id"] for row in assets},
    }
    bundle = SimpleNamespace(
        family="consumer_defensive",
        required_currency="USD",
        benchmark_asset_ids={"XLP": "asset-xlp"},
    )
    return paths, source_hashes, attestation, bundle


def test_verified_market_attestation_drives_source_semantics(tmp_path: Path) -> None:
    paths, hashes, attestation, bundle = _case(tmp_path)
    audit = validate_market_source_provenance(
        outcome_source_paths=paths,
        market_export_attestation=attestation,
        expected_source_sha256=hashes,
        bundle=bundle,  # type: ignore[arg-type]
        expected_benchmark_ticker="XLP",
    )
    assert audit["exact_asset_census_pass"] is True
    assert audit["exact_terminal_corporate_action_census_pass"] is True


def test_extra_asset_without_raw_bar_is_rejected(tmp_path: Path) -> None:
    paths, hashes, attestation, bundle = _case(tmp_path, extra_asset=True)
    with pytest.raises(ValueError, match="exact raw-bar plus terminal-event ticker census"):
        validate_market_source_provenance(
            outcome_source_paths=paths,
            market_export_attestation=attestation,
            expected_source_sha256=hashes,
            bundle=bundle,  # type: ignore[arg-type]
            expected_benchmark_ticker="XLP",
        )


def test_headerless_empty_terminal_source_is_rejected(tmp_path: Path) -> None:
    paths, _, attestation, bundle = _case(tmp_path)
    paths["terminal_events"].write_bytes(b"")
    hashes = {role: file_sha256(path) for role, path in paths.items()}
    attestation["source_sha256"] = hashes
    with pytest.raises(ValueError, match="terminal-event source.*schema"):
        validate_market_source_provenance(
            outcome_source_paths=paths,
            market_export_attestation=attestation,
            expected_source_sha256=hashes,
            bundle=bundle,  # type: ignore[arg-type]
            expected_benchmark_ticker="XLP",
        )


@pytest.mark.parametrize(
    ("role", "old", "new"),
    [
        ("asset_master", "AAA,asset-aaa", " aaa,asset-aaa"),
        ("total_return_bars", "AAA,asset-aaa", "aaa,asset-aaa"),
    ],
)
def test_signed_market_source_tickers_are_not_normalized(
    tmp_path: Path, role: str, old: str, new: str
) -> None:
    paths, _, attestation, bundle = _case(tmp_path)
    source = paths[role]
    source.write_text(
        source.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )
    hashes = {name: file_sha256(path) for name, path in paths.items()}
    attestation["source_sha256"] = hashes
    with pytest.raises(ValueError, match="canonical uppercase ticker"):
        validate_market_source_provenance(
            outcome_source_paths=paths,
            market_export_attestation=attestation,
            expected_source_sha256=hashes,
            bundle=bundle,  # type: ignore[arg-type]
            expected_benchmark_ticker="XLP",
        )


def test_signed_market_receipt_asset_ticker_is_not_normalized(
    tmp_path: Path,
) -> None:
    paths, hashes, attestation, bundle = _case(tmp_path)
    attestation["asset_ids"]["aaa"] = attestation["asset_ids"].pop("AAA")
    with pytest.raises(ValueError, match="canonical uppercase ticker"):
        validate_market_source_provenance(
            outcome_source_paths=paths,
            market_export_attestation=attestation,
            expected_source_sha256=hashes,
            bundle=bundle,  # type: ignore[arg-type]
            expected_benchmark_ticker="XLP",
        )
