#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_industrials_sec_positioning_stages")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
POSITIONING_STAGE = "import_industrials_positioning"
CSV_FIELDS = [
    "ticker",
    "is_active",
    "form4_rows",
    "form4_status",
    "form4_status_reason",
    "institutional_rows",
    "short_interest_rows",
    "borrow_rows",
    "feature_asof",
    "feature_quality",
    "missing_fields",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate industrials Stage 5 positioning coverage.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to validate, e.g. defense.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--allow-missing-borrow", action="store_true", help="Downgrade missing IBKR borrow coverage to warning.")
    parser.add_argument("--13f-exempt-tickers", default="", help="Comma-separated tickers with explicit 13F no-row exemptions.")
    parser.add_argument("--borrow-exempt-tickers", default="", help="Comma-separated tickers with explicit IBKR borrow no-row exemptions.")
    return parser.parse_args()


def scalar(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def value(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def cfg_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg_get(config, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def cfg_ticker_set(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = []
    return {ticker for ticker in (normalize_ticker(value) for value in values) if ticker}


def cfg_ticker_list(raw: Any) -> list[str]:
    return sorted(cfg_ticker_set(raw))


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text, text[:10], text[:11]]
    if "T" in text:
        candidates.append(text.split("T", 1)[0])
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y"):
        for candidate in candidates:
            try:
                return datetime.strptime(candidate.upper(), fmt).date()
            except ValueError:
                continue
    return None


def parse_date_strict(raw: object, *, field: str, context: str = "") -> date | None:
    """Parse an operator-supplied config/CSV date; raise loudly on garbage.

    Empty means "not set". A malformed value must never fail open into
    "gate always satisfied" / "exemption never expires".
    """
    text = str(raw or "").strip()
    if not text:
        return None
    parsed = parse_date(text)
    if parsed is None:
        where = f" for {context}" if context else ""
        raise ValueError(f"Unparseable {field} date {text!r}{where}; expected YYYY-MM-DD")
    return parsed


def institutional_13f_gate_config(config: dict[str, Any]) -> tuple[date | None, list[str], int]:
    gate = cfg_get(config, "positioning_import.institutional_13f_data_gate", {}) or {}
    required_period = parse_date_strict(
        gate.get("required_period_of_report"),
        field="positioning_import.institutional_13f_data_gate.required_period_of_report",
    )
    anchor_tickers = cfg_ticker_list(gate.get("anchor_tickers", []))
    min_anchor_count = int(gate.get("min_anchor_tickers_with_period", len(anchor_tickers) if anchor_tickers else 0) or 0)
    return required_period, anchor_tickers, min_anchor_count


def institutional_13f_period_available(
    conn: Any,
    *,
    required_period: date | None,
    anchor_tickers: list[str],
    min_anchor_count: int,
    source_id: str,
) -> bool:
    if required_period is None:
        return True
    row = conn.execute(
        """
        SELECT MAX(period_of_report) AS max_period
        FROM fact_13f_positioning
        WHERE source_id = ?
          AND COALESCE(period_of_report, '') <> ''
        """,
        (source_id,),
    ).fetchone()
    max_period = parse_date(row["max_period"] if row is not None else "")
    if max_period is None or max_period < required_period:
        return False
    if not anchor_tickers or min_anchor_count <= 0:
        return True
    anchors = sorted(set(anchor_tickers))
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT ticker) AS covered
        FROM fact_13f_positioning
        WHERE source_id = ?
          AND ticker IN ({placeholders(anchors)})
          AND period_of_report >= ?
        """,
        (source_id, *anchors, required_period.isoformat()),
    ).fetchone()
    covered = int(row["covered"] or 0) if row is not None else 0
    return covered >= min_anchor_count


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_exempt_tickers(
    config: dict[str, Any],
    *,
    base_dir: Path,
    config_key: str,
    override_flag: str,
    asof: date,
    until_key: str = "",
    ignore_until: bool = False,
) -> set[str]:
    """Load exemption overrides effective at the evaluation asof.

    `valid_from` gates effectiveness same-day-inclusive at the evaluation asof
    (med_devices convention); `reviewed_at` is provenance documentation only.
    Expiry windows are also evaluated at the asof, never wall-clock today, so
    historical rebuilds reproduce the exemption state that held at that date.
    """
    out = cfg_ticker_set(cfg_get(config, config_key, []))
    path_value = cfg_get(config, "positioning_import.positioning_overrides_csv", "")
    if path_value:
        path = resolve_path(path_value, base_dir=base_dir)
        for row in read_csv_rows(path):
            ticker = normalize_ticker(row.get("ticker"))
            if not ticker:
                continue
            valid_from = parse_date_strict(row.get("valid_from"), field="valid_from", context=ticker)
            if valid_from is not None and valid_from > asof:
                continue
            exempt = str(row.get(override_flag) or "").strip().lower() in {"1", "true", "yes", "y"}
            until = parse_date_strict(row.get(until_key), field=until_key, context=ticker) if until_key else None
            active = until is None or asof <= until or ignore_until
            if exempt and active:
                out.add(ticker)
    return out


def load_active_universe(conn: Any, model_family: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    return [normalize_ticker(row["ticker"]) for row in rows if normalize_ticker(row["ticker"])]


def load_inactive_universe(conn: Any, model_family: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT m.ticker
        FROM dim_universe_membership m
        JOIN dim_company c
          ON c.ticker = m.ticker
        WHERE m.model_family = ?
          AND m.is_current_member = 0
          AND c.is_active = 0
        ORDER BY m.ticker
        """,
        (model_family,),
    ).fetchall()
    return [normalize_ticker(row["ticker"]) for row in rows if normalize_ticker(row["ticker"])]


def count_by_ticker(conn: Any, table: str, tickers: list[str], source_id: str) -> dict[str, int]:
    if not tickers:
        return {}
    rows = conn.execute(
        f"""
        SELECT ticker, COUNT(*) AS row_count
        FROM {table}
        WHERE source_id = ?
          AND ticker IN ({placeholders(tickers)})
        GROUP BY ticker
        """,
        (source_id, *tickers),
    ).fetchall()
    counts = {ticker: 0 for ticker in tickers}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker in counts:
            counts[ticker] = int(row["row_count"] or 0)
    return counts


def latest_features(conn: Any, tickers: list[str], source_id: str, model_family: str, asof: str) -> dict[str, dict[str, str]]:
    if not tickers or not asof:
        return {}
    rows = conn.execute(
        f"""
        SELECT ticker, positioning_quality, latest_institutional_shares,
               latest_short_interest_pct_float, latest_borrow_fee_rate,
               form4_status, form4_status_reason
        FROM feature_positioning
        WHERE source_id = ?
          AND model_family = ?
          AND asof_date = ?
          AND ticker IN ({placeholders(tickers)})
        """,
        (source_id, model_family, asof, *tickers),
    ).fetchall()
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        missing: list[str] = []
        if row["latest_institutional_shares"] is None:
            missing.append("13f")
        if row["latest_short_interest_pct_float"] is None:
            missing.append("short_interest")
        if row["latest_borrow_fee_rate"] is None:
            missing.append("borrow")
        ticker = normalize_ticker(row["ticker"])
        out[ticker] = {
            "quality": str(row["positioning_quality"] or ""),
            "missing_fields": ";".join(missing),
            "form4_status": str(row["form4_status"] or ""),
            "form4_status_reason": str(row["form4_status_reason"] or ""),
        }
    return out


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(path, CSV_FIELDS, rows)


def validate() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "positioning_import.feature_output_csv"), base_dir=base_dir)
    )
    model_family = str(
        args.model_family
        or cfg_get(config, "industrials_universe.initial_subsector", "defense")
        or "defense"
    ).strip()
    form4_source = str(cfg_get(config, "positioning_import.form4_source_id", "sec_insider_upstream"))
    direct_ownership_source = str(cfg_get(config, "positioning_import.direct_ownership_source_id", "sec_ownership_direct"))
    mp_source = str(cfg_get(config, "positioning_import.market_positioning_source_id", "market_positioning_upstream"))
    positioning_source = str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))
    require_13f = cfg_bool(config, "positioning_import.require_upstream_13f_for_gate", False)
    require_short = cfg_bool(config, "positioning_import.require_upstream_short_for_gate", False)
    require_short_pct_float = cfg_bool(config, "positioning_import.require_short_pct_float_for_gate", False)
    require_borrow = cfg_bool(config, "positioning_import.require_upstream_borrow_for_gate", False) and not bool(args.allow_missing_borrow)
    min_form4_covered_fraction = float(
        cfg_get(config, "positioning_validation.min_form4_covered_fraction", 0.0) or 0.0
    )
    if not 0.0 <= min_form4_covered_fraction <= 1.0:
        raise ValueError(
            "positioning_validation.min_form4_covered_fraction must be in [0, 1]; "
            f"got {min_form4_covered_fraction!r}"
        )
    if min_form4_covered_fraction <= 0.0:
        LOGGER.warning(
            "positioning_validation.min_form4_covered_fraction is 0.0: a wholly missing "
            "insider feed (or a routing regression) will still pass Stage 5 validation."
        )
    cli_exempt_13f = cfg_ticker_set(args.__dict__.get("13f_exempt_tickers", ""))

    errors: list[str] = []
    warnings: list[str] = []
    report_rows: list[dict[str, Any]] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        feature_asof = str(
            value(
                conn,
                "SELECT MAX(asof_date) FROM feature_positioning WHERE source_id = ? AND model_family = ?",
                (positioning_source, model_family),
            )
            or ""
        )
        # Exemption windows must be evaluated at the feature/evaluation asof,
        # never wall-clock today, or historical rebuilds flip exemptions
        # retroactively once an expiry passes.
        evaluation_asof = parse_date(feature_asof)
        if evaluation_asof is None:
            evaluation_asof = date.today()
            warnings.append(
                "No parseable positioning feature asof; evaluating exemption windows at wall-clock today."
            )
        required_13f_period, anchor_13f_tickers, min_13f_anchor_count = institutional_13f_gate_config(config)
        institutional_13f_data_available = institutional_13f_period_available(
            conn,
            required_period=required_13f_period,
            anchor_tickers=anchor_13f_tickers,
            min_anchor_count=min_13f_anchor_count,
            source_id=mp_source,
        )
        exempt_13f = load_exempt_tickers(
            config,
            base_dir=base_dir,
            config_key="positioning_import.upstream_13f_gate_exempt_tickers",
            override_flag="institutional_13f_exempt",
            asof=evaluation_asof,
            until_key="institutional_13f_exempt_until",
            ignore_until=not institutional_13f_data_available,
        )
        exempt_13f.update(cli_exempt_13f)
        exempt_form4 = load_exempt_tickers(
            config,
            base_dir=base_dir,
            config_key="positioning_import.upstream_form4_gate_exempt_tickers",
            override_flag="form4_exempt",
            asof=evaluation_asof,
        )
        exempt_short = load_exempt_tickers(
            config,
            base_dir=base_dir,
            config_key="positioning_import.upstream_short_gate_exempt_tickers",
            override_flag="short_interest_exempt",
            asof=evaluation_asof,
        )
        exempt_short_pct_float = load_exempt_tickers(
            config,
            base_dir=base_dir,
            config_key="positioning_import.upstream_short_pct_float_gate_exempt_tickers",
            override_flag="short_pct_float_exempt",
            asof=evaluation_asof,
        )
        exempt_borrow = load_exempt_tickers(
            config,
            base_dir=base_dir,
            config_key="positioning_import.upstream_borrow_gate_exempt_tickers",
            override_flag="borrow_exempt",
            asof=evaluation_asof,
        )
        exempt_borrow.update(cfg_ticker_set(args.borrow_exempt_tickers))
        active = load_active_universe(conn, model_family)
        inactive = load_inactive_universe(conn, model_family)
        all_tickers = sorted(set(active) | set(inactive))
        if not active:
            errors.append(f"No active industrials universe tickers found for model_family={model_family}")
            active = ["__NO_ACTIVE_TICKERS__"]
        if not all_tickers:
            all_tickers = active

        for source_id in (form4_source, mp_source, positioning_source):
            status = value(conn, "SELECT status FROM source_registry WHERE source_id = ?", (source_id,))
            if status != "active":
                errors.append(f"Source {source_id} is not active in source_registry: {status!r}")

        form4_counts = count_by_ticker(conn, "fact_sec_form4_transaction", all_tickers, form4_source)
        direct_counts = count_by_ticker(conn, "fact_sec_form4_transaction", all_tickers, direct_ownership_source)
        inst_counts = count_by_ticker(conn, "fact_13f_positioning", all_tickers, mp_source)
        short_counts = count_by_ticker(conn, "fact_short_interest", all_tickers, mp_source)
        borrow_counts = count_by_ticker(conn, "fact_ibkr_borrow_snapshot", all_tickers, mp_source)
        feature_map = latest_features(conn, active, positioning_source, model_family, feature_asof)

        active_set = set(active)
        for ticker in all_tickers:
            is_active = ticker in active_set
            feature = feature_map.get(ticker, {})
            report_rows.append(
                {
                    "ticker": ticker,
                    "is_active": int(is_active),
                    "form4_rows": form4_counts.get(ticker, 0) + direct_counts.get(ticker, 0),
                    "form4_status": feature.get("form4_status", "") if is_active else "",
                    "form4_status_reason": feature.get("form4_status_reason", "") if is_active else "",
                    "institutional_rows": inst_counts.get(ticker, 0),
                    "short_interest_rows": short_counts.get(ticker, 0),
                    "borrow_rows": borrow_counts.get(ticker, 0),
                    "feature_asof": feature_asof if is_active else "",
                    "feature_quality": feature.get("quality", "") if is_active else "",
                    "missing_fields": feature.get("missing_fields", "") if is_active else "",
                }
            )

        active_missing_feature = sorted(ticker for ticker in active if ticker not in feature_map)
        active_missing_13f = sorted(ticker for ticker in active if inst_counts.get(ticker, 0) == 0 and ticker not in exempt_13f)
        active_missing_short = sorted(ticker for ticker in active if short_counts.get(ticker, 0) == 0 and ticker not in exempt_short)
        active_missing_short_pct = sorted(
            ticker
            for ticker in active
            if ticker not in exempt_short
            and ticker not in exempt_short_pct_float
            and ticker in feature_map
            and "short_interest" in set(feature_map[ticker].get("missing_fields", "").split(";"))
        )
        active_missing_borrow = sorted(ticker for ticker in active if borrow_counts.get(ticker, 0) == 0 and ticker not in exempt_borrow)
        inactive_missing_any = sorted(
            ticker
            for ticker in inactive
            if form4_counts.get(ticker, 0) + direct_counts.get(ticker, 0) == 0
            or inst_counts.get(ticker, 0) == 0
            or short_counts.get(ticker, 0) == 0
            or borrow_counts.get(ticker, 0) == 0
        )

        form4_gate_universe = sorted(
            ticker for ticker in active if ticker not in exempt_form4 and ticker != "__NO_ACTIVE_TICKERS__"
        )
        form4_covered_count = sum(
            1
            for ticker in form4_gate_universe
            if form4_counts.get(ticker, 0) + direct_counts.get(ticker, 0) > 0
        )
        form4_covered_fraction = (
            form4_covered_count / len(form4_gate_universe) if form4_gate_universe else 0.0
        )

        if not feature_asof:
            errors.append("No positioning feature rows loaded.")
        if active_missing_feature:
            errors.append(f"Positioning feature coverage missing active tickers: {active_missing_feature}")
        if min_form4_covered_fraction > 0.0 and form4_covered_fraction < min_form4_covered_fraction:
            errors.append(
                "Form 4 coverage below required floor: covered "
                f"{form4_covered_count}/{len(form4_gate_universe)} non-exempt active tickers "
                f"({form4_covered_fraction:.3f} < min {min_form4_covered_fraction:.3f}). "
                "A wholly missing insider feed or a Form 4 routing regression looks exactly like this."
            )
        if require_13f and active_missing_13f:
            errors.append(f"13F coverage required; missing active non-exempt tickers: {active_missing_13f}")
        if require_short and active_missing_short:
            errors.append(f"Short-interest coverage required; missing active tickers: {active_missing_short}")
        if require_short and require_short_pct_float and active_missing_short_pct:
            errors.append(
                "Short-interest percent-of-float required; active tickers with rows but missing pct-float: "
                f"{active_missing_short_pct}"
            )
        elif require_short and active_missing_short_pct:
            warnings.append(
                "Short-interest pct-of-float unavailable (FINRA free feed has no float shares); "
                f"tickers={len(active_missing_short_pct)}. Enable positioning_import.require_short_pct_float_for_gate "
                "only when a float source is wired."
            )
        if require_borrow and active_missing_borrow:
            errors.append(f"Borrow coverage required; missing active non-exempt tickers: {active_missing_borrow}")
        if inactive_missing_any:
            warnings.append(f"Inactive calibration tickers with at least one missing positioning source: {inactive_missing_any}")

        positioning_issue_count = scalar(
            conn,
            "SELECT COUNT(*) FROM data_quality_issues WHERE stage = ? AND resolution_status = 'open'",
            (POSITIONING_STAGE,),
        )
        active_complete_features = sum(1 for ticker in active if feature_map.get(ticker, {}).get("quality") == "complete")
        warnings.append(f"Active universe tickers={len(active)} inactive_calibration_tickers={len(inactive)}")
        warnings.append(f"Latest positioning feature asof={feature_asof or 'NONE'}")
        warnings.append(
            "13F data availability gate="
            f"required_period={required_13f_period.isoformat() if required_13f_period else ''} "
            f"anchors={anchor_13f_tickers} min_anchor={min_13f_anchor_count} "
            f"available={institutional_13f_data_available}"
        )
        warnings.append(
            "Form 4 covered active/inactive="
            f"{sum(1 for t in active if form4_counts.get(t, 0) + direct_counts.get(t, 0) > 0)}/{len(active)} "
            f"{sum(1 for t in inactive if form4_counts.get(t, 0) + direct_counts.get(t, 0) > 0)}/{len(inactive)} "
            f"non_exempt_covered_fraction={form4_covered_fraction:.3f} "
            f"min_required={min_form4_covered_fraction:.3f} "
            f"exemptions={sorted(exempt_form4)}"
        )
        warnings.append(
            f"13F covered active/inactive={sum(1 for t in active if inst_counts.get(t, 0) > 0)}/{len(active)} "
            f"{sum(1 for t in inactive if inst_counts.get(t, 0) > 0)}/{len(inactive)} "
            f"required={require_13f} exemptions={sorted(exempt_13f)}"
        )
        warnings.append(
            f"Short-interest covered active/inactive={sum(1 for t in active if short_counts.get(t, 0) > 0)}/{len(active)} "
            f"{sum(1 for t in inactive if short_counts.get(t, 0) > 0)}/{len(inactive)} required={require_short} exemptions={sorted(exempt_short)}"
        )
        warnings.append(f"Short-interest pct-float exemptions={sorted(exempt_short_pct_float)}")
        warnings.append(
            f"Borrow covered active/inactive={sum(1 for t in active if borrow_counts.get(t, 0) > 0)}/{len(active)} "
            f"{sum(1 for t in inactive if borrow_counts.get(t, 0) > 0)}/{len(inactive)} required={require_borrow}"
        )
        warnings.append(f"Complete active positioning feature rows={active_complete_features}/{len(active)} open_stage5_issues={positioning_issue_count}")

    write_report(output_csv, report_rows)
    for message in warnings:
        LOGGER.info(message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        LOGGER.error("Wrote positioning validation report: %s", output_csv)
        return 1
    LOGGER.info("Industrials Stage 5 positioning validation passed for model_family=%s", model_family)
    LOGGER.info("Wrote positioning validation report: %s", output_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(validate())
