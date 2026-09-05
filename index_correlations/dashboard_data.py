"""Verified readers for the dated index-correlation dashboard artifacts."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from index_correlations.pipeline import (
    ETF_TICKERS,
    METHODS,
    SCHEMA_VERSION,
    WINDOWS,
)

PAIR_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    itertools.combinations(ETF_TICKERS, 2)
)


class DashboardArtifactError(RuntimeError):
    """Raised when a correlation publication is missing or fails its seal."""


def _normalise_values(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values)


def _expected_outputs(
    methods: Sequence[str], windows: Sequence[int]
) -> set[str]:
    rolling = {
        f"rolling_{method}_{window}.csv"
        for method in methods
        for window in windows
    }
    return rolling | {
        "correlation_validation.csv",
        "latest_correlations.csv",
        "source_coverage.csv",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_publication_dir(output_root: Path) -> Path | None:
    """Return the newest ISO-dated directory, including incomplete latest runs."""
    if not output_root.is_dir():
        return None
    dated: list[tuple[date, Path]] = []
    for candidate in output_root.iterdir():
        if not candidate.is_dir():
            continue
        try:
            parsed = date.fromisoformat(candidate.name)
        except ValueError:
            continue
        if parsed.isoformat() == candidate.name:
            dated.append((parsed, candidate))
    return max(dated, key=lambda item: item[0])[1] if dated else None


def publication_signature(
    publication_dir: Path,
) -> tuple[tuple[str, str, int], ...]:
    """Return a content-based Streamlit cache key for one publication."""
    paths = [publication_dir / "correlation_manifest.json"]
    if paths[0].is_file():
        try:
            manifest = json.loads(paths[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = None
        if isinstance(manifest, dict) and isinstance(manifest.get("outputs"), dict):
            paths.extend(
                publication_dir / str(filename)
                for filename in sorted(manifest["outputs"])
            )
    signature: list[tuple[str, str, int]] = []
    for path in paths:
        try:
            signature.append((str(path), _sha256_file(path), path.stat().st_size))
        except OSError:
            signature.append((str(path), "MISSING", 0))
    return tuple(signature)


def load_verified_manifest(
    publication_dir: Path,
    *,
    tickers: Sequence[str] = ETF_TICKERS,
    windows: Sequence[int] = WINDOWS,
    methods: Sequence[str] = METHODS,
) -> dict[str, Any]:
    """Load a publication only after its complete hash contract validates."""
    manifest_path = publication_dir / "correlation_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DashboardArtifactError(
            f"Latest correlation run is incomplete: {manifest_path} is missing"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DashboardArtifactError(
            f"Cannot read correlation manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise DashboardArtifactError(
            "Correlation manifest must contain a JSON object"
        )

    try:
        directory_date = date.fromisoformat(publication_dir.name).isoformat()
    except ValueError as exc:
        raise DashboardArtifactError(
            f"Correlation publication directory is not ISO-dated: {publication_dir}"
        ) from exc
    if directory_date != publication_dir.name:
        raise DashboardArtifactError(
            f"Correlation publication directory is not canonical: {publication_dir}"
        )
    expected_tickers = _normalise_values(tickers)
    expected_methods = _normalise_values(methods)
    expected_windows = tuple(int(window) for window in windows)
    if manifest.get("acceptance") != "PASS":
        raise DashboardArtifactError("Latest correlation manifest is not accepted")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DashboardArtifactError("Correlation manifest schema is unsupported")
    if manifest.get("as_of") != directory_date:
        raise DashboardArtifactError(
            "Correlation manifest date does not match its directory"
        )
    if tuple(manifest.get("tickers", ())) != expected_tickers:
        raise DashboardArtifactError(
            "Correlation ticker contract does not match the dashboard"
        )
    if tuple(manifest.get("methods", ())) != expected_methods:
        raise DashboardArtifactError(
            "Correlation method contract does not match the dashboard"
        )
    if tuple(manifest.get("windows", ())) != expected_windows:
        raise DashboardArtifactError(
            "Correlation window contract does not match the dashboard"
        )
    if manifest.get("source_database_access") != "read_only":
        raise DashboardArtifactError("Correlation source was not opened read-only")
    if manifest.get("external_requests") != 0:
        raise DashboardArtifactError(
            "Correlation build unexpectedly made external requests"
        )
    if manifest.get("raw_price_or_return_artifacts_published") is not False:
        raise DashboardArtifactError(
            "Correlation publication contains duplicated market data"
        )

    pipeline_path = Path(__file__).with_name("pipeline.py")
    if manifest.get("pipeline_sha256") != _sha256_file(pipeline_path):
        raise DashboardArtifactError(
            "Correlation artifacts were not built by the current pipeline code"
        )

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise DashboardArtifactError("Correlation manifest has no output contract")
    expected_outputs = _expected_outputs(expected_methods, expected_windows)
    if set(map(str, outputs)) != expected_outputs:
        raise DashboardArtifactError(
            "Correlation manifest output set is incomplete or unexpected"
        )
    actual_files = {
        path.name for path in publication_dir.iterdir() if path.is_file()
    }
    if actual_files != expected_outputs | {manifest_path.name}:
        raise DashboardArtifactError(
            "Correlation directory contains missing or unexpected files"
        )

    for filename, raw_contract in outputs.items():
        if (
            Path(str(filename)).name != str(filename)
            or not isinstance(raw_contract, Mapping)
        ):
            raise DashboardArtifactError(f"Malformed output contract: {filename}")
        path = publication_dir / str(filename)
        try:
            size = path.stat().st_size
            digest = _sha256_file(path)
        except OSError as exc:
            raise DashboardArtifactError(
                f"Cannot verify correlation output {path}"
            ) from exc
        if (
            size != raw_contract.get("size_bytes")
            or digest != raw_contract.get("sha256")
        ):
            raise DashboardArtifactError(
                f"Correlation output seal mismatch: {path.name}"
            )
    return manifest


def load_verified_rolling(
    publication_dir: Path,
    method: str,
    window: int,
    *,
    tickers: Sequence[str] = ETF_TICKERS,
    windows: Sequence[int] = WINDOWS,
    methods: Sequence[str] = METHODS,
) -> pd.DataFrame:
    """Load one rolling matrix after validating publication and matrix semantics."""
    manifest = load_verified_manifest(
        publication_dir,
        tickers=tickers,
        windows=windows,
        methods=methods,
    )
    if method not in methods or window not in windows:
        raise DashboardArtifactError(
            f"Unsupported correlation selection: {method}/{window}"
        )
    path = publication_dir / f"rolling_{method}_{window}.csv"
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError) as exc:
        raise DashboardArtifactError(
            f"Cannot read rolling correlation output {path}"
        ) from exc

    pairs = tuple(itertools.combinations(tickers, 2))
    expected_columns = [
        "date",
        *(f"{left}__{right}" for left, right in pairs),
    ]
    if list(frame.columns) != expected_columns:
        raise DashboardArtifactError(
            f"Rolling correlation schema mismatch: {path.name}"
        )
    dates = pd.to_datetime(frame.pop("date"), errors="coerce")
    if (
        dates.isna().any()
        or dates.duplicated().any()
        or not dates.is_monotonic_increasing
    ):
        raise DashboardArtifactError(
            f"Rolling correlation dates are invalid: {path.name}"
        )
    numeric = cast(pd.DataFrame, frame.apply(pd.to_numeric, errors="coerce"))
    values = numeric.to_numpy(dtype=float)
    if (
        not np.isfinite(values).all()
        or bool((np.abs(values) > 1.0 + 1e-12).any())
    ):
        raise DashboardArtifactError(
            f"Rolling correlation values are invalid: {path.name}"
        )
    expected_rows = int(manifest["return_rows"]) - int(window) + 1
    if len(numeric) != expected_rows or not len(numeric):
        raise DashboardArtifactError(
            f"Rolling correlation row count is invalid: {path.name}"
        )
    if dates.iloc[-1].date().isoformat() != manifest["as_of"]:
        raise DashboardArtifactError(
            f"Rolling correlation end date is stale: {path.name}"
        )
    numeric.index = pd.DatetimeIndex(dates, name="date")
    return numeric


def pair_column(left: str, right: str) -> str:
    """Return the canonical wide-column name for two distinct tracked ETFs."""
    if (left, right) in PAIR_COLUMNS:
        return f"{left}__{right}"
    if (right, left) in PAIR_COLUMNS:
        return f"{right}__{left}"
    raise ValueError(f"Unknown or identical ETF pair: {left}/{right}")
