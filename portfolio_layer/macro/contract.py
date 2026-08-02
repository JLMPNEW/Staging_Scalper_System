"""Stage 6 macro contract schema and SQLite helpers.

The vendored MacroLayer owns macro data construction. This package owns the portfolio-layer
contract: PIT-filtered, provenance-sealed CSVs keyed to source_pipeline sleeves.
"""
from __future__ import annotations

import math
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any


MACRO_REGIME_FIELDS = [
    "run_as_of",
    "macro_as_of_date",
    "active_current_regime",
    "active_next_regime",
    "current_confidence",
    "next_confidence",
    "coverage_flag",
    "regime_override_reason",
    "staleness_days",
]

MACRO_SECTOR_FIELDS = [
    "run_as_of",
    "source_pipeline",
    "macro_as_of_date",
    "macro_level",
    "macro_key",
    "macro_sector_name",
    "target_weight",
    "macro_fit_score",
    "coverage_flag",
    "fallback_used",
    "fallback_reason",
    "staleness_days",
]

MACRO_STOCK_FIELDS = [
    "run_as_of",
    "ticker",
    "source_pipeline",
    "macro_as_of_date",
    "macro_stock_fit_z",
    "industry_macro_fit",
    "industry_aggregate_macro_fit",
    "sector_macro_fit",
    "coverage_flag",
    "fallback_used",
    "fallback_reason",
    "staleness_days",
]

MACRO_COUNTRY_FIELDS = [
    "run_as_of",
    "ticker",
    "macro_as_of_date",
    "ref_area",
    "country_name",
    "region",
    "market_class",
    "country_macro_fit",
    "confidence_adjusted_fit",
    "coverage_flag",
    "staleness_days",
]

MACRO_FOREIGN_BUDGET_FIELDS = [
    "run_as_of",
    "macro_as_of_date",
    "active_flag",
    "foreign_budget",
    "min_budget",
    "max_budget",
    "eligible_candidate_count",
    "selected_candidate_count",
    "activation_reason",
    "coverage_flag",
    "staleness_days",
]

MACRO_FOREIGN_CANDIDATE_FIELDS = [
    "run_as_of",
    "ticker",
    "macro_as_of_date",
    "market_name",
    "region",
    "candidate_score",
    "sleeve_weight",
    "portfolio_weight_at_budget",
    "eligible_flag",
    "selected_flag",
    "active_flag",
    "coverage_flag",
    "staleness_days",
]

MACRO_SERVING_CONTRACT_TABLES = [
    "macro_regime_decision_daily",
    "sector_macro_fit_daily",
    "industry_macro_fit_daily",
    "industry_aggregate_macro_fit_daily",
    "stock_macro_fit_daily",
    "country_macro_fit_daily",
    "foreign_sleeve_budget_daily",
    "foreign_sleeve_candidate_daily",
]

REGIME_SOURCE_TABLES = {
    "v1": "macro_regime_decision_daily",
    "v2": "macro_regime_v2_decision_daily",
    # H1 hybrid decisions live in the v2-family decision table under the H1 model_version
    # (H1_CANDIDATE_SPEC.md); promotion is gated on sealed H1 evidence, not the v2 summary.
    "h1": "macro_regime_v2_decision_daily",
}

MACRO_REGIME_LABELS = {
    "EXPANSION_DISINFLATION",
    "HEATING_UP",
    "SLOW_GROWTH",
    "STAGFLATION",
}


def finite_or_blank(value: Any) -> float | str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return ""
    return parsed if math.isfinite(parsed) else ""


def int_or_blank(value: Any) -> int | str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(parsed):
        return ""
    return int(parsed)


def regime_application_errors(row: dict[str, Any] | None) -> list[str]:
    """Return reasons a regime row must not influence portfolio allocation."""
    if row is None:
        return ["missing_regime_row"]

    errors: list[str] = []
    coverage = finite_or_blank(row.get("coverage_flag"))
    if coverage == "" or float(coverage) != 1.0:
        coverage_detail = repr(coverage) if coverage == "" else f"{float(coverage):g}"
        errors.append(f"coverage_flag={coverage_detail}")

    current = str(row.get("active_current_regime") or "").strip()
    next_regime = str(row.get("active_next_regime") or "").strip()
    if current not in MACRO_REGIME_LABELS:
        errors.append(f"invalid_current_regime={current!r}")
    if next_regime not in MACRO_REGIME_LABELS:
        errors.append(f"invalid_next_regime={next_regime!r}")

    for field in ("current_confidence", "next_confidence"):
        value = finite_or_blank(row.get(field))
        if value == "" or not 0.0 <= float(value) <= 1.0:
            errors.append(f"invalid_{field}={row.get(field)!r}")

    reason = str(row.get("regime_override_reason") or "").strip().upper()
    if "UNCOVERED" in reason:
        errors.append(f"override_reason={reason}")
    return errors


def staleness_days(run_as_of: str, macro_as_of: str) -> int | None:
    try:
        return (date.fromisoformat(run_as_of) - date.fromisoformat(macro_as_of)).days
    except (TypeError, ValueError):
        return None


def open_macro_serving_db(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Macro serving DB not found: {resolved}")
    uri = f"file:{resolved.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_snapshot_inputs(path: Path) -> dict[str, Path]:
    """Return authoritative SQLite files that define the readable snapshot.

    In WAL mode, uncheckpointed committed pages live in ``<db>-wal``. The ``-shm`` file is an
    ephemeral shared-memory index and is deliberately not hashed.
    """
    resolved = path.expanduser().resolve()
    inputs = {resolved.name: resolved}
    wal = Path(f"{resolved}-wal")
    if wal.exists():
        inputs[wal.name] = wal
    return inputs


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return [str(row["name"]) for row in rows]


def _digest_value(value: Any) -> bytes:
    if value is None:
        return b"<NULL>"
    if isinstance(value, bytes):
        return b"<BLOB>" + value.hex().encode("ascii")
    return str(value).encode("utf-8")


def regime_table_for_source(source: str) -> str:
    normalized = str(source or "").strip().lower()
    if normalized not in REGIME_SOURCE_TABLES:
        raise ValueError(f"Unsupported macro regime source {source!r}; expected one of {sorted(REGIME_SOURCE_TABLES)}.")
    return REGIME_SOURCE_TABLES[normalized]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_v2_promotion_manifest(
    path: Path,
    *,
    model_version: str,
    macro_config_path: Path,
    builder_path: Path,
    allowed_root: Path,
) -> list[str]:
    """Verify a v2 promotion seal and every artifact it transitively pins."""
    resolved = path.expanduser().resolve()
    root = allowed_root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return [f"manifest_outside_v2_output_root={resolved}"]
    if not resolved.is_file():
        return [f"missing_manifest={resolved}"]
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"unreadable_manifest={type(exc).__name__}"]

    errors: list[str] = []
    if payload.get("acceptance") != "PROMOTABLE":
        errors.append(f"manifest_acceptance={payload.get('acceptance')}")
    if payload.get("model_version") != model_version:
        errors.append("manifest_model_version_mismatch")
    if not macro_config_path.is_file() or payload.get("config_sha256") != _file_sha256(macro_config_path):
        errors.append("macro_config_hash_mismatch")
    if not builder_path.is_file() or payload.get("builder_sha256") != _file_sha256(builder_path):
        errors.append("promotion_builder_hash_mismatch")

    for section, prefix in (
        ("files", "promotion_artifact"),
        ("upstream_files", "promotion_upstream"),
    ):
        entries = payload.get(section)
        if not isinstance(entries, dict):
            errors.append(f"invalid_{section}_mapping")
            continue
        for filename, expected_hash in entries.items():
            artifact = (resolved.parent / str(filename)).resolve()
            try:
                artifact.relative_to(resolved.parent)
            except ValueError:
                errors.append(f"{prefix}_outside_manifest_dir:{filename}")
                continue
            if not artifact.is_file() or _file_sha256(artifact) != str(expected_hash):
                errors.append(f"{prefix}_hash_mismatch:{filename}")
    return errors


def macro_serving_content_sha256(
    path: Path,
    run_as_of: str,
    *,
    regime_table: str = "macro_regime_decision_daily",
    regime_model_version: str | None = None,
) -> str:
    """Hash the deterministic serving DB rows consumed by the Stage 6 contract.

    The live SQLite file and its WAL sidecar are mutable storage artifacts: checkpoints, readers,
    and journal state can change their bytes without changing the portfolio-visible macro
    contract. This digest hashes the latest PIT rows Stage 6 reads from each serving table instead.
    """
    h = hashlib.sha256()
    conn = open_macro_serving_db(path)
    try:
        selected_regime_table = regime_table_for_source(
            "v2" if regime_table == REGIME_SOURCE_TABLES["v2"] else "v1"
        )
        if selected_regime_table != regime_table:
            raise ValueError(f"Unsupported regime_table={regime_table!r}.")
        if selected_regime_table == REGIME_SOURCE_TABLES["v2"] and not str(regime_model_version or "").strip():
            raise ValueError("regime_model_version is required when hashing the v2 regime source.")
        tables = [
            selected_regime_table if table == REGIME_SOURCE_TABLES["v1"] else table
            for table in MACRO_SERVING_CONTRACT_TABLES
        ]
        h.update(f"run_as_of={run_as_of}\n".encode("utf-8"))
        h.update(f"regime_table={selected_regime_table}\n".encode("utf-8"))
        h.update(f"regime_model_version={regime_model_version or ''}\n".encode("utf-8"))
        for table in tables:
            columns = _table_columns(conn, table)
            h.update(f"table={table}\n".encode("utf-8"))
            if not columns:
                h.update(b"missing_table\n")
                continue
            h.update(("columns=" + "\x1f".join(columns) + "\n").encode("utf-8"))
            if "as_of_date" not in columns:
                h.update(b"missing_as_of_date\n")
                continue
            model_filter = regime_model_version if table == REGIME_SOURCE_TABLES["v2"] else None
            as_of = latest_as_of(conn, table, run_as_of, model_version=model_filter)
            h.update(f"as_of={as_of or ''}\n".encode("utf-8"))
            if not as_of:
                continue
            quoted_table = _quote_identifier(table)
            order_clause = ", ".join(_quote_identifier(col) for col in columns)
            if model_filter is None:
                sql = f"SELECT * FROM {quoted_table} WHERE as_of_date = ? ORDER BY {order_clause}"
                params: tuple[Any, ...] = (as_of,)
            else:
                sql = (
                    f"SELECT * FROM {quoted_table} WHERE as_of_date = ? AND model_version = ? "
                    f"ORDER BY {order_clause}"
                )
                params = (as_of, model_filter)
            row_count = 0
            for row in conn.execute(sql, params):
                row_count += 1
                for col in columns:
                    h.update(col.encode("utf-8"))
                    h.update(b"=")
                    h.update(_digest_value(row[col]))
                    h.update(b"\x1e")
                h.update(b"\n")
            h.update(f"rows={row_count}\n".encode("utf-8"))
    finally:
        conn.close()
    return h.hexdigest()


def latest_as_of(
    conn: sqlite3.Connection,
    table: str,
    run_as_of: str,
    *,
    model_version: str | None = None,
) -> str | None:
    if model_version is None:
        row = conn.execute(
            f"SELECT MAX(as_of_date) AS as_of_date FROM {table} WHERE as_of_date <= ?",
            (run_as_of,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT MAX(as_of_date) AS as_of_date FROM {table} WHERE as_of_date <= ? AND model_version = ?",
            (run_as_of, model_version),
        ).fetchone()
    value = None if row is None else row["as_of_date"]
    return str(value) if value else None


def rows_at_latest(conn: sqlite3.Connection, table: str, run_as_of: str) -> tuple[str | None, list[sqlite3.Row]]:
    as_of = latest_as_of(conn, table, run_as_of)
    if not as_of:
        return None, []
    rows = conn.execute(f"SELECT * FROM {table} WHERE as_of_date = ?", (as_of,)).fetchall()
    return as_of, list(rows)


def single_latest_row(conn: sqlite3.Connection, table: str, run_as_of: str) -> sqlite3.Row | None:
    as_of = latest_as_of(conn, table, run_as_of)
    if not as_of:
        return None
    return conn.execute(f"SELECT * FROM {table} WHERE as_of_date = ? LIMIT 1", (as_of,)).fetchone()


def single_latest_regime_row(
    conn: sqlite3.Connection,
    *,
    source: str,
    run_as_of: str,
    model_version: str | None = None,
    covered_only: bool = False,
) -> sqlite3.Row | None:
    normalized_source = str(source or "").strip().lower()
    table = regime_table_for_source(normalized_source)
    if normalized_source == "v1":
        if covered_only:
            return conn.execute(
                f"""
                SELECT *
                FROM {table}
                WHERE as_of_date <= ? AND coverage_flag = 1
                ORDER BY as_of_date DESC
                LIMIT 1
                """,
                (run_as_of,),
            ).fetchone()
        return single_latest_row(conn, table, run_as_of)
    model = str(model_version or "").strip()
    if not model:
        raise ValueError("A model_version is required for the v2 regime source.")
    if covered_only:
        return conn.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE as_of_date <= ? AND model_version = ? AND coverage_flag = 1
            ORDER BY as_of_date DESC
            LIMIT 1
            """,
            (run_as_of, model),
        ).fetchone()
    as_of = latest_as_of(conn, table, run_as_of, model_version=model)
    if not as_of:
        return None
    return conn.execute(
        f"SELECT * FROM {table} WHERE as_of_date = ? AND model_version = ? LIMIT 1",
        (as_of, model),
    ).fetchone()


def v2_promotion_status(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    run_as_of: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM macro_regime_v2_promotion_summary
        WHERE model_version = ? AND evidence_as_of_date <= ?
        ORDER BY evidence_as_of_date DESC
        LIMIT 1
        """,
        (model_version, run_as_of),
    ).fetchone()


def _verify_h1_manifest(manifest_path: Path, *, model_version: str, evidence_as_of_date: str) -> list[str]:
    """Recompute and compare EVERY hash sealed in the H1 promotion manifest (A1.6). Fail-closed.

    Anchors are rederived from the manifest's own location so nothing in the manifest can
    redirect verification elsewhere. The layout is MacroLayer/out/regime_h1/<date>/manifest:
      evidence_dir = manifest dir; output_root = regime_h1 (parent); macro_root = MacroLayer
      (great-grandparent, i.e. output_root's grandparent).
    A null sealed hash means the file was intentionally absent at seal time and must still be
    absent; a non-null hash requires the file to exist and match byte-for-byte.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"unreadable_h1_manifest:{type(exc).__name__}"]

    errors: list[str] = []
    if str(manifest.get("model_version") or "") != str(model_version):
        errors.append("manifest_model_version_mismatch")
    if str(manifest.get("evidence_as_of_date") or "") != str(evidence_as_of_date):
        errors.append("manifest_evidence_date_mismatch")

    evidence_dir = manifest_path.parent
    output_root = evidence_dir.parent
    macro_root = evidence_dir.parent.parent.parent
    anchor_dirs = {
        "evidence_dir": evidence_dir,
        "output_root": output_root,
        "macro_root": macro_root,
        # AMENDMENT 2 (A2.4): portfolio-layer root (16d source + sealed A1.7 gate) sits one level
        # above MacroLayer.
        "portfolio_root": macro_root.parent,
    }
    anchors = manifest.get("anchors")
    if not isinstance(anchors, dict):
        return errors + ["manifest_missing_anchors"]
    for anchor_name, base_dir in anchor_dirs.items():
        entries = anchors.get(anchor_name)
        if not isinstance(entries, dict):
            errors.append(f"manifest_missing_anchor:{anchor_name}")
            continue
        for filename, expected in entries.items():
            artifact = (base_dir / str(filename)).resolve()
            try:
                artifact.relative_to(base_dir.resolve())
            except ValueError:
                errors.append(f"manifest_path_escape:{anchor_name}/{filename}")
                continue
            if expected is None:
                if artifact.exists():
                    errors.append(f"manifest_unexpected_present:{anchor_name}/{filename}")
                continue
            if not artifact.is_file():
                errors.append(f"manifest_missing_file:{anchor_name}/{filename}")
                continue
            if _file_sha256(artifact) != str(expected):
                errors.append(f"manifest_hash_mismatch:{anchor_name}/{filename}")

    # AMENDMENT 2 (A2.4/A2.8): cross-check the manifest's recorded canonical config-block sha
    # against the (already hash-verified) drift baseline, so config-block drift cannot slip through
    # even though the blocks are sealed transitively via config_macro_raw.yaml.
    manifest_cfg_sha = manifest.get("config_block_sha256")
    if manifest_cfg_sha is None:
        errors.append("manifest_missing_config_block_sha256")
    else:
        baseline_path = output_root / "h1_prospective_baseline.json"
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append("h1_baseline_unreadable_for_config_crosscheck")
        else:
            if str(baseline.get("config_blocks_sha256") or "") != str(manifest_cfg_sha):
                errors.append("manifest_config_block_sha_mismatch")
    return errors


def h1_promotion_status(
    *,
    output_root: Path,
    run_as_of: str,
    model_version: str,
) -> tuple[Path | None, list[str]]:
    """Latest sealed H1 promotion evidence at or before run_as_of. Fail-closed.

    Returns (evidence_path, errors); the h1 regime source is usable ONLY when errors is
    empty, which requires (a) a sealed ``h1_promotion_manifest.json`` next to the evidence
    JSON whose EVERY hash re-verifies (spec, config, ledger, baseline, builder sources, and
    the evidence JSON itself), and (b) acceptance == PROMOTABLE under the frozen prospective
    contract (H1_CANDIDATE_SPEC.md + AMENDMENT 1).
    """
    if not output_root.exists():
        return None, ["missing_h1_output_root"]
    candidates = sorted(
        child for child in output_root.iterdir() if child.is_dir() and child.name <= run_as_of
    )
    for child in reversed(candidates):
        path = child / "h1_promotion_evidence.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return path, [f"unreadable_h1_evidence:{type(exc).__name__}"]
        errors: list[str] = []
        if str(payload.get("model_version") or "") != str(model_version):
            errors.append("h1_model_version_mismatch")

        evidence_as_of_date = str(payload.get("evidence_as_of_date") or child.name)
        manifest_path = child / "h1_promotion_manifest.json"
        if not manifest_path.is_file():
            errors.append("missing_h1_promotion_manifest")
        else:
            errors.extend(
                _verify_h1_manifest(
                    manifest_path, model_version=model_version, evidence_as_of_date=evidence_as_of_date
                )
            )

        if str(payload.get("acceptance") or "") != "PROMOTABLE":
            errors.append(f"acceptance={payload.get('acceptance')}")
        return path, errors
    return None, ["missing_h1_promotion_evidence"]
