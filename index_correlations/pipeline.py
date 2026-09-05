"""Build point-in-time ETF correlations from the existing Stage 2 price cache.

The builder is read-only with respect to market data. It verifies the Stage 2
Norgate extraction manifest against SQLite, truncates rows at ``--as-of``, and
publishes only derived correlations. It never calls a data provider and never
copies prices or returns into this package.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Self, cast

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 2
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DATABASE = (
    PROJECT_ROOT / "portfolio_layer" / "output" / "cache"
    / "norgate_market_instruments.sqlite"
)
DEFAULT_SOURCE_MANIFEST = (
    PROJECT_ROOT / "portfolio_layer" / "output" / "cache"
    / "norgate_market_instruments_manifest.json"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "index_correlations"
SOURCE_TABLE = "fact_price_ohlcv"
SOURCE_ID = "norgate_us_equities_total_return"
SOURCE_DATE_COLUMN = "bar_date"
SOURCE_PRICE_COLUMN = "adj_close"
SOURCE_PRICE_ADJUSTMENT = "total_return_adjusted_close"

ETF_LABELS: dict[str, str] = {
    "XBI": "Biotech",
    "IHI": "Medical devices",
    "SOXX": "Semiconductors",
    "IGV": "Software infrastructure",
    "XLK": "Technology hardware",
    "XAR": "Defense",
    "XLI": "Machinery",
    "IYT": "Transportation",
    "XLP": "Consumer defensive/staples",
    "SPY": "S&P 500",
    "QQQ": "Nasdaq-100",
}
ETF_TICKERS: tuple[str, ...] = tuple(ETF_LABELS)
PAIR_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    itertools.combinations(ETF_TICKERS, 2)
)
WINDOWS: tuple[int, ...] = (90, 120, 250)
METHODS: tuple[str, ...] = ("pearson", "kendall_tau")


class CorrelationPipelineError(RuntimeError):
    """Base error for a fail-closed correlation build."""


class DatabaseCoverageError(CorrelationPipelineError):
    """Raised when a database cannot cover every requested ETF."""


class SourceContractError(CorrelationPipelineError):
    """Raised when the Stage 2 cache does not match its source manifest."""


class PublicationError(CorrelationPipelineError):
    """Raised when an existing dated publication cannot be reused safely."""


@dataclass(frozen=True)
class DatabasePriceSpec:
    """Low-level SQLite contract retained for reusable reader tests."""

    database_filename: str
    table: str
    source_column: str
    source_value: str
    date_column: str
    adjusted_price_column: str


@dataclass(frozen=True)
class ManifestInstrument:
    ticker: str
    row_count: int
    first_date: str
    last_date: str
    extracted_sha256: str


@dataclass(frozen=True)
class SeriesEvidence:
    ticker: str
    label: str
    manifest_rows: int
    manifest_start_date: str
    manifest_end_date: str
    manifest_sha256: str
    as_of_rows: int
    as_of_start_date: str
    as_of_end_date: str
    as_of_sha256: str


DATABASE_PRICE_SPECS: dict[str, DatabasePriceSpec] = {
    ticker: DatabasePriceSpec(
        DEFAULT_SOURCE_DATABASE.name,
        SOURCE_TABLE,
        "source_id",
        SOURCE_ID,
        SOURCE_DATE_COLUMN,
        SOURCE_PRICE_COLUMN,
    )
    for ticker in ETF_TICKERS
}
DEFAULT_DB_DIR = DEFAULT_SOURCE_DATABASE.parent


def _parse_date(value: object, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise SourceContractError(f"{field} must be an ISO date, got {value!r}") from exc


def _normalise_symbols(tickers: Iterable[str]) -> tuple[str, ...]:
    symbols = tuple(str(ticker).strip().upper() for ticker in tickers)
    if len(symbols) < 2 or any(not ticker for ticker in symbols):
        raise ValueError("At least two non-empty ETF tickers are required")
    if len(set(symbols)) != len(symbols):
        raise ValueError("ETF ticker list contains duplicates")
    return symbols


def _normalise_windows(windows: Iterable[int]) -> tuple[int, ...]:
    values = tuple(int(window) for window in windows)
    if not values or any(window < 2 for window in values):
        raise ValueError("Correlation windows must contain integers of at least 2")
    if len(set(values)) != len(values):
        raise ValueError("Correlation windows contain duplicates")
    return values


def _normalise_methods(methods: Iterable[str]) -> tuple[str, ...]:
    values = tuple(str(method).strip().lower() for method in methods)
    unsupported = sorted(set(values) - set(METHODS))
    if not values or unsupported:
        raise ValueError(f"Unsupported correlation methods: {unsupported or values}")
    if len(set(values)) != len(values):
        raise ValueError("Correlation methods contain duplicates")
    return values


def _quote_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise ValueError(f"Unsafe database identifier: {identifier!r}")
    return f'"{identifier}"'


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _series_sha256(rows: Sequence[tuple[str, float]]) -> str:
    digest = hashlib.sha256()
    for bar_date, value in rows:
        digest.update(f"{bar_date},{value:.17g}\n".encode())
    return digest.hexdigest()


def _json_digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return _sha256_bytes(encoded)


def _database_path(
    ticker: str,
    db_dir: Path,
    database_paths: Mapping[str, Path] | None,
    specs: Mapping[str, DatabasePriceSpec],
) -> Path:
    if database_paths and ticker in database_paths:
        return Path(database_paths[ticker]).expanduser().resolve()
    return (db_dir / specs[ticker].database_filename).expanduser().resolve()


def _validated_rows(frame: pd.DataFrame, ticker: str) -> list[tuple[str, float]]:
    if frame.empty:
        raise DatabaseCoverageError(f"{ticker}: no adjusted-price observations were found")
    parsed_dates = pd.Series(
        pd.to_datetime(frame["observation_date"], errors="coerce"),
        index=frame.index,
    )
    if parsed_dates.isna().any():
        raise DatabaseCoverageError(f"{ticker}: invalid observation dates found")
    dates = parsed_dates.dt.strftime("%Y-%m-%d")
    if dates.duplicated().any():
        duplicates = sorted(set(dates.loc[dates.duplicated(keep=False)].tolist()))
        raise DatabaseCoverageError(f"{ticker}: duplicate price dates: {duplicates[:5]}")
    prices = pd.Series(
        pd.to_numeric(frame["adjusted_price"], errors="coerce"),
        index=frame.index,
        dtype=float,
    )
    values = np.asarray(prices, dtype=np.float64)
    if not np.isfinite(values).all() or bool(np.any(values <= 0.0)):
        raise DatabaseCoverageError(f"{ticker}: prices must be finite and positive")
    rows = sorted(
        (str(bar_date), float(value))
        for bar_date, value in zip(dates.tolist(), values.tolist(), strict=True)
    )
    return [(bar_date, float(value)) for bar_date, value in rows]


def _read_database_series(
    ticker: str,
    db_path: Path,
    spec: DatabasePriceSpec,
    *,
    as_of: date | None = None,
) -> pd.Series:
    if not db_path.is_file():
        raise DatabaseCoverageError(f"{ticker}: database not found: {db_path}")
    table = _quote_identifier(spec.table)
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        schema = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        required = {"ticker", spec.source_column, spec.date_column, spec.adjusted_price_column}
        missing = sorted(required - schema)
        if missing:
            raise DatabaseCoverageError(
                f"{ticker}: {db_path.name}.{spec.table} missing columns: {', '.join(missing)}"
            )
        where = "ticker = ? AND " + _quote_identifier(spec.source_column) + " = ?"
        params: list[object] = [ticker, spec.source_value]
        if as_of is not None:
            where += " AND " + _quote_identifier(spec.date_column) + " <= ?"
            params.append(as_of.isoformat())
        query = (
            f"SELECT {_quote_identifier(spec.date_column)} AS observation_date, "
            f"{_quote_identifier(spec.adjusted_price_column)} AS adjusted_price "
            f"FROM {table} WHERE {where} ORDER BY {_quote_identifier(spec.date_column)}"
        )
        frame = pd.read_sql_query(query, connection, params=params)
    rows = _validated_rows(frame, ticker)
    return pd.Series(
        [value for _, value in rows],
        index=pd.to_datetime([bar_date for bar_date, _ in rows]),
        name=ticker,
        dtype=float,
    )


def load_prices_from_databases(
    db_dir: Path = DEFAULT_DB_DIR,
    tickers: Iterable[str] = ETF_TICKERS,
    *,
    database_paths: Mapping[str, Path] | None = None,
    specs: Mapping[str, DatabasePriceSpec] = DATABASE_PRICE_SPECS,
    as_of: date | None = None,
) -> pd.DataFrame:
    """Read declared SQLite series in read-only mode and fail as one unit."""
    symbols = _normalise_symbols(tickers)
    missing_specs = sorted(set(symbols) - set(specs))
    if missing_specs:
        raise DatabaseCoverageError(f"No database price contract for: {', '.join(missing_specs)}")
    series_by_ticker: dict[str, pd.Series] = {}
    errors: list[str] = []
    for ticker in symbols:
        spec = specs[ticker]
        path = _database_path(ticker, Path(db_dir), database_paths, specs)
        try:
            series_by_ticker[ticker] = _read_database_series(
                ticker, path, spec, as_of=as_of
            )
        except (CorrelationPipelineError, OSError, sqlite3.Error, pd.errors.DatabaseError) as exc:
            errors.append(str(exc))
    if errors:
        raise DatabaseCoverageError(
            "Database coverage validation failed; no artifacts were written:\n- "
            + "\n- ".join(errors)
        )
    return pd.concat(series_by_ticker, axis=1, join="outer", sort=True)


def align_prices(
    raw_prices: pd.DataFrame, tickers: Iterable[str] | None = None
) -> pd.DataFrame:
    """Align requested prices on their common dates without forward filling."""
    symbols = _normalise_symbols(tickers if tickers is not None else raw_prices.columns)
    missing = sorted(set(symbols) - set(raw_prices.columns))
    if missing:
        raise ValueError(f"Raw price panel is missing ticker columns: {', '.join(missing)}")
    prices = raw_prices.loc[:, list(symbols)].apply(pd.to_numeric, errors="coerce")
    prices = prices.replace([np.inf, -np.inf], np.nan).dropna(axis=0, how="any").sort_index()
    prices.index = pd.to_datetime(prices.index, errors="raise").normalize()
    if prices.index.duplicated().any():
        raise ValueError("Aligned prices contain duplicate dates")
    if prices.empty or not bool(prices.gt(0.0).all().all()):
        raise ValueError("Aligned prices must contain finite positive observations")
    return prices


def compute_daily_log_returns(aligned_prices: pd.DataFrame) -> pd.DataFrame:
    """Compute finite daily log returns from adjusted prices."""
    numeric = aligned_prices.astype(float)
    returns = cast(pd.DataFrame, np.log(numeric / numeric.shift(1)).iloc[1:])
    if returns.empty or not np.isfinite(returns.to_numpy(dtype=float)).all():
        raise ValueError("Daily log returns are empty or non-finite")
    return returns


def _rolling_pearson(left: pd.Series, right: pd.Series, window: int) -> pd.Series:
    return cast(
        pd.Series,
        left.rolling(window=window, min_periods=window).corr(right).clip(-1.0, 1.0),
    )


def _rolling_kendall(left: pd.Series, right: pd.Series, window: int) -> pd.Series:
    try:
        from scipy.stats import kendalltau
    except ImportError as exc:  # pragma: no cover - environment-specific dependency
        raise RuntimeError("scipy is required for Kendall tau correlations") from exc
    x = left.to_numpy(dtype=float)
    y = right.to_numpy(dtype=float)
    result = np.full(len(left), np.nan, dtype=float)
    for right_edge in range(window - 1, len(left)):
        start = right_edge - window + 1
        result_object: Any = kendalltau(
            x[start : right_edge + 1], y[start : right_edge + 1]
        )
        statistic = result_object.statistic
        result[right_edge] = float(statistic) if statistic is not None else np.nan
    return pd.Series(result, index=left.index, dtype=float)


def compute_rolling_correlations(
    log_returns: pd.DataFrame,
    windows: Iterable[int] = WINDOWS,
    methods: Iterable[str] = METHODS,
) -> dict[tuple[str, int], pd.DataFrame]:
    """Compute every pair for the columns actually supplied by the caller."""
    symbols = _normalise_symbols(log_returns.columns)
    window_values = _normalise_windows(windows)
    method_values = _normalise_methods(methods)
    returns = log_returns.loc[:, list(symbols)].astype(float)
    if returns.empty or not np.isfinite(returns.to_numpy(dtype=float)).all():
        raise ValueError("Return panel must be non-empty and finite")
    pairs = tuple(itertools.combinations(symbols, 2))
    outputs: dict[tuple[str, int], pd.DataFrame] = {}
    for method in method_values:
        for window in window_values:
            if len(returns) < window:
                raise ValueError(
                    f"Insufficient return history for window={window}: rows={len(returns)}"
                )
            result = pd.DataFrame(index=returns.index)
            for left_name, right_name in pairs:
                if method == "pearson":
                    values = _rolling_pearson(
                        returns[left_name], returns[right_name], window
                    )
                else:
                    values = _rolling_kendall(
                        returns[left_name], returns[right_name], window
                    )
                result[f"{left_name}__{right_name}"] = values
            result = result.round(12)
            incomplete = result.iloc[: window - 1]
            complete = result.iloc[window - 1 :]
            if incomplete.notna().to_numpy(dtype=bool).any():
                raise ValueError(f"{method}/{window} violated the full-window rule")
            if complete.isna().to_numpy(dtype=bool).any():
                raise ValueError(f"{method}/{window} produced undefined correlations")
            outputs[(method, window)] = result
    return outputs


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SourceContractError(f"Required source manifest not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"Cannot read source manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SourceContractError(f"Source manifest must contain a JSON object: {path}")
    return value


def _manifest_instruments(
    payload: Mapping[str, Any], symbols: Sequence[str]
) -> dict[str, ManifestInstrument]:
    raw_items = payload.get("instruments")
    if not isinstance(raw_items, list):
        raise SourceContractError("Source manifest instruments must be a list")
    parsed: dict[str, ManifestInstrument] = {}
    duplicates: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise SourceContractError("Source manifest contains a non-object instrument")
        ticker = str(raw.get("ticker") or "").strip().upper()
        if ticker in parsed:
            duplicates.add(ticker)
            continue
        try:
            item = ManifestInstrument(
                ticker=ticker,
                row_count=int(raw["row_count"]),
                first_date=str(raw["first_date"]).strip(),
                last_date=str(raw["last_date"]).strip(),
                extracted_sha256=str(raw["extracted_sha256"]).strip().lower(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceContractError(
                f"Malformed source-manifest instrument for {ticker or '<blank>'}"
            ) from exc
        first = _parse_date(item.first_date, field=f"{ticker}.first_date")
        last = _parse_date(item.last_date, field=f"{ticker}.last_date")
        if not ticker or item.row_count <= 0 or first > last:
            raise SourceContractError(f"Invalid source-manifest range for {ticker!r}")
        if len(item.extracted_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in item.extracted_sha256
        ):
            raise SourceContractError(f"Invalid source-manifest hash for {ticker}")
        parsed[ticker] = item
    if duplicates:
        raise SourceContractError(
            f"Duplicate source-manifest instruments: {', '.join(sorted(duplicates))}"
        )
    missing = sorted(set(symbols) - set(parsed))
    if missing:
        raise SourceContractError(
            f"Source manifest is missing required ETFs: {', '.join(missing)}"
        )
    return {ticker: parsed[ticker] for ticker in symbols}


def _verify_source_schema(connection: sqlite3.Connection) -> None:
    table = _quote_identifier(SOURCE_TABLE)
    schema = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    required = {
        "ticker",
        "source_id",
        SOURCE_DATE_COLUMN,
        SOURCE_PRICE_COLUMN,
        "is_adjusted",
        "price_adjustment",
    }
    missing = sorted(required - schema)
    if missing:
        raise SourceContractError(
            f"{SOURCE_TABLE} is missing required columns: {', '.join(missing)}"
        )


def load_verified_stage2_prices(
    *,
    as_of: date,
    source_database: Path = DEFAULT_SOURCE_DATABASE,
    source_manifest: Path = DEFAULT_SOURCE_MANIFEST,
    tickers: Iterable[str] = ETF_TICKERS,
    minimum_rows: int,
) -> tuple[pd.DataFrame, list[SeriesEvidence], dict[str, Any]]:
    """Load manifest-verified Stage 2 prices without writing or fetching data."""
    symbols = _normalise_symbols(tickers)
    database_path = source_database.expanduser().resolve()
    manifest_path = source_manifest.expanduser().resolve()
    if not database_path.is_file():
        raise SourceContractError(f"Required Stage 2 price cache not found: {database_path}")
    manifest_hash_before = _sha256_file(manifest_path)
    payload = _load_json_object(manifest_path)
    if str(payload.get("acceptance") or "").strip().upper() != "PASS":
        raise SourceContractError(
            f"Stage 2 source manifest is not accepted: {payload.get('acceptance')!r}"
        )
    manifest_as_of = _parse_date(payload.get("as_of"), field="manifest.as_of")
    if manifest_as_of < as_of:
        raise SourceContractError(
            f"Stage 2 source manifest is stale: {manifest_as_of} < requested {as_of}"
        )
    declared_path = Path(str(payload.get("database_path") or "")).expanduser().resolve()
    if os.path.normcase(str(declared_path)) != os.path.normcase(str(database_path)):
        raise SourceContractError(
            f"Source database path mismatch: manifest={declared_path} requested={database_path}"
        )
    instruments = _manifest_instruments(payload, symbols)
    stale_instruments = [
        ticker
        for ticker, item in instruments.items()
        if item.last_date != manifest_as_of.isoformat()
    ]
    if stale_instruments:
        raise SourceContractError(
            "Required ETFs do not reach the Stage 2 manifest date: "
            + ", ".join(stale_instruments)
        )

    series: dict[str, pd.Series] = {}
    evidence: list[SeriesEvidence] = []
    calendars: dict[str, tuple[str, ...]] = {}
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        _verify_source_schema(connection)
        query = (
            f"SELECT {_quote_identifier(SOURCE_DATE_COLUMN)}, "
            f"{_quote_identifier(SOURCE_PRICE_COLUMN)} FROM {_quote_identifier(SOURCE_TABLE)} "
            "WHERE ticker = ? AND source_id = ? AND is_adjusted = 1 "
            "AND price_adjustment = ? "
            f"AND {_quote_identifier(SOURCE_DATE_COLUMN)} BETWEEN ? AND ? "
            f"ORDER BY {_quote_identifier(SOURCE_DATE_COLUMN)}"
        )
        for ticker in symbols:
            item = instruments[ticker]
            raw_rows = connection.execute(
                query,
                (
                    ticker,
                    SOURCE_ID,
                    SOURCE_PRICE_ADJUSTMENT,
                    item.first_date,
                    item.last_date,
                ),
            ).fetchall()
            rows = [(str(row[0]), float(row[1])) for row in raw_rows]
            if len(rows) != item.row_count:
                raise SourceContractError(
                    f"{ticker}: source row count mismatch: DB={len(rows)} "
                    f"manifest={item.row_count}"
                )
            actual_hash = _series_sha256(rows)
            if actual_hash != item.extracted_sha256:
                raise SourceContractError(
                    f"{ticker}: source series hash mismatch: "
                    f"DB={actual_hash} manifest={item.extracted_sha256}"
                )
            selected = [row for row in rows if row[0] <= as_of.isoformat()]
            if not selected or selected[-1][0] != as_of.isoformat():
                last = selected[-1][0] if selected else "NONE"
                raise SourceContractError(
                    f"{ticker}: exact as-of price missing for {as_of}; last={last}"
                )
            if len(selected) < minimum_rows:
                raise SourceContractError(
                    f"{ticker}: insufficient verified history: {len(selected)} < {minimum_rows}"
                )
            calendars[ticker] = tuple(row[0] for row in selected)
            series[ticker] = pd.Series(
                [row[1] for row in selected],
                index=pd.to_datetime([row[0] for row in selected]),
                name=ticker,
                dtype=float,
            )
            evidence.append(
                SeriesEvidence(
                    ticker=ticker,
                    label=ETF_LABELS.get(ticker, ticker),
                    manifest_rows=item.row_count,
                    manifest_start_date=item.first_date,
                    manifest_end_date=item.last_date,
                    manifest_sha256=item.extracted_sha256,
                    as_of_rows=len(selected),
                    as_of_start_date=selected[0][0],
                    as_of_end_date=selected[-1][0],
                    as_of_sha256=_series_sha256(selected),
                )
            )
        connection.rollback()

    first_calendar = calendars[symbols[0]]
    mismatched = [ticker for ticker in symbols[1:] if calendars[ticker] != first_calendar]
    if mismatched:
        raise SourceContractError(
            "ETF trading calendars differ; refusing a silent date intersection: "
            + ", ".join(mismatched)
        )
    manifest_hash_after = _sha256_file(manifest_path)
    if manifest_hash_after != manifest_hash_before:
        raise SourceContractError("Stage 2 source manifest changed during the correlation read")
    prices = pd.concat(series, axis=1)
    if list(prices.columns) != list(symbols) or bool(
        prices.isna().to_numpy(dtype=bool).any()
    ):
        raise SourceContractError("Verified Stage 2 price panel is incomplete")
    source_meta = {
        "database_path": str(database_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_hash_before,
        "manifest_as_of": manifest_as_of.isoformat(),
        "source_table": SOURCE_TABLE,
        "source_id": SOURCE_ID,
        "price_field": SOURCE_PRICE_COLUMN,
        "price_adjustment": SOURCE_PRICE_ADJUSTMENT,
        "is_adjusted": 1,
    }
    return prices, evidence, source_meta


def _frame_csv_bytes(frame: pd.DataFrame, *, include_index: bool) -> bytes:
    output = frame.copy()
    if include_index:
        output.index.name = "date"
    buffer = io.StringIO(newline="")
    output.to_csv(
        buffer,
        index=include_index,
        date_format="%Y-%m-%d",
        float_format="%.12g",
        lineterminator="\n",
    )
    return buffer.getvalue().encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _WriterLock(AbstractContextManager["_WriterLock"]):
    """Cross-process publication lock with bounded crash recovery."""

    _stale_after_seconds = 6 * 60 * 60

    def __init__(self, path: Path) -> None:
        self.path = path
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError as exc:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age <= self._stale_after_seconds:
                    raise PublicationError(
                        f"Correlation publisher is already locked: {self.path}"
                    ) from exc
                self.path.unlink(missing_ok=True)
                continue
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "created_at_utc": datetime.now(UTC).isoformat(),
                    "token": self.token,
                },
                sort_keys=True,
            ).encode()
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                self.path.unlink(missing_ok=True)
                raise
            self.acquired = True
            return self
        raise PublicationError(f"Could not acquire publication lock: {self.path}")

    def __exit__(self, *exc_info: object) -> None:
        if not self.acquired:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if isinstance(current, dict) and current.get("token") == self.token:
            self.path.unlink(missing_ok=True)


def _latest_correlations(
    rolling: Mapping[tuple[str, int], pd.DataFrame],
    labels: Mapping[str, str],
) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for (method, window), panel in sorted(rolling.items()):
        latest_timestamp = cast(pd.Timestamp, pd.Timestamp(cast(Any, panel.index[-1])))
        latest_date = latest_timestamp.strftime("%Y-%m-%d")
        for pair, value in panel.iloc[-1].items():
            left, right = str(pair).split("__", maxsplit=1)
            records.append(
                {
                    "as_of": latest_date,
                    "method": method,
                    "window": window,
                    "left_ticker": left,
                    "left_label": labels.get(left, left),
                    "right_ticker": right,
                    "right_label": labels.get(right, right),
                    "correlation": float(value),
                }
            )
    return pd.DataFrame.from_records(records)


def _validate_outputs(
    rolling: Mapping[tuple[str, int], pd.DataFrame],
    latest: pd.DataFrame,
    *,
    symbols: Sequence[str],
    windows: Sequence[int],
    methods: Sequence[str],
    return_rows: int,
    as_of: date,
) -> list[dict[str, str]]:
    expected_pairs = math.comb(len(symbols), 2)
    expected_keys = {(method, window) for method in methods for window in windows}
    if set(rolling) != expected_keys:
        raise PublicationError("Rolling output set does not match the configured grid")
    expected_columns = {
        f"{left}__{right}" for left, right in itertools.combinations(symbols, 2)
    }
    for (method, window), panel in rolling.items():
        expected_rows = return_rows - window + 1
        if len(panel) != expected_rows or set(panel.columns) != expected_columns:
            raise PublicationError(
                f"{method}/{window}: invalid output shape "
                f"{panel.shape}; expected=({expected_rows}, {expected_pairs})"
            )
        values = panel.to_numpy(dtype=float)
        if not np.isfinite(values).all() or bool((np.abs(values) > 1.0 + 1e-12).any()):
            raise PublicationError(f"{method}/{window}: invalid correlation coefficients")
        if pd.Timestamp(cast(Any, panel.index[-1])).date() != as_of:
            raise PublicationError(f"{method}/{window}: output does not end on {as_of}")
    expected_latest = len(expected_keys) * expected_pairs
    if len(latest) != expected_latest:
        raise PublicationError(
            f"Latest-correlation rows={len(latest)} expected={expected_latest}"
        )
    if set(latest["as_of"].astype(str)) != {as_of.isoformat()}:
        raise PublicationError("Latest-correlation artifact has the wrong as-of date")
    return [
        {"gate": "source_manifest_accepted", "status": "PASS"},
        {"gate": "source_manifest_not_older_than_as_of", "status": "PASS"},
        {"gate": "source_rows_match_manifest_hashes", "status": "PASS"},
        {"gate": "all_etfs_have_exact_as_of_price", "status": "PASS"},
        {"gate": "etf_calendars_match_without_fill", "status": "PASS"},
        {"gate": "minimum_history_for_largest_window", "status": "PASS"},
        {"gate": "correlation_output_shape", "status": "PASS"},
        {"gate": "correlation_values_finite_and_bounded", "status": "PASS"},
        {"gate": "latest_artifact_complete", "status": "PASS"},
    ]


def _existing_manifest_if_reusable(
    path: Path,
    *,
    input_digest: str,
    expected_outputs: set[str],
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"Existing manifest is unreadable: {path}") from exc
    if not isinstance(manifest, dict):
        raise PublicationError(f"Existing manifest is not a JSON object: {path}")
    if manifest.get("acceptance") != "PASS" or manifest.get("input_digest") != input_digest:
        return None
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        raise PublicationError("Existing manifest has no output hash contract")
    if set(map(str, outputs)) != expected_outputs:
        raise PublicationError("Existing manifest output set is incomplete or unexpected")
    for filename, raw_contract in outputs.items():
        if Path(str(filename)).name != str(filename):
            raise PublicationError(f"Unsafe output filename in manifest: {filename}")
        if not isinstance(raw_contract, dict):
            raise PublicationError(f"Malformed output contract for {filename}")
        output_path = path.parent / str(filename)
        if (
            not output_path.is_file()
            or _sha256_file(output_path) != raw_contract.get("sha256")
        ):
            raise PublicationError(f"Existing output hash mismatch: {output_path}")
    return manifest


def build_artifacts(
    as_of: date,
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    source_database: Path = DEFAULT_SOURCE_DATABASE,
    source_manifest: Path = DEFAULT_SOURCE_MANIFEST,
    tickers: Iterable[str] = ETF_TICKERS,
    windows: Iterable[int] = WINDOWS,
    methods: Iterable[str] = METHODS,
    force: bool = False,
) -> dict[str, Any]:
    """Build and atomically publish one dated correlation package."""
    symbols = _normalise_symbols(tickers)
    window_values = _normalise_windows(windows)
    method_values = _normalise_methods(methods)
    prices, evidence, source_meta = load_verified_stage2_prices(
        as_of=as_of,
        source_database=source_database,
        source_manifest=source_manifest,
        tickers=symbols,
        minimum_rows=max(window_values) + 1,
    )
    code_hash = _sha256_file(Path(__file__).resolve())
    input_contract = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "tickers": list(symbols),
        "windows": list(window_values),
        "methods": list(method_values),
        "source_id": SOURCE_ID,
        "series": [
            {
                "ticker": item.ticker,
                "rows": item.as_of_rows,
                "sha256": item.as_of_sha256,
            }
            for item in evidence
        ],
        "pipeline_sha256": code_hash,
    }
    input_digest = _json_digest(input_contract)
    resolved_output_root = output_root.expanduser().resolve()
    output_dir = resolved_output_root / as_of.isoformat()
    manifest_path = output_dir / "correlation_manifest.json"
    expected_outputs = {
        *(
            f"rolling_{method}_{window}.csv"
            for method in method_values
            for window in window_values
        ),
        "latest_correlations.csv",
        "source_coverage.csv",
        "correlation_validation.csv",
    }
    lock_path = resolved_output_root / ".publisher.lock"

    # The source hashes and code identity are enough to validate a prior build.
    # Avoid recalculating the expensive Kendall panels on an idempotent rerun.
    with _WriterLock(lock_path):
        if not force:
            reusable = _existing_manifest_if_reusable(
                manifest_path,
                input_digest=input_digest,
                expected_outputs=expected_outputs,
            )
            if reusable is not None:
                LOGGER.info("UP_TO_DATE: %s", manifest_path)
                return reusable
            if output_dir.exists() and any(output_dir.iterdir()):
                raise PublicationError(
                    "Dated output exists with different or invalid inputs: "
                    f"{output_dir}; use --force for an intentional replacement"
                )

    returns = compute_daily_log_returns(prices)
    rolling_with_warmup = compute_rolling_correlations(
        returns, windows=window_values, methods=method_values
    )
    rolling = {
        (method, window): panel.iloc[window - 1 :].copy()
        for (method, window), panel in rolling_with_warmup.items()
    }
    latest = _latest_correlations(rolling, ETF_LABELS)
    validations = _validate_outputs(
        rolling,
        latest,
        symbols=symbols,
        windows=window_values,
        methods=method_values,
        return_rows=len(returns),
        as_of=as_of,
    )
    coverage = pd.DataFrame.from_records([asdict(item) for item in evidence])
    validation_frame = pd.DataFrame.from_records(validations)

    # Recheck under the write lock in case another process published while this
    # process was computing.
    with _WriterLock(lock_path):
        if not force:
            reusable = _existing_manifest_if_reusable(
                manifest_path,
                input_digest=input_digest,
                expected_outputs=expected_outputs,
            )
            if reusable is not None:
                LOGGER.info("UP_TO_DATE: %s", manifest_path)
                return reusable
        if output_dir.exists() and any(output_dir.iterdir()) and not force:
            raise PublicationError(
                f"Dated output already exists with different or invalid inputs: {output_dir}; "
                "use --force for an intentional replacement"
            )

        payloads: dict[str, bytes] = {}
        output_rows: dict[str, int] = {}
        for (method, window), panel in sorted(rolling.items()):
            filename = f"rolling_{method}_{window}.csv"
            payloads[filename] = _frame_csv_bytes(panel, include_index=True)
            output_rows[filename] = len(panel)
        payloads["latest_correlations.csv"] = _frame_csv_bytes(
            latest, include_index=False
        )
        output_rows["latest_correlations.csv"] = len(latest)
        payloads["source_coverage.csv"] = _frame_csv_bytes(
            coverage, include_index=False
        )
        output_rows["source_coverage.csv"] = len(coverage)
        payloads["correlation_validation.csv"] = _frame_csv_bytes(
            validation_frame, include_index=False
        )
        output_rows["correlation_validation.csv"] = len(validation_frame)
        output_contracts = {
            filename: {
                "sha256": _sha256_bytes(payload),
                "size_bytes": len(payload),
                "rows": output_rows[filename],
            }
            for filename, payload in payloads.items()
        }
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "acceptance": "PASS",
            "as_of": as_of.isoformat(),
            "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "input_digest": input_digest,
            "pipeline_path": str(Path(__file__).resolve()),
            "pipeline_sha256": code_hash,
            "source": source_meta,
            "source_database_access": "read_only",
            "external_requests": 0,
            "raw_price_or_return_artifacts_published": False,
            "tickers": list(symbols),
            "labels": {
                ticker: ETF_LABELS.get(ticker, ticker) for ticker in symbols
            },
            "windows": list(window_values),
            "methods": list(method_values),
            "pair_count": math.comb(len(symbols), 2),
            "price_rows": len(prices),
            "return_rows": len(returns),
            "price_start_date": cast(
                pd.Timestamp, pd.Timestamp(cast(Any, prices.index[0]))
            ).strftime("%Y-%m-%d"),
            "price_end_date": cast(
                pd.Timestamp, pd.Timestamp(cast(Any, prices.index[-1]))
            ).strftime("%Y-%m-%d"),
            "alignment_policy": (
                "identical verified ETF calendars; no forward fill and no silent row drop"
            ),
            "source_series": [asdict(item) for item in evidence],
            "validation": validations,
            "outputs": output_contracts,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename, payload in payloads.items():
            _atomic_write(output_dir / filename, payload)
        for filename, contract in output_contracts.items():
            if _sha256_file(output_dir / filename) != contract["sha256"]:
                raise PublicationError(f"Post-write hash mismatch: {filename}")
        manifest_bytes = (
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode()
        _atomic_write(manifest_path, manifest_bytes)
        LOGGER.info(
            "PASS: %d ETFs, %d pairs, %d latest rows through %s",
            len(symbols),
            manifest["pair_count"],
            len(latest),
            as_of,
        )
        return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build ETF correlations from the verified Stage 2 price cache"
    )
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--source-database", type=Path, default=DEFAULT_SOURCE_DATABASE
    )
    parser.add_argument(
        "--source-manifest", type=Path, default=DEFAULT_SOURCE_MANIFEST
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        build_artifacts(
            args.as_of,
            output_root=args.output_root,
            source_database=args.source_database,
            source_manifest=args.source_manifest,
            force=args.force,
        )
    except (CorrelationPipelineError, OSError, sqlite3.Error, ValueError) as exc:
        LOGGER.error("Index-correlation build failed: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
