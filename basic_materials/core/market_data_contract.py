"""Governed Stage 3 market-instrument and terminal-return contracts."""

from __future__ import annotations

from collections import Counter
import csv
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.db import assert_database_identity, utc_now


class MarketDataContractError(ValueError):
    """Raised when a Stage 3 policy, manifest, or governed CSV is invalid."""


MARKET_INSTRUMENT_COLUMNS = (
    "role_key",
    "instrument_key",
    "instrument_role",
    "model_ticker",
    "security_scope",
    "event_key",
    "provider_source_id",
    "provider_database",
    "provider_symbol",
    "provider_asset_id",
    "provider_first_quoted_date",
    "provider_last_quoted_date",
    "expected_start_date",
    "expected_end_date",
    "trading_currency",
    "required_for_stage3",
    "required_for_current_gate",
    "evidence_label",
    "review_status",
    "reviewed_on",
    "notes",
)

TERMINAL_RETURN_RULE_COLUMNS = (
    "event_key",
    "outcome_class",
    "cash_weight",
    "stock_weight",
    "bankruptcy_distribution_value",
    "distribution_currency",
    "otc_continuation_symbol",
    "fractional_share_treatment",
    "max_reference_lag_calendar_days",
    "rule_status",
    "source_id",
    "source_url",
    "source_document_date",
    "evidence_label",
    "review_status",
    "reviewed_on",
    "notes",
)

CSV_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "market_instruments": MARKET_INSTRUMENT_COLUMNS,
    "terminal_return_rules": TERMINAL_RETURN_RULE_COLUMNS,
}

ALLOWED_ROLES = {
    "current_universe",
    "historical_pilot",
    "sector_benchmark",
    "broad_benchmark",
    "terminal_successor",
}
ALLOWED_OUTCOME_CLASSES = {
    "fixed_cash",
    "stock_conversion",
    "mixed_prorated",
    "bankruptcy_distribution",
    "otc_continuation",
}
_TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,11}$")
_PROVIDER_SYMBOL = re.compile(r"^[A-Z0-9.]+(?:-[0-9]{6})?$")


@dataclass(frozen=True)
class MarketFileContract:
    name: str
    path: str
    source_id: str
    expected_rows: int
    unique_key: str


@dataclass(frozen=True)
class MarketDataPolicy:
    path: Path
    policy_version: str
    contract_as_of_date: str
    review_status: str
    files: Mapping[str, MarketFileContract]
    expected_role_counts: Mapping[str, int]
    expected_unique_instruments: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class MarketManifestEntry:
    name: str
    path: Path
    source_id: str
    sha256: str
    byte_size: int
    row_count: int
    unique_key: str


@dataclass(frozen=True)
class MarketDataManifest:
    manifest_version: int
    artifact_id: str
    policy_version: str
    contract_as_of_date: str
    policy_sha256: str
    path: Path
    checksum: str
    artifacts: Mapping[str, MarketManifestEntry]


@dataclass(frozen=True)
class MarketDataBundle:
    market_instruments: tuple[Mapping[str, str], ...]
    terminal_return_rules: tuple[Mapping[str, str], ...]

    def rows(self, name: str) -> tuple[Mapping[str, str], ...]:
        return getattr(self, name)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "market_instrument_role_rows": len(self.market_instruments),
            "unique_market_instruments": len(
                {row["instrument_key"] for row in self.market_instruments}
            ),
            "terminal_return_rule_rows": len(self.terminal_return_rules),
            "role_counts": dict(
                sorted(Counter(row["instrument_role"] for row in self.market_instruments).items())
            ),
            "terminal_rule_status_counts": dict(
                sorted(Counter(row["rule_status"] for row in self.terminal_return_rules).items())
            ),
        }


@dataclass(frozen=True)
class MarketContractLoadStats:
    policy_version: str
    manifest_checksum: str
    unique_instruments: int
    role_rows: int
    terminal_rules: int
    raw_payloads: int
    role_counts: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["role_counts"] = dict(self.role_counts)
        return payload


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarketDataContractError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        raise MarketDataContractError(
            f"Invalid keys for {context}; missing={sorted(missing)}, unexpected={sorted(extra)}"
        )


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MarketDataContractError(f"{context} must be an integer >= {minimum}")
    return value


def _iso_date(value: str, context: str, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if not text and not required:
        return ""
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise MarketDataContractError(f"{context} must be ISO YYYY-MM-DD") from exc
    return text


def _number(value: str, context: str, *, required: bool = True) -> float | None:
    text = str(value or "").strip()
    if not text and not required:
        return None
    try:
        result = float(text)
    except ValueError as exc:
        raise MarketDataContractError(f"{context} must be numeric") from exc
    if not 0 <= result <= 1:
        raise MarketDataContractError(f"{context} must be between 0 and 1")
    return result


def _read_csv(path: Path, columns: tuple[str, ...], expected_rows: int) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != columns:
            raise MarketDataContractError(
                f"{path.name} columns differ from contract; expected={columns}, actual={reader.fieldnames}"
            )
        rows = []
        for row_number, raw in enumerate(reader, start=2):
            if None in raw or any(value is None for value in raw.values()):
                raise MarketDataContractError(f"{path.name} row {row_number} is malformed")
            rows.append({str(key): str(value).strip() for key, value in raw.items()})
    if len(rows) != expected_rows:
        raise MarketDataContractError(
            f"{path.name} expected {expected_rows} rows and found {len(rows)}"
        )
    return rows


def _simple_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def load_market_data_policy(path: str | Path) -> MarketDataPolicy:
    policy_path = Path(path).resolve()
    root = _mapping(yaml.safe_load(policy_path.read_text(encoding="utf-8")), "market policy")
    expected_root = {
        "policy_version",
        "contract_as_of_date",
        "model_family",
        "sector",
        "review_status",
        "files",
        "expected_role_counts",
        "expected_unique_instruments",
        "provider",
        "history",
        "benchmarks",
        "coverage",
        "features",
        "terminal_returns",
        "provider_symbol_overrides",
        "required_flags",
    }
    _exact_keys(root, expected_root, "market policy")
    if root["policy_version"] != "basic_materials_market_data_policy_v1":
        raise MarketDataContractError("Unsupported market-data policy_version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise MarketDataContractError("Market-data model family or sector is invalid")
    as_of = _iso_date(str(root["contract_as_of_date"]), "contract_as_of_date")
    if root["review_status"] != "approved_stage3_instrument_contract":
        raise MarketDataContractError("Market-data review_status is invalid")

    files_raw = _mapping(root["files"], "files")
    if set(files_raw) != set(CSV_COLUMNS):
        raise MarketDataContractError("files must define the two Stage 3 governed CSVs")
    files: dict[str, MarketFileContract] = {}
    for name, value in files_raw.items():
        item = _mapping(value, f"files.{name}")
        _exact_keys(item, {"path", "source_id", "expected_rows", "unique_key"}, f"files.{name}")
        contract = MarketFileContract(
            name=name,
            path=str(item["path"]),
            source_id=str(item["source_id"]),
            expected_rows=_integer(item["expected_rows"], f"files.{name}.expected_rows"),
            unique_key=str(item["unique_key"]),
        )
        if not contract.path.startswith("system_csvs/"):
            raise MarketDataContractError(f"files.{name}.path must be package-owned")
        if contract.unique_key not in CSV_COLUMNS[name]:
            raise MarketDataContractError(f"files.{name}.unique_key is invalid")
        files[name] = contract

    role_counts = {
        str(key): _integer(value, f"expected_role_counts.{key}")
        for key, value in _mapping(root["expected_role_counts"], "expected_role_counts").items()
    }
    if set(role_counts) != ALLOWED_ROLES or sum(role_counts.values()) != files["market_instruments"].expected_rows:
        raise MarketDataContractError("Expected role counts are incomplete or do not sum to the file contract")
    if role_counts["current_universe"] != 134 or role_counts["historical_pilot"] != 20:
        raise MarketDataContractError("Current and historical Stage 3 role counts must remain 134 and 20")
    if files["terminal_return_rules"].expected_rows != 20:
        raise MarketDataContractError("Stage 3 must govern exactly 20 terminal-return rules")

    provider = _mapping(root["provider"], "provider")
    _exact_keys(
        provider,
        {
            "source_id",
            "databases",
            "adjustment_basis",
            "stock_price_adjustment",
            "raw_price_adjustment",
            "provider_asset_id_required",
            "ticker_only_join_forbidden",
            "snapshot_fence_required",
        },
        "provider",
    )
    if (
        provider.get("source_id") != "norgate_us_equities_total_return"
        or provider.get("adjustment_basis") != "norgate_total_return"
        or provider.get("stock_price_adjustment") != "TOTALRETURN"
        or provider.get("raw_price_adjustment") != "NONE"
        or provider.get("provider_asset_id_required") is not True
        or provider.get("ticker_only_join_forbidden") is not True
        or provider.get("snapshot_fence_required") is not True
        or tuple(provider.get("databases") or ()) != ("US Equities", "US Equities Delisted")
    ):
        raise MarketDataContractError("Provider contract must remain stable-ID Norgate total return")
    history = _mapping(root["history"], "history")
    _exact_keys(history, {"history_start", "first_scoring_date", "current_end_is_run_asof"}, "history")
    _iso_date(str(history.get("history_start", "")), "history.history_start")
    _iso_date(str(history.get("first_scoring_date", "")), "history.first_scoring_date")
    if history.get("current_end_is_run_asof") is not True:
        raise MarketDataContractError("Current market coverage must end at the run as-of date")
    benchmarks = _mapping(root["benchmarks"], "benchmarks")
    _exact_keys(
        benchmarks,
        {"sector", "broad", "trading_calendar_role", "trading_calendar_code"},
        "benchmarks",
    )
    if (
        _mapping(benchmarks["sector"], "benchmarks.sector")
        != {"ticker": "XLB", "role_type": "sector_benchmark"}
        or _mapping(benchmarks["broad"], "benchmarks.broad")
        != {"ticker": "SPY", "role_type": "broad_benchmark"}
        or benchmarks["trading_calendar_role"] != "broad_benchmark"
        or benchmarks["trading_calendar_code"] != "XNYS_PROXY_SPY"
    ):
        raise MarketDataContractError("Benchmark and trading-calendar contract is invalid")
    coverage = _mapping(root["coverage"], "coverage")
    _exact_keys(
        coverage,
        {
            "start_tolerance_calendar_days",
            "active_max_staleness_calendar_days",
            "historical_end_tolerance_calendar_days",
            "minimum_rows_partial",
            "minimum_rows_full",
            "first_snapshot_minimum_observations",
            "maximum_missing_session_ratio",
            "missing_session_warning_ratio",
            "maximum_consecutive_missing_sessions",
            "current_gate_minimum_ratio",
            "recent_listing_short_history_is_rank_ready",
            "sparse_history_rank_minimum_observations",
            "sparse_history_rank_maximum_missing_session_ratio",
            "sparse_history_rank_maximum_consecutive_missing_sessions",
        },
        "coverage",
    )
    for name in (
        "start_tolerance_calendar_days",
        "active_max_staleness_calendar_days",
        "historical_end_tolerance_calendar_days",
        "minimum_rows_partial",
        "minimum_rows_full",
        "first_snapshot_minimum_observations",
        "maximum_consecutive_missing_sessions",
        "sparse_history_rank_minimum_observations",
        "sparse_history_rank_maximum_consecutive_missing_sessions",
    ):
        _integer(coverage[name], f"coverage.{name}")
    for name in (
        "maximum_missing_session_ratio",
        "missing_session_warning_ratio",
        "current_gate_minimum_ratio",
        "sparse_history_rank_maximum_missing_session_ratio",
    ):
        value = coverage[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise MarketDataContractError(f"coverage.{name} must be between zero and one")
    if (
        coverage["minimum_rows_partial"] > coverage["minimum_rows_full"]
        or coverage["sparse_history_rank_minimum_observations"] < coverage["minimum_rows_full"]
        or coverage["current_gate_minimum_ratio"] != 0.95
        or coverage["recent_listing_short_history_is_rank_ready"] is not True
    ):
        raise MarketDataContractError("Coverage rank-readiness thresholds are invalid")
    features = _mapping(root["features"], "features")
    _exact_keys(
        features,
        {
            "return_windows",
            "momentum_12m_days",
            "momentum_skip_days",
            "volatility_days",
            "drawdown_days",
            "short_moving_average_days",
            "long_moving_average_days",
            "average_dollar_volume_days",
            "beta_days",
            "beta_residual_momentum_days",
            "feature_definition_version",
        },
        "features",
    )
    if (
        tuple(features["return_windows"]) != (21, 63, 126, 252)
        or features["feature_definition_version"] != "basic_materials_market_features_v1"
    ):
        raise MarketDataContractError("Market feature definition contract is invalid")
    terminal = _mapping(root["terminal_returns"], "terminal_returns")
    _exact_keys(
        terminal,
        {
            "required_event_count",
            "allowed_outcome_classes",
            "max_reference_lag_calendar_days",
            "require_exact_historical_final_quote",
            "unresolved_distribution_is_explicit_exclusion",
            "historical_calibration_activation_allowed",
        },
        "terminal_returns",
    )
    if (
        terminal.get("required_event_count") != 20
        or set(terminal.get("allowed_outcome_classes") or ()) != ALLOWED_OUTCOME_CLASSES
        or terminal.get("max_reference_lag_calendar_days") != 7
        or terminal.get("require_exact_historical_final_quote") is not True
        or terminal.get("unresolved_distribution_is_explicit_exclusion") is not True
        or terminal.get("historical_calibration_activation_allowed") is not False
    ):
        raise MarketDataContractError("Terminal-return policy must remain complete and fail-closed")
    overrides = _mapping(root["provider_symbol_overrides"], "provider_symbol_overrides")
    if set(overrides) != {"terminal_ZEUS_20260213"}:
        raise MarketDataContractError("Only the reviewed ZEUS successor override is allowed")
    zeus_override = _mapping(overrides["terminal_ZEUS_20260213"], "ZEUS override")
    _exact_keys(
        zeus_override,
        {"economic_successor_ticker", "provider_symbol", "provider_asset_id", "effective_date", "reason"},
        "ZEUS override",
    )
    if (
        zeus_override["economic_successor_ticker"] != "RYI"
        or zeus_override["provider_symbol"] != "RYZ"
        or zeus_override["provider_asset_id"] != "1606887"
        or _iso_date(str(zeus_override["effective_date"]), "ZEUS override effective_date") != "2026-02-24"
    ):
        raise MarketDataContractError("ZEUS successor provider override is invalid")
    flags = _mapping(root["required_flags"], "required_flags")
    if flags != {
        "required_for_stage3": True,
        "current_and_benchmark_required_for_current_gate": True,
        "calibration_eligible": False,
    }:
        raise MarketDataContractError("Stage 3 activation flags are invalid")
    return MarketDataPolicy(
        path=policy_path,
        policy_version=str(root["policy_version"]),
        contract_as_of_date=as_of,
        review_status=str(root["review_status"]),
        files=files,
        expected_role_counts=role_counts,
        expected_unique_instruments=_integer(
            root["expected_unique_instruments"], "expected_unique_instruments", minimum=1
        ),
        payload=root,
    )


def validate_market_data_manifest(
    path: str | Path,
    policy: MarketDataPolicy,
    package_root: str | Path,
) -> MarketDataManifest:
    manifest_path = Path(path).resolve()
    payload = manifest_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "market manifest")
    _exact_keys(
        root,
        {
            "manifest_version",
            "artifact_id",
            "policy_version",
            "policy_sha256",
            "contract_as_of_date",
            "state",
            "calibration_eligible",
            "artifacts",
        },
        "market manifest",
    )
    if root["manifest_version"] != 1:
        raise MarketDataContractError("Unsupported market manifest_version")
    if root["artifact_id"] != "basic_materials_market_data_contract_v1":
        raise MarketDataContractError("Unexpected market manifest artifact_id")
    if root["policy_version"] != policy.policy_version or root["contract_as_of_date"] != policy.contract_as_of_date:
        raise MarketDataContractError("Market manifest does not match policy")
    policy_sha = hashlib.sha256(policy.path.read_bytes()).hexdigest()
    if str(root["policy_sha256"]).lower() != policy_sha:
        raise MarketDataContractError("Market policy fingerprint differs from manifest")
    if root["state"] != "stage3_contract_reviewed_calibration_blocked" or root["calibration_eligible"] is not False:
        raise MarketDataContractError("Market manifest must remain calibration blocked")

    artifacts_raw = _mapping(root["artifacts"], "artifacts")
    if set(artifacts_raw) != set(policy.files):
        raise MarketDataContractError("Market manifest artifact set differs from policy")
    package = Path(package_root).resolve()
    artifacts: dict[str, MarketManifestEntry] = {}
    for name, contract in policy.files.items():
        item = _mapping(artifacts_raw[name], f"artifacts.{name}")
        _exact_keys(
            item,
            {"path", "source_id", "sha256", "byte_size", "row_count", "unique_key"},
            f"artifacts.{name}",
        )
        resolved = (manifest_path.parent / str(item["path"])).resolve()
        expected = (package / contract.path).resolve()
        if resolved != expected or str(item["source_id"]) != contract.source_id:
            raise MarketDataContractError(f"artifacts.{name} path or source differs from policy")
        raw = resolved.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if (
            digest != str(item["sha256"]).lower()
            or len(raw) != _integer(item["byte_size"], f"artifacts.{name}.byte_size")
            or _integer(item["row_count"], f"artifacts.{name}.row_count") != contract.expected_rows
            or str(item["unique_key"]) != contract.unique_key
        ):
            raise MarketDataContractError(f"artifacts.{name} fingerprint differs from manifest")
        rows = _read_csv(resolved, CSV_COLUMNS[name], contract.expected_rows)
        keys = [row[contract.unique_key] for row in rows]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise MarketDataContractError(f"artifacts.{name} unique key is blank or duplicated")
        artifacts[name] = MarketManifestEntry(
            name=name,
            path=resolved,
            source_id=contract.source_id,
            sha256=digest,
            byte_size=len(raw),
            row_count=len(rows),
            unique_key=contract.unique_key,
        )
    return MarketDataManifest(
        manifest_version=1,
        artifact_id=str(root["artifact_id"]),
        policy_version=policy.policy_version,
        contract_as_of_date=policy.contract_as_of_date,
        policy_sha256=policy_sha,
        path=manifest_path,
        checksum=hashlib.sha256(payload).hexdigest(),
        artifacts=artifacts,
    )


def _flag(value: str, context: str) -> int:
    if value not in {"0", "1"}:
        raise MarketDataContractError(f"{context} must be 0 or 1")
    return int(value)


def _nonnegative_number(
    value: str,
    context: str,
    *,
    required: bool = True,
) -> float | None:
    text = str(value or "").strip()
    if not text and not required:
        return None
    try:
        result = float(text)
    except ValueError as exc:
        raise MarketDataContractError(f"{context} must be numeric") from exc
    if result < 0:
        raise MarketDataContractError(f"{context} must be non-negative")
    return result


def _secure_primary_url(value: str, context: str) -> str:
    parsed = urlparse(str(value).strip())
    if parsed.scheme != "https" or parsed.hostname not in {"sec.gov", "www.sec.gov"}:
        raise MarketDataContractError(f"{context} must be an HTTPS SEC URL")
    return str(value).strip()


def _validate_market_instruments(
    rows: Sequence[Mapping[str, str]],
    *,
    policy: MarketDataPolicy,
    universe_rows: Sequence[Mapping[str, str]],
    historical_rows: Sequence[Mapping[str, str]],
    terminal_rows: Sequence[Mapping[str, str]],
) -> None:
    provider = _mapping(policy.payload["provider"], "provider")
    allowed_databases = set(provider["databases"])
    provider_source_id = str(provider["source_id"])
    role_counts = Counter(row["instrument_role"] for row in rows)
    if dict(role_counts) != dict(policy.expected_role_counts):
        raise MarketDataContractError(
            f"Market role counts differ from policy: expected={dict(policy.expected_role_counts)}, "
            f"actual={dict(role_counts)}"
        )
    if len({row["instrument_key"] for row in rows}) != policy.expected_unique_instruments:
        raise MarketDataContractError("Unique market-instrument count differs from policy")

    current_tickers = {row["ticker"].upper() for row in universe_rows}
    historical_by_ticker = {row["historical_ticker"].upper(): row for row in historical_rows}
    terminal_by_key = {row["event_key"]: row for row in terminal_rows}
    expected_successor_keys = {
        event_key
        for event_key, row in terminal_by_key.items()
        if row["successor_ticker"] and row["terminal_type"] in {"stock_merger", "stock_acquisition"}
    }
    instrument_identity: dict[str, tuple[str, ...]] = {}
    provider_identity: dict[tuple[str, str], str] = {}
    seen_current: set[str] = set()
    seen_historical: set[str] = set()
    seen_successors: set[str] = set()

    for index, row in enumerate(rows, start=2):
        context = f"market instruments row {index}"
        role = row["instrument_role"]
        if role not in ALLOWED_ROLES:
            raise MarketDataContractError(f"{context} has invalid role {role!r}")
        ticker = row["model_ticker"].upper()
        if not _TICKER.fullmatch(ticker):
            raise MarketDataContractError(f"{context} has invalid model_ticker")
        symbol = row["provider_symbol"].upper()
        if not _PROVIDER_SYMBOL.fullmatch(symbol):
            raise MarketDataContractError(f"{context} has invalid provider_symbol")
        asset_id = row["provider_asset_id"]
        if not asset_id.isdigit() or row["instrument_key"] != f"{provider_source_id}:{asset_id}":
            raise MarketDataContractError(f"{context} has invalid stable provider identity")
        if row["provider_source_id"] != provider_source_id:
            raise MarketDataContractError(f"{context} has the wrong provider source")
        if row["provider_database"] not in allowed_databases:
            raise MarketDataContractError(f"{context} has an unapproved provider database")
        first = _iso_date(row["provider_first_quoted_date"], f"{context}.provider_first_quoted_date")
        last = _iso_date(
            row["provider_last_quoted_date"],
            f"{context}.provider_last_quoted_date",
            required=False,
        )
        expected_start = _iso_date(row["expected_start_date"], f"{context}.expected_start_date")
        expected_end = _iso_date(
            row["expected_end_date"], f"{context}.expected_end_date", required=False
        )
        reviewed_on = _iso_date(row["reviewed_on"], f"{context}.reviewed_on")
        if last and last < first:
            raise MarketDataContractError(f"{context} has provider_last_quoted_date before first")
        if expected_start < first or (expected_end and expected_end < expected_start):
            raise MarketDataContractError(f"{context} has an invalid expected coverage window")
        if last and expected_end and expected_end > last:
            raise MarketDataContractError(f"{context} expects data after provider history ends")
        if reviewed_on != policy.contract_as_of_date:
            raise MarketDataContractError(f"{context} review date differs from the contract")
        if row["review_status"] != "approved_stage3_market_instrument":
            raise MarketDataContractError(f"{context} is not approved")
        if row["evidence_label"] != "provider_snapshot_reviewed":
            raise MarketDataContractError(f"{context} lacks reviewed provider evidence")
        if _flag(row["required_for_stage3"], f"{context}.required_for_stage3") != 1:
            raise MarketDataContractError(f"{context} must be required for Stage 3")
        current_gate = _flag(row["required_for_current_gate"], f"{context}.required_for_current_gate")
        should_gate = role in {"current_universe", "sector_benchmark", "broad_benchmark"}
        if bool(current_gate) != should_gate:
            raise MarketDataContractError(f"{context} has an invalid current-gate flag")
        identity = (
            row["provider_source_id"],
            asset_id,
            symbol,
            row["provider_database"],
            first,
            last,
            row["trading_currency"],
        )
        prior = instrument_identity.setdefault(row["instrument_key"], identity)
        if prior != identity:
            raise MarketDataContractError(f"{context} changes metadata for a shared instrument")
        provider_key = (row["provider_source_id"], asset_id)
        prior_key = provider_identity.setdefault(provider_key, row["instrument_key"])
        if prior_key != row["instrument_key"]:
            raise MarketDataContractError(f"{context} aliases one provider asset to two instruments")

        event_key = row["event_key"]
        if role == "current_universe":
            if ticker not in current_tickers or row["role_key"] != f"current:{ticker}" or event_key:
                raise MarketDataContractError(f"{context} does not match the current universe")
            seen_current.add(ticker)
        elif role == "historical_pilot":
            source = historical_by_ticker.get(ticker)
            if source is None or row["role_key"] != f"historical:{ticker}" or event_key:
                raise MarketDataContractError(f"{context} does not match the historical pilot")
            if symbol != source["provider_symbol"].upper() or asset_id != source["provider_asset_id"]:
                raise MarketDataContractError(f"{context} differs from Stage 2B provider identity")
            if first != source["membership_start_date"] or last != source["membership_end_date"]:
                raise MarketDataContractError(f"{context} differs from Stage 2B quoted dates")
            seen_historical.add(ticker)
        elif role == "sector_benchmark":
            expected = str(_mapping(policy.payload["benchmarks"], "benchmarks")["sector"]["ticker"])
            if ticker != expected or row["role_key"] != f"benchmark:sector:{expected}" or event_key:
                raise MarketDataContractError(f"{context} has the wrong sector benchmark")
        elif role == "broad_benchmark":
            expected = str(_mapping(policy.payload["benchmarks"], "benchmarks")["broad"]["ticker"])
            if ticker != expected or row["role_key"] != f"benchmark:broad:{expected}" or event_key:
                raise MarketDataContractError(f"{context} has the wrong broad benchmark")
        else:
            event = terminal_by_key.get(event_key)
            if (
                event is None
                or event_key not in expected_successor_keys
                or ticker != event["successor_ticker"].upper()
                or row["role_key"] != f"terminal_successor:{event_key}"
            ):
                raise MarketDataContractError(f"{context} does not match its terminal successor event")
            override = _mapping(policy.payload["provider_symbol_overrides"], "provider_symbol_overrides").get(event_key)
            expected_symbol = str((override or {}).get("provider_symbol") or event["successor_provider_symbol"] or ticker)
            expected_asset = str((override or {}).get("provider_asset_id") or "")
            if symbol != expected_symbol or (expected_asset and asset_id != expected_asset):
                raise MarketDataContractError(f"{context} violates its provider-symbol override")
            seen_successors.add(event_key)

    if seen_current != current_tickers:
        raise MarketDataContractError("Current market-role ticker set differs from the universe")
    if seen_historical != set(historical_by_ticker):
        raise MarketDataContractError("Historical market-role ticker set differs from Stage 2B")
    if seen_successors != expected_successor_keys:
        raise MarketDataContractError("Terminal-successor role set differs from terminal events")


def _validate_terminal_rules(
    rows: Sequence[Mapping[str, str]],
    *,
    policy: MarketDataPolicy,
    terminal_rows: Sequence[Mapping[str, str]],
) -> None:
    terminal_by_key = {row["event_key"]: row for row in terminal_rows}
    if {row["event_key"] for row in rows} != set(terminal_by_key):
        raise MarketDataContractError("Terminal-rule event set differs from Stage 2B terminal events")
    policy_terminal = _mapping(policy.payload["terminal_returns"], "terminal_returns")
    max_lag = int(policy_terminal["max_reference_lag_calendar_days"])
    source_id = policy.files["terminal_return_rules"].source_id
    for index, row in enumerate(rows, start=2):
        context = f"terminal rules row {index}"
        event = terminal_by_key[row["event_key"]]
        outcome = row["outcome_class"]
        if outcome not in ALLOWED_OUTCOME_CLASSES:
            raise MarketDataContractError(f"{context} has an invalid outcome class")
        cash_weight = _number(row["cash_weight"], f"{context}.cash_weight")
        stock_weight = _number(row["stock_weight"], f"{context}.stock_weight")
        distribution = _nonnegative_number(
            row["bankruptcy_distribution_value"],
            f"{context}.bankruptcy_distribution_value",
            required=False,
        )
        if cash_weight is None or stock_weight is None or cash_weight + stock_weight > 1.0000001:
            raise MarketDataContractError(f"{context} has invalid allocation weights")
        if row["source_id"] != source_id:
            raise MarketDataContractError(f"{context} has the wrong source ID")
        _secure_primary_url(row["source_url"], f"{context}.source_url")
        _iso_date(row["source_document_date"], f"{context}.source_document_date")
        if _iso_date(row["reviewed_on"], f"{context}.reviewed_on") != policy.contract_as_of_date:
            raise MarketDataContractError(f"{context} review date differs from the contract")
        if row["evidence_label"] != "primary_sec" or row["review_status"] != "approved_stage3_terminal_rule":
            raise MarketDataContractError(f"{context} lacks approved primary evidence")
        try:
            rule_lag = int(row["max_reference_lag_calendar_days"])
        except ValueError as exc:
            raise MarketDataContractError(f"{context} has invalid reference lag") from exc
        if rule_lag != max_lag:
            raise MarketDataContractError(f"{context} reference lag differs from policy")

        if outcome == "fixed_cash":
            valid = cash_weight == 1 and stock_weight == 0 and bool(event["cash_consideration"])
        elif outcome == "stock_conversion":
            valid = cash_weight == 0 and stock_weight == 1 and bool(event["successor_ticker"])
        elif outcome == "mixed_prorated":
            valid = cash_weight > 0 and stock_weight > 0 and abs(cash_weight + stock_weight - 1) < 1e-9
            valid = valid and bool(event["cash_consideration"]) and bool(event["successor_ticker"])
        elif outcome == "bankruptcy_distribution":
            valid = cash_weight == 0 and stock_weight == 0
        else:
            valid = bool(row["otc_continuation_symbol"])
        if not valid:
            raise MarketDataContractError(f"{context} conflicts with the Stage 2B event terms")
        expected_status = (
            "pending_distribution_evidence"
            if outcome == "bankruptcy_distribution" and distribution is None
            else "ready_for_calculation"
        )
        if row["rule_status"] != expected_status:
            raise MarketDataContractError(f"{context} has a non-fail-closed rule status")
        if outcome in {"stock_conversion", "mixed_prorated"}:
            if row["fractional_share_treatment"] != "continuous_per_original_share_value":
                raise MarketDataContractError(f"{context} must use continuous fractional-share value")
        elif row["fractional_share_treatment"] != "not_applicable":
            raise MarketDataContractError(f"{context} has invalid fractional-share treatment")


def read_and_validate_market_contract(
    *,
    policy: MarketDataPolicy,
    manifest: MarketDataManifest,
    universe_path: str | Path,
    historical_membership_path: str | Path,
    terminal_events_path: str | Path,
) -> MarketDataBundle:
    """Validate all Stage 3 contract rows and their Stage 1/2 cross-references."""

    rows = {
        name: tuple(_read_csv(entry.path, CSV_COLUMNS[name], entry.row_count))
        for name, entry in manifest.artifacts.items()
    }
    universe = _simple_csv(universe_path)
    historical = _simple_csv(historical_membership_path)
    terminal = _simple_csv(terminal_events_path)
    _validate_market_instruments(
        rows["market_instruments"],
        policy=policy,
        universe_rows=universe,
        historical_rows=historical,
        terminal_rows=terminal,
    )
    _validate_terminal_rules(
        rows["terminal_return_rules"],
        policy=policy,
        terminal_rows=terminal,
    )
    return MarketDataBundle(
        market_instruments=rows["market_instruments"],
        terminal_return_rules=rows["terminal_return_rules"],
    )


def _require_active_sources(conn: sqlite3.Connection, source_ids: set[str]) -> None:
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"SELECT source_id, active FROM source_registry WHERE source_id IN ({placeholders})",
        tuple(sorted(source_ids)),
    ).fetchall()
    active = {str(row["source_id"]) for row in rows if int(row["active"]) == 1}
    missing = source_ids - active
    if missing:
        raise MarketDataContractError(
            f"Active source-registry rows are required before Stage 3 load: {sorted(missing)}"
        )


def load_market_data_contract(
    conn: sqlite3.Connection,
    *,
    policy: MarketDataPolicy,
    manifest: MarketDataManifest,
    bundle: MarketDataBundle,
) -> MarketContractLoadStats:
    """Atomically load immutable provider identities, roles, and terminal rules."""

    assert_database_identity(conn)
    if conn.in_transaction:
        raise RuntimeError("load_market_data_contract requires a clean connection")
    source_ids = {
        str(policy.payload["provider"]["source_id"]),
        *(entry.source_id for entry in manifest.artifacts.values()),
    }
    _require_active_sources(conn, source_ids)

    security_rows = conn.execute("SELECT security_id, ticker FROM dim_security").fetchall()
    securities = {str(row["ticker"]).upper(): int(row["security_id"]) for row in security_rows}
    required_security_tickers = {
        row["model_ticker"].upper()
        for row in bundle.market_instruments
        if row["instrument_role"] in {"current_universe", "historical_pilot"}
    }
    missing_securities = required_security_tickers - set(securities)
    if missing_securities:
        raise MarketDataContractError(
            "Stage 1 and Stage 2B securities must be loaded before Stage 3: "
            f"{sorted(missing_securities)}"
        )
    terminal_keys = {
        str(row[0])
        for row in conn.execute(
            "SELECT event_key FROM fact_terminal_event_reconciliation WHERE event_key IS NOT NULL"
        ).fetchall()
    }
    expected_terminal_keys = {row["event_key"] for row in bundle.terminal_return_rules}
    if terminal_keys != expected_terminal_keys:
        raise MarketDataContractError(
            "Loaded Stage 2B terminal-event keys differ from the Stage 3 rule contract"
        )

    market_source = manifest.artifacts["market_instruments"].source_id
    rule_source = manifest.artifacts["terminal_return_rules"].source_id
    existing_roles = {
        str(row[0])
        for row in conn.execute(
            "SELECT role_key FROM bridge_market_instrument_role WHERE source_id = ?",
            (market_source,),
        ).fetchall()
    }
    incoming_roles = {row["role_key"] for row in bundle.market_instruments}
    if existing_roles - incoming_roles:
        raise MarketDataContractError(
            "Existing Stage 3 roles are absent from the new contract; use a reviewed migration"
        )
    existing_rules = {
        str(row[0])
        for row in conn.execute(
            "SELECT event_key FROM dim_terminal_return_rule WHERE source_id = ?",
            (rule_source,),
        ).fetchall()
    }
    if existing_rules - expected_terminal_keys:
        raise MarketDataContractError(
            "Existing terminal rules are absent from the new contract; use a reviewed migration"
        )

    now = utc_now()
    market_sha = manifest.artifacts["market_instruments"].sha256
    rule_sha = manifest.artifacts["terminal_return_rules"].sha256
    conn.execute("BEGIN IMMEDIATE")
    try:
        for entry in manifest.artifacts.values():
            conn.execute(
                """
                INSERT INTO raw_source_payloads (
                    snapshot_id, source_id, source_snapshot_date, source_path, sha256,
                    byte_size, row_count, media_type, payload, manifest_version, ingested_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'text/csv', ?, ?, ?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                (
                    f"{entry.source_id}:{entry.sha256}",
                    entry.source_id,
                    policy.contract_as_of_date,
                    str(entry.path),
                    entry.sha256,
                    entry.byte_size,
                    entry.row_count,
                    entry.path.read_bytes(),
                    policy.policy_version,
                    now,
                ),
            )

        unique_rows: dict[str, Mapping[str, str]] = {}
        for row in bundle.market_instruments:
            unique_rows.setdefault(row["instrument_key"], row)
        for row in unique_rows.values():
            conn.execute(
                """
                INSERT INTO dim_market_instrument (
                    instrument_key, provider_source_id, provider_asset_id, provider_symbol,
                    canonical_ticker, provider_database, trading_currency,
                    provider_first_quoted_date, provider_last_quoted_date, adjustment_basis,
                    contract_version, contract_sha256, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'norgate_total_return', ?, ?, ?, ?)
                ON CONFLICT(instrument_key) DO UPDATE SET
                    provider_source_id = excluded.provider_source_id,
                    provider_asset_id = excluded.provider_asset_id,
                    provider_symbol = excluded.provider_symbol,
                    canonical_ticker = excluded.canonical_ticker,
                    provider_database = excluded.provider_database,
                    trading_currency = excluded.trading_currency,
                    provider_first_quoted_date = excluded.provider_first_quoted_date,
                    provider_last_quoted_date = excluded.provider_last_quoted_date,
                    adjustment_basis = excluded.adjustment_basis,
                    contract_version = excluded.contract_version,
                    contract_sha256 = excluded.contract_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    row["instrument_key"],
                    row["provider_source_id"],
                    row["provider_asset_id"],
                    row["provider_symbol"],
                    row["model_ticker"],
                    row["provider_database"],
                    row["trading_currency"],
                    row["provider_first_quoted_date"],
                    row["provider_last_quoted_date"] or None,
                    policy.policy_version,
                    market_sha,
                    now,
                    now,
                ),
            )

        instrument_ids = {
            str(row["instrument_key"]): int(row["instrument_id"])
            for row in conn.execute(
                "SELECT instrument_id, instrument_key FROM dim_market_instrument"
            ).fetchall()
        }
        for row in bundle.market_instruments:
            role = row["instrument_role"]
            security_id = (
                securities[row["model_ticker"].upper()]
                if role in {"current_universe", "historical_pilot"}
                else None
            )
            conn.execute(
                """
                INSERT INTO bridge_market_instrument_role (
                    role_key, instrument_id, security_id, event_key, role_type, model_ticker,
                    security_scope, expected_start_date, expected_end_date,
                    required_for_stage3, required_for_current_gate, source_id,
                    contract_version, contract_sha256, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(role_key) DO UPDATE SET
                    instrument_id = excluded.instrument_id,
                    security_id = excluded.security_id,
                    event_key = excluded.event_key,
                    role_type = excluded.role_type,
                    model_ticker = excluded.model_ticker,
                    security_scope = excluded.security_scope,
                    expected_start_date = excluded.expected_start_date,
                    expected_end_date = excluded.expected_end_date,
                    required_for_stage3 = excluded.required_for_stage3,
                    required_for_current_gate = excluded.required_for_current_gate,
                    source_id = excluded.source_id,
                    contract_version = excluded.contract_version,
                    contract_sha256 = excluded.contract_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    row["role_key"],
                    instrument_ids[row["instrument_key"]],
                    security_id,
                    row["event_key"] or None,
                    role,
                    row["model_ticker"],
                    row["security_scope"],
                    row["expected_start_date"],
                    row["expected_end_date"] or None,
                    int(row["required_for_stage3"]),
                    int(row["required_for_current_gate"]),
                    market_source,
                    policy.policy_version,
                    market_sha,
                    now,
                    now,
                ),
            )

        for row in bundle.terminal_return_rules:
            distribution = _nonnegative_number(
                row["bankruptcy_distribution_value"],
                f"{row['event_key']}.bankruptcy_distribution_value",
                required=False,
            )
            conn.execute(
                """
                INSERT INTO dim_terminal_return_rule (
                    event_key, outcome_class, cash_weight, stock_weight,
                    bankruptcy_distribution_value, distribution_currency,
                    otc_continuation_symbol, fractional_share_treatment,
                    max_reference_lag_calendar_days, rule_status, source_id,
                    evidence_json, contract_version, contract_sha256,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    outcome_class = excluded.outcome_class,
                    cash_weight = excluded.cash_weight,
                    stock_weight = excluded.stock_weight,
                    bankruptcy_distribution_value = excluded.bankruptcy_distribution_value,
                    distribution_currency = excluded.distribution_currency,
                    otc_continuation_symbol = excluded.otc_continuation_symbol,
                    fractional_share_treatment = excluded.fractional_share_treatment,
                    max_reference_lag_calendar_days = excluded.max_reference_lag_calendar_days,
                    rule_status = excluded.rule_status,
                    source_id = excluded.source_id,
                    evidence_json = excluded.evidence_json,
                    contract_version = excluded.contract_version,
                    contract_sha256 = excluded.contract_sha256,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    row["event_key"],
                    row["outcome_class"],
                    float(row["cash_weight"]),
                    float(row["stock_weight"]),
                    distribution,
                    row["distribution_currency"] or None,
                    row["otc_continuation_symbol"] or None,
                    row["fractional_share_treatment"],
                    int(row["max_reference_lag_calendar_days"]),
                    row["rule_status"],
                    rule_source,
                    json.dumps(row, sort_keys=True),
                    policy.policy_version,
                    rule_sha,
                    now,
                    now,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return MarketContractLoadStats(
        policy_version=policy.policy_version,
        manifest_checksum=manifest.checksum,
        unique_instruments=len(unique_rows),
        role_rows=len(bundle.market_instruments),
        terminal_rules=len(bundle.terminal_return_rules),
        raw_payloads=len(manifest.artifacts),
        role_counts=dict(
            sorted(Counter(row["instrument_role"] for row in bundle.market_instruments).items())
        ),
    )
