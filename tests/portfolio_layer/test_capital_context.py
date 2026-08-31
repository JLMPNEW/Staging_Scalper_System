from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from portfolio_layer.capital.context import (
    build_capital_context,
    load_capital_context,
    validate_capital_context,
    write_capital_context_immutable,
)


SOURCE_SHA = "a" * 64


def _context() -> dict[str, object]:
    return build_capital_context(
        account_aum_usd="500000.00",
        active_sector_count=8,
        sector_cap_fraction="0.125",
        asof_date="2026-08-27",
        source_id="user_confirmed_planning_aum",
        source_sha256=SOURCE_SHA,
    )


def _rehash(payload: dict[str, object]) -> None:
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    payload["payload_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_build_is_deterministic_and_uses_exact_decimal_arithmetic() -> None:
    first = _context()
    second = _context()

    assert first == second
    assert first["account_aum_usd"] == "500000.00"
    assert first["sector_cap_fraction"] == "0.125"
    assert first["sector_cap_notional_usd"] == "62500.00"
    assert first["equal_split_reference"] == {"numerator": 1, "denominator": 8}
    assert first["artifact_role"] == "report_only_capital_context"
    assert first["portfolio_write_performed"] is False
    assert validate_capital_context(first) == first


def test_validator_reproduces_arithmetic_even_with_a_valid_self_hash() -> None:
    tampered = copy.deepcopy(_context())
    tampered["sector_cap_notional_usd"] = "62500.01"
    _rehash(tampered)

    with pytest.raises(ValueError, match="does not equal AUM times cap fraction"):
        validate_capital_context(tampered)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"account_aum_usd": "0"}, "must be positive"),
        ({"account_aum_usd": "1.001"}, "smaller than one cent"),
        ({"active_sector_count": 0}, "must be in"),
        ({"active_sector_count": True}, "must be an integer"),
        ({"sector_cap_fraction": "1.01"}, "must be in"),
        ({"sector_cap_fraction": "0.1234567890123"}, "cannot exceed"),
        ({"asof_date": "2026-8-27"}, "canonical ISO date"),
        ({"source_id": "User supplied"}, "source_id"),
        ({"source_sha256": "A" * 64}, "lowercase SHA-256"),
    ],
)
def test_builder_rejects_invalid_inputs(
    overrides: dict[str, object], message: str
) -> None:
    inputs: dict[str, object] = {
        "account_aum_usd": "500000.00",
        "active_sector_count": 8,
        "sector_cap_fraction": "0.125",
        "asof_date": "2026-08-27",
        "source_id": "user_confirmed_planning_aum",
        "source_sha256": SOURCE_SHA,
    }
    inputs.update(overrides)

    with pytest.raises(ValueError, match=message):
        build_capital_context(**inputs)  # type: ignore[arg-type]


def test_validator_rejects_wrong_schema_and_noncanonical_numbers() -> None:
    extra = copy.deepcopy(_context())
    extra["unexpected"] = True
    with pytest.raises(ValueError, match="wrong root schema"):
        validate_capital_context(extra)

    noncanonical = copy.deepcopy(_context())
    noncanonical["sector_cap_fraction"] = "0.1250"
    _rehash(noncanonical)
    with pytest.raises(ValueError, match="not canonical"):
        validate_capital_context(noncanonical)

    noninteger_rational = copy.deepcopy(_context())
    noninteger_rational["equal_split_reference"]["numerator"] = 1.0  # type: ignore[index]
    _rehash(noninteger_rational)
    with pytest.raises(ValueError, match="exact rational"):
        validate_capital_context(noninteger_rational)


def test_immutable_write_load_and_expected_sha_pin(tmp_path: Path) -> None:
    output = tmp_path / "capital" / "context.json"
    payload = _context()

    assert write_capital_context_immutable(output, payload) == output
    assert load_capital_context(
        output,
        expected_payload_sha256=str(payload["payload_sha256"]),
    ) == payload

    original = output.read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        write_capital_context_immutable(output, payload)
    assert output.read_bytes() == original
    assert not list(output.parent.glob(".portfolio-capital-context-*.tmp"))


def test_loader_rejects_duplicate_keys_and_wrong_sha_pin(tmp_path: Path) -> None:
    output = tmp_path / "context.json"
    output.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicates key"):
        load_capital_context(output)

    output.unlink()
    write_capital_context_immutable(output, _context())
    with pytest.raises(ValueError, match="expected SHA-256 pin"):
        load_capital_context(output, expected_payload_sha256="b" * 64)


def test_cli_build_and_validate_round_trip(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    builder = project_root / "portfolio_layer" / "capital" / "build_capital_context.py"
    validator = (
        project_root / "portfolio_layer" / "capital" / "validate_capital_context.py"
    )
    output = tmp_path / "capital_context_v1.json"
    build = subprocess.run(
        [
            sys.executable,
            str(builder),
            "--account-aum-usd",
            "500000.00",
            "--active-sector-count",
            "8",
            "--sector-cap-fraction",
            "0.125",
            "--asof-date",
            "2026-08-27",
            "--source-id",
            "user_confirmed_planning_aum",
            "--source-sha256",
            SOURCE_SHA,
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    build_result = json.loads(build.stdout)
    assert build_result["acceptance"] == "PASS"
    assert build_result["sector_cap_notional_usd"] == "62500.00"

    validate = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--input",
            str(output),
            "--expected-sha256",
            build_result["payload_sha256"],
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validate.returncode == 0, validate.stderr
    assert json.loads(validate.stdout)["acceptance"] == "PASS"
