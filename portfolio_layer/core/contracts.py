"""Canonical cross-sector `stocks_scores` contract: schema, IO, calibration, persistence.

The portfolio layer consumes ONE artifact per as-of date: `runs/<as_of>/stocks_scores.csv`.
Each sector publishes its own native score export; Stage 1 adapters map those onto this contract.
Nothing here imports a sector package or reads a sector DB.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from orchestration_contracts.financial_lineage import (
    LINEAGE_FIELDS as FINANCIAL_LINEAGE_FIELDS,
)
from portfolio_layer.core.db import utc_now


DEFAULT_CONTRACT_VERSION = "stocks_scores_v1"
CONTRACT_VERSION = DEFAULT_CONTRACT_VERSION


# Intermediate artifact written by Stage 1 collect (native score, pre-calibration).
COLLECTED_FIELDS = [
    "as_of_date",            # run as-of (snapshot date)
    "ticker",
    "source_pipeline",
    "sector",
    "industry",
    "industry_aggregate",
    "native_score",          # the sector's own headline score (0-100 composite)
    "investable_eligible",   # 0/1 hard gate carried from the sector's native gate
    "eligibility_reason",
    "calibration_research_eligible",  # 0/1 empirical score-to-return calibration universe
    "calibration_research_reason",
    "calibration_sample_role",  # source/intrinsic role: strict_oos | pre_lock_research | excluded
    "stage1_sample_role",  # portfolio-layer verdict after adapter guardrails
    "oos_score_valid_flag",  # 0/1 source score was frozen/live-valid for OOS use
    "missing_score_flag",  # 0/1 native score is a source sentinel and must not enter rank/calibration math
    "survivorship_corrected_panel_flag",  # 0/1 historical replay row came from a PIT survivorship panel
    "score_confidence",      # 0-1
    "source_asof_date",      # the sector file's own date
    *FINANCIAL_LINEAGE_FIELDS,
]

# Final canonical contract consumed by the optimizer.
CONTRACT_FIELDS = [
    "as_of_date",
    "ticker",
    "source_pipeline",
    "sector",
    "industry",
    "industry_aggregate",
    "final_score",               # calibrated expected forward alpha (annualized fraction)
    "rating",
    "within_sector_percentile",  # legacy name; percentile is computed within source_pipeline/sleeve
    "score_confidence",
    "investable_eligible",
    "eligibility_reason",
    "calibration_research_eligible",
    "calibration_research_reason",
    "calibration_sample_role",
    "stage1_sample_role",
    "oos_score_valid_flag",
    "missing_score_flag",
    "survivorship_corrected_panel_flag",
    "native_score",
    "source_asof_date",
    *FINANCIAL_LINEAGE_FIELDS,
    "staleness_days",
    "score_version",
]

DEFAULT_RATING_BANDS = {  # within-sector percentile floor -> rating label
    "strong_buy": 90.0,
    "buy": 70.0,
    "hold": 40.0,
    "reduce": 20.0,
    "avoid": 0.0,
}


@dataclass
class CanonicalScore:
    """One adapter-normalized row, before calibration/percentile/rating are applied."""

    ticker: str
    source_pipeline: str
    sector: str
    industry: str
    industry_aggregate: str
    native_score: float
    investable_eligible: int
    eligibility_reason: str
    calibration_research_eligible: int
    calibration_research_reason: str
    calibration_sample_role: str
    stage1_sample_role: str
    oos_score_valid_flag: int
    missing_score_flag: int
    survivorship_corrected_panel_flag: int
    score_confidence: float
    source_asof_date: str
    financial_lineage_checked_asof_date: str = ""
    financial_lineage_status: str = ""
    financial_lineage_gate: int = 0
    financial_lineage_classification: str = ""
    latest_material_financial_filing_date: str = ""
    latest_material_financial_form: str = ""
    latest_material_financial_accession: str = ""
    latest_material_financial_report_date: str = ""
    incorporated_financial_filing_date: str = ""
    incorporated_financial_accession: str = ""
    incorporated_financial_report_date: str = ""
    incorporated_financial_core_metric_count: int = 0
    financial_lineage_reason: str = ""


@dataclass
class AdapterResult:
    source_pipeline: str
    adapter: str
    source_file: Path
    source_asof_date: str
    rows: list[CanonicalScore]
    source_files: tuple[Path, ...] = ()


# ---------------------------------------------------------------------------
# IO helpers (atomic writes so consumers never read a half-written file)
# ---------------------------------------------------------------------------
def _replace_atomic(source: str | Path, target: str | Path) -> None:
    """Publish a sibling temp file despite brief Windows/OneDrive sharing locks."""
    attempts = 8
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.05 * (2**attempt), 1.0))


def replace_atomic(source: str | Path, target: str | Path) -> None:
    """Publish a sibling temp file with the repository's Windows retry policy."""
    _replace_atomic(source, target)


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                count += 1
        _replace_atomic(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
    return count


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        _replace_atomic(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
        _replace_atomic(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def write_via_temp(path: Path, writer: Callable[[Path], None]) -> None:
    """Run a path-based writer against a sibling temp file, then atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        writer(tmp_path)
        _replace_atomic(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def read_manifest(path: Path) -> dict[str, Any]:
    """Read a JSON manifest and require a top-level object."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Manifest {path} must contain a JSON object")
    return payload


def manifest_acceptance_value(manifest: dict[str, Any]) -> str:
    """Return the authoritative acceptance value used across portfolio stages."""
    hard = str(manifest.get("hard_gate_acceptance", "")).strip()
    return hard or str(manifest.get("acceptance", "")).strip()


def manifest_accepts(manifest: dict[str, Any], *, allow_deferred: bool = True) -> bool:
    acceptance = manifest_acceptance_value(manifest)
    return acceptance == "PASS" or (allow_deferred and acceptance.startswith("PASS_"))


def manifest_recorded_sha256(manifest: dict[str, Any], *artifact_keys: str) -> str | None:
    """Find an artifact hash in one of the manifest layouts used by the pipeline.

    Callers provide explicit keys, including any relative prefix. Basename guessing is intentionally
    avoided because a manifest can contain multiple files with the same basename in different stages.
    """
    for section_name in ("files", "provenance_sha256", "outputs_sha256", "inputs_sha256"):
        section = manifest.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in artifact_keys:
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                sha = str(value.get("sha256", "")).strip()
                if sha:
                    return sha
    for key in artifact_keys:
        value = manifest.get(f"{key}_sha256")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def sealed_artifact_errors(
    manifest: dict[str, Any],
    artifact: Path,
    *artifact_keys: str,
    run_as_of: str | None = None,
    allow_deferred: bool = True,
) -> list[str]:
    """Validate acceptance, run date, presence, and the manifest-recorded artifact hash."""
    errors: list[str] = []
    acceptance = manifest_acceptance_value(manifest)
    if not manifest_accepts(manifest, allow_deferred=allow_deferred):
        errors.append(f"acceptance={acceptance or 'MISSING'}")
    manifest_as_of = str(
        manifest.get(
            "run_as_of",
            manifest.get(
                "run_as_of_date",
                manifest.get("as_of_date", manifest.get("ledger_as_of", "")),
            ),
        )
    ).strip()
    if run_as_of:
        if not manifest_as_of:
            errors.append(f"run_as_of=MISSING expected={run_as_of}")
        elif manifest_as_of != run_as_of:
            errors.append(f"run_as_of={manifest_as_of} expected={run_as_of}")
    if not artifact.exists():
        errors.append(f"artifact_missing={artifact}")
        return errors
    expected = manifest_recorded_sha256(manifest, *artifact_keys)
    if not expected:
        errors.append(f"artifact_sha_missing={list(artifact_keys)}")
    else:
        actual = sha256_file(artifact)
        if actual != expected:
            errors.append(f"artifact_sha_mismatch={actual[:12]}!={expected[:12]}")
    return errors


def fail_if_exists(paths: Iterable[Path], *, force: bool = False) -> None:
    """Protect run artifacts from accidental overwrite unless the caller opts in."""
    existing = [path for path in paths if path.exists()]
    if existing and not force:
        joined = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"Refusing to overwrite existing run artifacts without --force: {joined}")


def contract_version(config: dict[str, Any]) -> str:
    value = (
        config.get("score_contract", {}).get("contract_version")
        if isinstance(config.get("score_contract"), dict)
        else None
    )
    return str(value or DEFAULT_CONTRACT_VERSION)


# ---------------------------------------------------------------------------
# Calibration + rating
# ---------------------------------------------------------------------------
def expected_alpha(native_score: float, *, neutral: float, scale: float, expected_alpha_at_full: float) -> float:
    """Map a sector's 0-100 native composite to a common expected-alpha scale (annualized fraction).

    Monotonic-increasing linear anchor: native==neutral -> 0 alpha; native==neutral+scale ->
    +expected_alpha_at_full. Provisional magnitudes per sector; empirical IC re-calibration against the
    Stage 2 return panel refines the slopes later. The transform preserves each sector's own rank-IC sign.
    """
    values = (native_score, neutral, scale, expected_alpha_at_full)
    if not all(math.isfinite(v) for v in values):
        raise ValueError(f"expected_alpha inputs must be finite: {values}")
    if scale <= 0:
        raise ValueError(f"expected_alpha scale must be positive, got {scale}")
    return expected_alpha_at_full * (native_score - neutral) / scale


def rating_for_percentile(pct: float, bands: dict[str, float]) -> str:
    for label in ("strong_buy", "buy", "hold", "reduce", "avoid"):
        threshold = _f(bands[label]) if label in bands else None
        if threshold is not None and pct >= threshold:
            return label
    return "avoid"


def validate_rating_bands(bands: dict[str, Any]) -> list[str]:
    """Validate percentile rating thresholds before they are used for ranking/exit labels."""
    labels = ("strong_buy", "buy", "hold", "reduce", "avoid")
    errors: list[str] = []
    values: dict[str, float] = {}
    for label in labels:
        if label not in bands:
            errors.append(f"missing:{label}")
            continue
        value = _f(bands.get(label))
        if value is None:
            errors.append(f"non_numeric:{label}={bands.get(label)!r}")
            continue
        if not 0.0 <= value <= 100.0:
            errors.append(f"out_of_range:{label}={value}")
            continue
        values[label] = value
    for higher, lower in zip(labels, labels[1:]):
        if higher in values and lower in values and values[higher] < values[lower]:
            errors.append(f"not_descending:{higher}={values[higher]} < {lower}={values[lower]}")
    return errors


def percentiles_within(values: Sequence[float]) -> list[float]:
    """Rank-based percentile (0-100, higher score -> higher pct) with order-independent ties."""
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    pct = [0.0] * n
    pos = 0
    while pos < n:
        end = pos + 1
        while end < n and values[order[end]] == values[order[pos]]:
            end += 1
        # Average the rank percentile across exact ties so input order cannot change the result.
        avg_rank = (pos + 1 + end) / 2.0
        tied_pct = 100.0 * (avg_rank - 0.5) / n
        for tied_pos in range(pos, end):
            pct[order[tied_pos]] = tied_pct
        pos = end
    return pct


# ---------------------------------------------------------------------------
# Persistence (Stage 1 introduces the contract table)
# ---------------------------------------------------------------------------
STOCKS_SCORES_DDL = """
CREATE TABLE IF NOT EXISTS stocks_scores (
    run_as_of_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    source_pipeline TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    sector TEXT,
    industry TEXT,
    industry_aggregate TEXT,
    final_score REAL,
    rating TEXT,
    within_sector_percentile REAL,
    score_confidence REAL,
    investable_eligible INTEGER NOT NULL DEFAULT 0,
    eligibility_reason TEXT,
    calibration_research_eligible INTEGER NOT NULL DEFAULT 0,
    calibration_research_reason TEXT,
    calibration_sample_role TEXT NOT NULL DEFAULT 'excluded',
    stage1_sample_role TEXT NOT NULL DEFAULT 'excluded',
    oos_score_valid_flag INTEGER NOT NULL DEFAULT 0,
    missing_score_flag INTEGER NOT NULL DEFAULT 0,
    survivorship_corrected_panel_flag INTEGER NOT NULL DEFAULT 0,
    native_score REAL,
    source_asof_date TEXT,
    financial_lineage_checked_asof_date TEXT,
    financial_lineage_status TEXT,
    financial_lineage_gate INTEGER NOT NULL DEFAULT 0,
    financial_lineage_classification TEXT,
    latest_material_financial_filing_date TEXT,
    latest_material_financial_form TEXT,
    latest_material_financial_accession TEXT,
    latest_material_financial_report_date TEXT,
    incorporated_financial_filing_date TEXT,
    incorporated_financial_accession TEXT,
    incorporated_financial_report_date TEXT,
    incorporated_financial_core_metric_count INTEGER NOT NULL DEFAULT 0,
    financial_lineage_reason TEXT,
    staleness_days INTEGER,
    score_version TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_as_of_date, ticker)
);
"""


def init_contract_tables(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(STOCKS_SCORES_DDL)
        _ensure_stocks_scores_columns(conn)
        if _stocks_scores_pk_columns(conn) == ["run_as_of_date", "ticker"]:
            conn.execute("DROP INDEX IF EXISTS ux_stocks_scores_run_ticker")
        else:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_stocks_scores_run_ticker
                ON stocks_scores(run_as_of_date, ticker)
                """
            )


def _stocks_scores_pk_columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(stocks_scores)").fetchall()
    pk_cols: list[tuple[int, str]] = []
    for row in rows:
        pk_order = int(row["pk"] if isinstance(row, sqlite3.Row) else row[5])
        if pk_order:
            name = str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            pk_cols.append((pk_order, name))
    return [name for _, name in sorted(pk_cols)]


def _ensure_stocks_scores_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(stocks_scores)").fetchall()
    existing = {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}
    if "calibration_research_eligible" not in existing:
        conn.execute("ALTER TABLE stocks_scores ADD COLUMN calibration_research_eligible INTEGER NOT NULL DEFAULT 0")
    if "calibration_research_reason" not in existing:
        conn.execute("ALTER TABLE stocks_scores ADD COLUMN calibration_research_reason TEXT")
    if "calibration_sample_role" not in existing:
        conn.execute("ALTER TABLE stocks_scores ADD COLUMN calibration_sample_role TEXT NOT NULL DEFAULT 'excluded'")
    if "stage1_sample_role" not in existing:
        conn.execute("ALTER TABLE stocks_scores ADD COLUMN stage1_sample_role TEXT NOT NULL DEFAULT 'excluded'")
    if "oos_score_valid_flag" not in existing:
        conn.execute("ALTER TABLE stocks_scores ADD COLUMN oos_score_valid_flag INTEGER NOT NULL DEFAULT 0")
    if "missing_score_flag" not in existing:
        conn.execute("ALTER TABLE stocks_scores ADD COLUMN missing_score_flag INTEGER NOT NULL DEFAULT 0")
    if "survivorship_corrected_panel_flag" not in existing:
        conn.execute("ALTER TABLE stocks_scores ADD COLUMN survivorship_corrected_panel_flag INTEGER NOT NULL DEFAULT 0")
    for column in FINANCIAL_LINEAGE_FIELDS:
        if column in existing:
            continue
        if column in {"financial_lineage_gate", "incorporated_financial_core_metric_count"}:
            conn.execute(
                f"ALTER TABLE stocks_scores ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )
        else:
            conn.execute(f"ALTER TABLE stocks_scores ADD COLUMN {column} TEXT")


def upsert_stocks_scores(conn: sqlite3.Connection, run_as_of: str, rows: Sequence[dict[str, Any]]) -> int:
    init_contract_tables(conn)
    now = utc_now()
    payload = [
        (
            run_as_of, r["ticker"], r["source_pipeline"], r["as_of_date"], r["sector"], r["industry"],
            r["industry_aggregate"], _f(r.get("final_score")), r.get("rating"),
            _f(r.get("within_sector_percentile")), _f(r.get("score_confidence")),
            int(r.get("investable_eligible") or 0), r.get("eligibility_reason"), _f(r.get("native_score")),
            int(r.get("calibration_research_eligible") or 0), r.get("calibration_research_reason"),
            r.get("calibration_sample_role") or "excluded", r.get("stage1_sample_role") or "excluded",
            int(r.get("oos_score_valid_flag") or 0), int(r.get("missing_score_flag") or 0),
            int(r.get("survivorship_corrected_panel_flag") or 0),
            r.get("source_asof_date"), _i(r.get("staleness_days")), r.get("score_version"), now,
            r.get("financial_lineage_checked_asof_date"), r.get("financial_lineage_status"),
            int(r.get("financial_lineage_gate") or 0), r.get("financial_lineage_classification"),
            r.get("latest_material_financial_filing_date"),
            r.get("latest_material_financial_form"), r.get("latest_material_financial_accession"),
            r.get("latest_material_financial_report_date"), r.get("incorporated_financial_filing_date"),
            r.get("incorporated_financial_accession"), r.get("incorporated_financial_report_date"),
            int(r.get("incorporated_financial_core_metric_count") or 0),
            r.get("financial_lineage_reason"),
        )
        for r in rows
    ]
    with conn:
        conn.execute("DELETE FROM stocks_scores WHERE run_as_of_date = ?", (run_as_of,))
        conn.executemany(
            """
            INSERT INTO stocks_scores(
                run_as_of_date, ticker, source_pipeline, as_of_date, sector, industry, industry_aggregate,
                final_score, rating, within_sector_percentile, score_confidence, investable_eligible,
                eligibility_reason, native_score, calibration_research_eligible, calibration_research_reason,
                calibration_sample_role, stage1_sample_role, oos_score_valid_flag, missing_score_flag,
                survivorship_corrected_panel_flag, source_asof_date, staleness_days, score_version, created_at,
                financial_lineage_checked_asof_date, financial_lineage_status, financial_lineage_gate,
                financial_lineage_classification, latest_material_financial_filing_date,
                latest_material_financial_form, latest_material_financial_accession,
                latest_material_financial_report_date, incorporated_financial_filing_date, incorporated_financial_accession,
                incorporated_financial_report_date, incorporated_financial_core_metric_count, financial_lineage_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            payload,
        )
    return len(payload)


def _f(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    f = _f(value)
    return None if f is None else int(f)
