from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from technology.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path
from technology.core.db import connect, finish_run, init_db, start_run, utc_now
from technology.core.logging_utils import configure_utc_logging
from technology.core.source_registry import load_source_registry, upsert_source_registry
from technology.core.text_norm import normalize_ticker


LOGGER = logging.getLogger("semiconductor_sector_overlays")

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
DEFAULT_USER_AGENT = "JL, Independent Research, jm.357@hotmail.com"
MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
WSTS_REGIONS = {"Americas", "Europe", "Japan", "Asia Pacific", "Worldwide"}
CAPEX_CONCEPT_PRIORITY = {
    "PaymentsToAcquirePropertyPlantAndEquipment": 1,
    "PaymentsToAcquireProductiveAssets": 2,
}


@dataclass(frozen=True)
class RuntimePaths:
    config_path: Path
    config: dict[str, Any]
    base_dir: Path
    db_path: Path


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Feature as-of date. Defaults to today.")
    parser.add_argument("--manual-xlsx", type=Path, default=None, help="Optional local WSTS workbook override.")
    return parser.parse_args()


def runtime_paths(args: argparse.Namespace) -> RuntimePaths:
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    return RuntimePaths(config_path=config_path, config=config, base_dir=base_dir, db_path=db_path)


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_asof(raw: object) -> date:
    return parse_date(raw) or date.today()


def safe_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    value = num / den
    return value if math.isfinite(value) else None


def pct_change(value: float | None, prior: float | None) -> float | None:
    ratio = safe_div(value, prior)
    return ratio - 1.0 if ratio is not None else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def pct_to_score(value: float | None, scale: float) -> float:
    if value is None:
        return 50.0
    return clamp(50.0 + value * scale)


def qmarks(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def load_registry(conn: Any, paths: RuntimePaths) -> None:
    registry_path = resolve_path(cfg_get(paths.config, "source_registry.path"), base_dir=paths.base_dir)
    upsert_source_registry(conn, load_source_registry(registry_path))


def user_agent(config: dict[str, Any], key: str = "sec_fundamentals.user_agent") -> str:
    return expand_env_vars(cfg_get(config, key, DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT)


def fetch_url(url: str, *, ua: str, timeout_sec: float, max_bytes: int | None = None, retries: int = 3) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "application/json,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/html,*/*",
        },
    )
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                if max_bytes is None:
                    return response.read()
                return response.read(max_bytes)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < max(1, retries):
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Fetch failed for {url}: {last_exc}")


def record_raw_response(
    conn: Any,
    *,
    source_id: str,
    endpoint: str,
    body: bytes,
    ingestion_run_id: int,
    query_params: dict[str, Any] | None = None,
    status: int = 200,
    asof: str | None = None,
    binary: bool = False,
) -> None:
    now = utc_now()
    digest = hashlib.sha256(body).hexdigest()
    payload_text = f"base64:{base64.b64encode(body).decode('ascii')}" if binary else body.decode("utf-8", errors="replace")
    linked_ingestion_run_id = None
    if ingestion_run_id:
        row = conn.execute(
            "SELECT source_id FROM ingestion_runs WHERE ingestion_run_id = ?",
            (int(ingestion_run_id),),
        ).fetchone()
        if row is not None and str(row["source_id"] if hasattr(row, "keys") else row[0]) == source_id:
            linked_ingestion_run_id = int(ingestion_run_id)
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            endpoint,
            json.dumps(query_params or {}, sort_keys=True),
            now,
            status,
            digest,
            asof or date.today().isoformat(),
            payload_text,
            linked_ingestion_run_id,
            now,
        ),
    )


def extract_links(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"""href\s*=\s*["']([^"']+)["']""", html, flags=re.IGNORECASE):
        href = match.group(1).strip()
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        links.append(urllib.parse.urljoin(base_url, href))
    return links


def discover_wsts_workbook_url_from_html(html: str, landing_url: str) -> str:
    links = extract_links(html, landing_url)
    candidates = [
        link
        for link in links
        if any(token in link.lower() for token in ("historical-billings-report", "billings", "wsts"))
        and link.lower().endswith((".xlsx", ".xls"))
    ]
    if not candidates:
        raise ValueError(f"No WSTS workbook link found on {landing_url}")
    # Deterministic preference: explicit historical-billings links first, then .xlsx over .xls.
    candidates.sort(key=lambda link: ("historical" not in link.lower(), not link.lower().endswith(".xlsx"), link))
    return candidates[0]


def discover_wsts_workbook_url(landing_url: str, *, ua: str, timeout_sec: float) -> str:
    html = fetch_url(landing_url, ua=ua, timeout_sec=timeout_sec).decode("utf-8", errors="ignore")
    return discover_wsts_workbook_url_from_html(html, landing_url)


def month_header_map(values: tuple[Any, ...]) -> dict[int, int]:
    out: dict[int, int] = {}
    for idx, value in enumerate(values, start=1):
        key = str(value or "").strip().lower()
        if key in MONTH_NAMES:
            out[idx] = MONTH_NAMES[key]
    return out


def parse_wsts_workbook(body: bytes, *, source_url: str, source_file: str) -> list[dict[str, Any]]:
    try:
        import openpyxl  # type: ignore
    except ImportError as exc:
        raise RuntimeError("openpyxl is required to parse WSTS XLSX files.") from exc
    workbook = openpyxl.load_workbook(io.BytesIO(body), data_only=True, read_only=True)
    rows: list[dict[str, Any]] = []
    for sheet in workbook.worksheets:
        title = str(sheet.title or "")
        dataset_type = "3mma" if "3mma" in title.lower() else "monthly"
        header_cols: dict[int, int] = {}
        current_year: int | None = None
        for values in sheet.iter_rows(values_only=True):
            if not header_cols:
                maybe_header = month_header_map(values)
                if len(maybe_header) >= 6:
                    header_cols = maybe_header
                continue
            first = str(values[0] or "").strip() if values else ""
            if re.fullmatch(r"(?:19|20)\d{2}", first):
                current_year = int(first)
                continue
            if first not in WSTS_REGIONS or current_year is None:
                continue
            for col_idx, month_num in header_cols.items():
                if col_idx - 1 >= len(values):
                    continue
                raw_value = safe_float(values[col_idx - 1])
                if raw_value is None or raw_value <= 0:
                    continue
                period_month = date(current_year, month_num, 1).isoformat()
                rows.append(
                    {
                        "dataset_type": dataset_type,
                        "period_month": period_month,
                        "region": first,
                        "value_usd_thousands": raw_value,
                        "value_millions_usd": raw_value / 1000.0,
                        "source_url": source_url,
                        "source_file": source_file,
                        "workbook_sheet": title,
                    }
                )
    if not rows:
        raise ValueError("No WSTS monthly rows parsed from workbook.")
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def add_issue(conn: Any, *, stage: str, source_id: str, issue_type: str, detail: str, severity: str = "warning") -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, source_id, issue_type, issue_detail,
            resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, stage, source_id, issue_type, detail, now, now),
    )


def sync_wsts_billings() -> None:
    configure_utc_logging()
    args = parse_args("Sync WSTS historical billings into technology.sqlite.")
    paths = runtime_paths(args)
    config = paths.config
    source_id = str(cfg_get(config, "semiconductor_sector_overlays.wsts.source_id", "wsts_historical_billings"))
    landing_url = str(cfg_get(config, "semiconductor_sector_overlays.wsts.landing_url"))
    output_csv = resolve_path(cfg_get(config, "semiconductor_sector_overlays.wsts.raw_output_csv"), base_dir=paths.base_dir)
    cache_dir = resolve_path(cfg_get(config, "semiconductor_sector_overlays.cache_dir"), base_dir=paths.base_dir)
    timeout_sec = float(cfg_get(config, "semiconductor_sector_overlays.wsts.timeout_sec", 30.0))
    ua = user_agent(config)
    with connect(paths.db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry(conn, paths)
        run_id = start_run(conn, run_type="sync_wsts_billings", input_path=paths.config_path)
        try:
            if args.manual_xlsx:
                workbook_url = args.manual_xlsx.expanduser().resolve().as_posix()
                body = args.manual_xlsx.expanduser().resolve().read_bytes()
                source_file = args.manual_xlsx.name
                record_raw_response(
                    conn,
                    source_id=source_id,
                    endpoint=workbook_url,
                    body=body,
                    ingestion_run_id=run_id,
                    query_params={"kind": "manual_xlsx"},
                    binary=True,
                )
            else:
                landing_body = fetch_url(landing_url, ua=ua, timeout_sec=timeout_sec)
                record_raw_response(
                    conn,
                    source_id=source_id,
                    endpoint=landing_url,
                    body=landing_body,
                    ingestion_run_id=run_id,
                    query_params={"kind": "landing_page"},
                )
                workbook_url = discover_wsts_workbook_url_from_html(landing_body.decode("utf-8", errors="ignore"), landing_url)
                body = fetch_url(workbook_url, ua=ua, timeout_sec=timeout_sec)
                record_raw_response(
                    conn,
                    source_id=source_id,
                    endpoint=workbook_url,
                    body=body,
                    ingestion_run_id=run_id,
                    query_params={"kind": "workbook"},
                    binary=True,
                )
                source_file = Path(urllib.parse.urlparse(workbook_url).path).name
                cache_dir.mkdir(parents=True, exist_ok=True)
                (cache_dir / source_file).write_bytes(body)
            parsed_rows = parse_wsts_workbook(body, source_url=workbook_url, source_file=source_file)
            now = utc_now()
            with conn:
                conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", ("sync_wsts_billings",))
                for row in parsed_rows:
                    conn.execute(
                        """
                        INSERT INTO fact_semiconductor_wsts_billings(
                            source_id, dataset_type, period_month, region, value_usd_thousands,
                            value_millions_usd, source_url, source_file, workbook_sheet,
                            created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_id, dataset_type, period_month, region) DO UPDATE SET
                            value_usd_thousands = excluded.value_usd_thousands,
                            value_millions_usd = excluded.value_millions_usd,
                            source_url = excluded.source_url,
                            source_file = excluded.source_file,
                            workbook_sheet = excluded.workbook_sheet,
                            updated_at = excluded.updated_at
                        """,
                        (
                            source_id,
                            row["dataset_type"],
                            row["period_month"],
                            row["region"],
                            row["value_usd_thousands"],
                            row["value_millions_usd"],
                            row["source_url"],
                            row["source_file"],
                            row["workbook_sheet"],
                            now,
                            now,
                        ),
                    )
            report_rows = [
                {
                    "source_id": source_id,
                    "source_file": source_file,
                    "source_url": workbook_url,
                    "dataset_types": ",".join(sorted({str(row["dataset_type"]) for row in parsed_rows})),
                    "rows": len(parsed_rows),
                    "min_month": min(str(row["period_month"]) for row in parsed_rows),
                    "max_month": max(str(row["period_month"]) for row in parsed_rows),
                    "regions": ",".join(sorted({str(row["region"]) for row in parsed_rows})),
                }
            ]
            write_csv(output_csv, report_rows, list(report_rows[0].keys()))
            finish_run(conn, run_id=run_id, status="success", row_count=len(parsed_rows), message=f"rows={len(parsed_rows)} file={source_file}")
            LOGGER.info("Synced WSTS rows=%d file=%s output=%s", len(parsed_rows), source_file, output_csv)
        except BaseException as exc:
            with conn:
                add_issue(conn, stage="sync_wsts_billings", source_id=source_id, issue_type="wsts_sync_failed", detail=f"{type(exc).__name__}: {exc}", severity="error")
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


def value_at(series: dict[str, float], period_month: date, lag_months: int = 0) -> float | None:
    month = period_month.month - lag_months
    year = period_month.year
    while month <= 0:
        month += 12
        year -= 1
    return series.get(date(year, month, 1).isoformat())


def load_wsts_series(conn: Any, source_id: str, dataset_type: str, region: str) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT period_month, value_millions_usd
        FROM fact_semiconductor_wsts_billings
        WHERE source_id = ? AND dataset_type = ? AND region = ?
        ORDER BY period_month
        """,
        (source_id, dataset_type, region),
    ).fetchall()
    return {str(row["period_month"]): float(row["value_millions_usd"]) for row in rows if row["value_millions_usd"] is not None}


def fallback_sector_cycle(conn: Any, *, source_id: str, model_family: str, asof: date, reason: str) -> dict[str, Any]:
    prior = conn.execute(
        """
        SELECT *
        FROM feature_semiconductor_sector_cycle
        WHERE source_id = ? AND model_family = ? AND asof_date < ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (source_id, model_family, asof.isoformat()),
    ).fetchone()
    if prior is None:
        return {
            "asof_date": asof.isoformat(),
            "source_id": source_id,
            "model_family": model_family,
            "sector_cycle_score": 50.0,
            "component_quality": 0.0,
            "stale_data": 1,
            "source_status": "missing_required_source",
            "data_quality_status": "review",
            "review_reason": reason,
        }
    row = dict(prior)
    row["asof_date"] = asof.isoformat()
    row["stale_data"] = 1
    row["source_status"] = "fallback_prior_month"
    row["data_quality_status"] = "review"
    row["review_reason"] = reason
    return row


def build_sector_cycle_features() -> None:
    configure_utc_logging()
    args = parse_args("Build WSTS sector-cycle features.")
    paths = runtime_paths(args)
    config = paths.config
    raw_source_id = str(cfg_get(config, "semiconductor_sector_overlays.wsts.source_id", "wsts_historical_billings"))
    feature_source_id = str(cfg_get(config, "semiconductor_sector_overlays.wsts.feature_source_id", "semiconductor_sector_cycle"))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    output_csv = resolve_path(cfg_get(config, "semiconductor_sector_overlays.wsts.feature_output_csv"), base_dir=paths.base_dir)
    max_staleness_days = int(cfg_get(config, "semiconductor_sector_overlays.wsts.max_staleness_days", 60))
    min_history_years = int(cfg_get(config, "semiconductor_sector_overlays.wsts.min_history_years", 10))
    asof = parse_asof(args.asof)
    with connect(paths.db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry(conn, paths)
        run_id = start_run(conn, run_type="build_semiconductor_sector_cycle_features", input_path=paths.config_path)
        try:
            monthly_world = load_wsts_series(conn, raw_source_id, "monthly", "Worldwide")
            mma_world = load_wsts_series(conn, raw_source_id, "3mma", "Worldwide")
            if not monthly_world:
                feature = fallback_sector_cycle(conn, source_id=feature_source_id, model_family=model_family, asof=asof, reason="missing_wsts_monthly_worldwide")
            else:
                latest_month = parse_date(max(monthly_world))
                if latest_month is None:
                    feature = fallback_sector_cycle(conn, source_id=feature_source_id, model_family=model_family, asof=asof, reason="invalid_wsts_latest_month")
                else:
                    latest_value = value_at(monthly_world, latest_month)
                    yoy = pct_change(latest_value, value_at(monthly_world, latest_month, 12))
                    change_3m = pct_change(latest_value, value_at(monthly_world, latest_month, 3))
                    change_6m = pct_change(latest_value, value_at(monthly_world, latest_month, 6))
                    latest_3mma = value_at(mma_world, latest_month)
                    change_3mma = pct_change(latest_3mma, value_at(mma_world, latest_month, 3))
                    region_scores: list[float] = []
                    for region in sorted(WSTS_REGIONS - {"Worldwide"}):
                        series = load_wsts_series(conn, raw_source_id, "monthly", region)
                        region_yoy = pct_change(value_at(series, latest_month), value_at(series, latest_month, 12))
                        if region_yoy is not None:
                            region_scores.append(1.0 if region_yoy > 0 else 0.0)
                    breadth_score = sum(region_scores) / len(region_scores) * 100.0 if region_scores else 50.0
                    yoy_score = pct_to_score(yoy, 250.0)
                    accel_score = pct_to_score(change_3mma if change_3mma is not None else change_3m, 400.0)
                    six_month_score = pct_to_score(change_6m, 250.0)
                    sector_score = clamp(yoy_score * 0.35 + accel_score * 0.25 + six_month_score * 0.15 + breadth_score * 0.25)
                    latest_month_end = date(latest_month.year, latest_month.month, 28) + timedelta(days=4)
                    latest_month_end = latest_month_end - timedelta(days=latest_month_end.day)
                    stale_days = (asof - latest_month_end).days
                    stale = int(stale_days > max_staleness_days)
                    history_months = len(monthly_world)
                    reasons: list[str] = []
                    if history_months < min_history_years * 12:
                        reasons.append(f"low_wsts_history_months={history_months}")
                    if stale:
                        reasons.append(f"wsts_latest_month_stale_days={stale_days}")
                    if yoy is None:
                        reasons.append("missing_yoy")
                    quality = 1.0
                    if reasons:
                        quality = 0.75 if history_months >= min_history_years * 12 and yoy is not None else 0.5
                    feature = {
                        "asof_date": asof.isoformat(),
                        "source_id": feature_source_id,
                        "model_family": model_family,
                        "latest_month": latest_month.isoformat(),
                        "global_sales_millions_usd": latest_value,
                        "global_sales_yoy": yoy,
                        "global_sales_3m_change": change_3m,
                        "global_sales_6m_change": change_6m,
                        "global_3mma_millions_usd": latest_3mma,
                        "global_3mma_3m_change": change_3mma,
                        "regional_breadth_score": breadth_score,
                        "sector_cycle_score": sector_score,
                        "component_quality": quality,
                        "stale_data": stale,
                        "source_status": "success" if not reasons else "review",
                        "data_quality_status": "complete" if not reasons else "review",
                        "review_reason": ";".join(reasons),
                    }
            upsert_sector_cycle_feature(conn, feature)
            write_csv(output_csv, [feature], list(feature.keys()))
            finish_run(conn, run_id=run_id, status="success", row_count=1, message=f"score={feature.get('sector_cycle_score')}")
            LOGGER.info("Built WSTS sector-cycle feature score=%s output=%s", feature.get("sector_cycle_score"), output_csv)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


def upsert_sector_cycle_feature(conn: Any, feature: dict[str, Any]) -> None:
    now = utc_now()
    fields = [
        "asof_date",
        "source_id",
        "model_family",
        "latest_month",
        "global_sales_millions_usd",
        "global_sales_yoy",
        "global_sales_3m_change",
        "global_sales_6m_change",
        "global_3mma_millions_usd",
        "global_3mma_3m_change",
        "regional_breadth_score",
        "sector_cycle_score",
        "component_quality",
        "stale_data",
        "source_status",
        "data_quality_status",
        "review_reason",
    ]
    values = [feature.get(field) for field in fields] + [now, now]
    update_clause = ", ".join(f"{field}=excluded.{field}" for field in fields[3:])
    conn.execute(
        f"""
        INSERT INTO feature_semiconductor_sector_cycle({", ".join(fields)}, created_at, updated_at)
        VALUES ({", ".join("?" for _ in values)})
        ON CONFLICT(asof_date, source_id, model_family) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        values,
    )


def capex_calendar_key(frame: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"CY((?:19|20)\d{2})Q([1-4])", str(frame or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def capex_duration_class(days: int) -> str:
    if 70 <= days <= 120:
        return "qtd"
    if 150 <= days <= 200:
        return "h1"
    if 240 <= days <= 300:
        return "m9"
    if 330 <= days <= 400:
        return "fy"
    return ""


def calendar_period_from_end(end: date) -> str:
    return f"CY{end.year}Q{(end.month - 1) // 3 + 1}"


def quarter_end(year: int, quarter: int) -> date:
    return date(year, quarter * 3, 1).replace(day=28) + timedelta(days=4) - timedelta(days=(date(year, quarter * 3, 1).replace(day=28) + timedelta(days=4)).day)


def duration_days(start: object, end: object) -> int | None:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date is None or end_date is None:
        return None
    return (end_date - start_date).days + 1


def sync_big_tech_capex() -> None:
    configure_utc_logging()
    args = parse_args("Sync SEC big-tech capex proxy facts.")
    paths = runtime_paths(args)
    config = paths.config
    source_id = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.source_id", "sec_big_tech_capex"))
    url_template = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.companyfacts_url_template"))
    output_csv = resolve_path(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.raw_output_csv"), base_dir=paths.base_dir)
    companies = cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.companies", [])
    ua = user_agent(config, "semiconductor_sector_overlays.big_tech_capex.user_agent")
    timeout_sec = float(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.timeout_sec", 30.0))
    max_bytes = int(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.max_bytes", 20_000_000))
    sleep_sec = float(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.request_sleep_sec", 0.12))
    with connect(paths.db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry(conn, paths)
        run_id = start_run(conn, run_type="sync_big_tech_capex", input_path=paths.config_path)
        try:
            report_rows: list[dict[str, Any]] = []
            now = utc_now()
            with conn:
                conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", ("sync_big_tech_capex",))
                for company in companies:
                    ticker = normalize_ticker(company.get("ticker"))
                    cik = str(company.get("cik") or "").zfill(10)
                    if not ticker or not cik:
                        continue
                    url = url_template.format(cik=cik)
                    body = fetch_url(url, ua=ua, timeout_sec=timeout_sec, max_bytes=max_bytes)
                    record_raw_response(
                        conn,
                        source_id=source_id,
                        endpoint=url,
                        body=body,
                        ingestion_run_id=run_id,
                        query_params={"ticker": ticker, "cik": cik},
                    )
                    payload = json.loads(body.decode("utf-8"))
                    facts = payload.get("facts", {}).get("us-gaap", {})
                    inserted = 0
                    for concept, priority in CAPEX_CONCEPT_PRIORITY.items():
                        for unit, fact_rows in facts.get(concept, {}).get("units", {}).items():
                            if unit != "USD" or not isinstance(fact_rows, list):
                                continue
                            for fact in fact_rows:
                                days = duration_days(fact.get("start"), fact.get("end"))
                                if days is None:
                                    continue
                                dur_class = capex_duration_class(days)
                                if not dur_class:
                                    continue
                                value = safe_float(fact.get("val"))
                                if value is None or value <= 0:
                                    continue
                                period_end = parse_date(fact.get("end"))
                                if period_end is None:
                                    continue
                                # Quarter-length facts keep the bare CYyyyyQq key; longer
                                # YTD spans are stored under a suffixed key so the build
                                # step can derive missing quarters by differencing.
                                frame = str(fact.get("frame") or "")
                                base_key = capex_calendar_key(frame)
                                base_period = f"CY{base_key[0]}Q{base_key[1]}" if base_key and dur_class == "qtd" else calendar_period_from_end(period_end)
                                calendar_period = base_period if dur_class == "qtd" else f"{base_period}_{dur_class}"
                                conn.execute(
                                    """
                                    INSERT INTO fact_big_tech_capex(
                                        ticker, calendar_period, source_id, cik, period_start_date,
                                        period_end_date, fiscal_year, fiscal_period, form_type,
                                        filed_date, accession_number, source_concept, frame,
                                        duration_days, capex_usd, created_at, updated_at
                                    )
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(ticker, calendar_period, source_id, accession_number, source_concept)
                                    DO UPDATE SET
                                        period_start_date = excluded.period_start_date,
                                        period_end_date = excluded.period_end_date,
                                        fiscal_year = excluded.fiscal_year,
                                        fiscal_period = excluded.fiscal_period,
                                        form_type = excluded.form_type,
                                        filed_date = excluded.filed_date,
                                        frame = excluded.frame,
                                        duration_days = excluded.duration_days,
                                        capex_usd = excluded.capex_usd,
                                        updated_at = excluded.updated_at
                                    """,
                                    (
                                        ticker,
                                        calendar_period,
                                        source_id,
                                        cik,
                                        str(fact.get("start") or ""),
                                        period_end.isoformat(),
                                        fact.get("fy"),
                                        str(fact.get("fp") or ""),
                                        str(fact.get("form") or ""),
                                        str(fact.get("filed") or ""),
                                        str(fact.get("accn") or ""),
                                        concept,
                                        frame,
                                        days,
                                        value,
                                        now,
                                        now,
                                    ),
                                )
                                inserted += 1
                    report_rows.append({"ticker": ticker, "cik": cik, "rows": inserted, "source_url": url})
                    time.sleep(sleep_sec)
            write_csv(output_csv, report_rows, ["ticker", "cik", "rows", "source_url"])
            finish_run(conn, run_id=run_id, status="success", row_count=sum(int(row["rows"]) for row in report_rows), message=f"tickers={len(report_rows)} output={output_csv}")
            LOGGER.info("Synced big-tech capex facts: %s", output_csv)
        except BaseException as exc:
            with conn:
                add_issue(conn, stage="sync_big_tech_capex", source_id=source_id, issue_type="big_tech_capex_sync_failed", detail=f"{type(exc).__name__}: {exc}", severity="error")
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


def period_shift(calendar_period: str, quarters: int) -> str:
    year, quarter = capex_calendar_key(calendar_period) or (0, 0)
    quarter += quarters
    while quarter <= 0:
        quarter += 4
        year -= 1
    while quarter > 4:
        quarter -= 4
        year += 1
    return f"CY{year}Q{quarter}"


def latest_capex_by_ticker_period(conn: Any, source_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM fact_big_tech_capex
        WHERE source_id = ?
        ORDER BY ticker, calendar_period, source_concept, filed_date DESC
        """,
        (source_id,),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    spans: list[dict[str, Any]] = []

    def keep_better(store: dict[tuple[str, str], dict[str, Any]], key: tuple[str, str], row_dict: dict[str, Any]) -> None:
        current = store.get(key)
        if current is None:
            store[key] = row_dict
            return
        current_priority = CAPEX_CONCEPT_PRIORITY.get(str(current["source_concept"]), 99)
        row_priority = CAPEX_CONCEPT_PRIORITY.get(str(row_dict["source_concept"]), 99)
        if row_priority < current_priority or (
            row_priority == current_priority and str(row_dict["filed_date"] or "") > str(current["filed_date"] or "")
        ):
            store[key] = row_dict

    for row in rows:
        row_dict = dict(row)
        period = str(row["calendar_period"])
        if "_" in period:
            spans.append(row_dict)
            continue
        keep_better(out, (str(row["ticker"]), period), row_dict)
    # Derive missing quarters by differencing YTD spans that share a start date
    # (Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M). Most issuers only report YTD
    # cash flows in 10-Qs, so without this Q2/Q4 coverage is structurally sparse.
    spans_by_chain: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row_dict in spans:
        start = str(row_dict["period_start_date"] or "")
        if not start:
            continue
        spans_by_chain.setdefault((str(row_dict["ticker"]), start), []).append(row_dict)
    # Quarter-length facts also belong to the chains (they anchor H1 - Q1).
    for (ticker, period), row_dict in list(out.items()):
        start = str(row_dict["period_start_date"] or "")
        if start:
            spans_by_chain.setdefault((ticker, start), []).append(row_dict)
    for (ticker, _start), chain in spans_by_chain.items():
        chain.sort(key=lambda item: str(item["period_end_date"]))
        for prev, cur in zip(chain, chain[1:]):
            prev_end = parse_date(prev["period_end_date"])
            cur_end = parse_date(cur["period_end_date"])
            if prev_end is None or cur_end is None:
                continue
            gap = (cur_end - prev_end).days
            if not 70 <= gap <= 120:
                continue
            value = safe_float(cur["capex_usd"])
            prev_value = safe_float(prev["capex_usd"])
            if value is None or prev_value is None:
                continue
            derived = value - prev_value
            if derived <= 0:
                continue
            key = (ticker, calendar_period_from_end(cur_end))
            if key in out:
                continue  # direct quarterly facts win over derived values
            out[key] = {
                **cur,
                "calendar_period": key[1],
                "capex_usd": derived,
                "source_concept": str(cur["source_concept"]),
                "duration_days": gap,
            }
    return out


def fallback_big_tech_capex(conn: Any, *, source_id: str, model_family: str, asof: date, reason: str) -> dict[str, Any]:
    prior = conn.execute(
        """
        SELECT *
        FROM feature_big_tech_capex_cycle
        WHERE source_id = ? AND model_family = ? AND asof_date < ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (source_id, model_family, asof.isoformat()),
    ).fetchone()
    if prior is None:
        return {
            "asof_date": asof.isoformat(),
            "source_id": source_id,
            "model_family": model_family,
            "big_tech_capex_score": 50.0,
            "component_quality": 0.0,
            "stale_data": 1,
            "source_status": "missing_required_source",
            "data_quality_status": "review",
            "review_reason": reason,
        }
    row = dict(prior)
    row["asof_date"] = asof.isoformat()
    row["stale_data"] = 1
    row["source_status"] = "fallback_prior_quarter"
    row["data_quality_status"] = "review"
    row["review_reason"] = reason
    return row


def build_big_tech_capex_features() -> None:
    configure_utc_logging()
    args = parse_args("Build big-tech capex demand proxy features.")
    paths = runtime_paths(args)
    config = paths.config
    raw_source_id = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.source_id", "sec_big_tech_capex"))
    feature_source_id = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.feature_source_id", "semiconductor_big_tech_capex_cycle"))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    output_csv = resolve_path(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.feature_output_csv"), base_dir=paths.base_dir)
    companies = [normalize_ticker(row.get("ticker")) for row in cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.companies", [])]
    companies = [ticker for ticker in companies if ticker]
    min_companies = int(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.min_current_companies", 4))
    max_staleness_days = int(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.max_staleness_days", 150))
    asof = parse_asof(args.asof)
    with connect(paths.db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry(conn, paths)
        run_id = start_run(conn, run_type="build_big_tech_capex_features", input_path=paths.config_path)
        try:
            facts = latest_capex_by_ticker_period(conn, raw_source_id)
            periods = sorted({period for _, period in facts if capex_calendar_key(period)})
            feature: dict[str, Any]
            selected_period = ""
            for period in reversed(periods):
                prior_year = period_shift(period, -4)
                available = [ticker for ticker in companies if (ticker, period) in facts and (ticker, prior_year) in facts]
                if len(available) >= min_companies:
                    selected_period = period
                    break
            if not selected_period:
                feature = fallback_big_tech_capex(conn, source_id=feature_source_id, model_family=model_family, asof=asof, reason="missing_common_big_tech_capex_period")
            else:
                prior_year = period_shift(selected_period, -4)
                prior_quarter = period_shift(selected_period, -1)
                yoy_tickers = [ticker for ticker in companies if (ticker, selected_period) in facts and (ticker, prior_year) in facts]
                qoq_tickers = [ticker for ticker in companies if (ticker, selected_period) in facts and (ticker, prior_quarter) in facts]
                current_sum_yoy = sum(float(facts[(ticker, selected_period)]["capex_usd"]) for ticker in yoy_tickers)
                prior_year_sum = sum(float(facts[(ticker, prior_year)]["capex_usd"]) for ticker in yoy_tickers)
                current_sum_qoq = sum(float(facts[(ticker, selected_period)]["capex_usd"]) for ticker in qoq_tickers)
                prior_quarter_sum = sum(float(facts[(ticker, prior_quarter)]["capex_usd"]) for ticker in qoq_tickers)
                yoy_growth = pct_change(current_sum_yoy, prior_year_sum)
                qoq_growth = pct_change(current_sum_qoq, prior_quarter_sum) if len(qoq_tickers) >= min_companies else None
                breadth_values = []
                for ticker in yoy_tickers:
                    growth = pct_change(float(facts[(ticker, selected_period)]["capex_usd"]), float(facts[(ticker, prior_year)]["capex_usd"]))
                    if growth is not None:
                        breadth_values.append(1.0 if growth > 0 else 0.0)
                breadth_score = sum(breadth_values) / len(breadth_values) * 100.0 if breadth_values else 50.0
                score = clamp(pct_to_score(yoy_growth, 100.0) * 0.45 + pct_to_score(qoq_growth, 150.0) * 0.25 + breadth_score * 0.30)
                current_rows = [facts[(ticker, selected_period)] for ticker in yoy_tickers]
                latest_period_end = max(str(row["period_end_date"]) for row in current_rows)
                latest_filed = max(str(row["filed_date"] or "") for row in current_rows)
                latest_end_date = parse_date(latest_period_end)
                stale_days = (asof - latest_end_date).days if latest_end_date else 9999
                stale = int(stale_days > max_staleness_days)
                reasons: list[str] = []
                if len(yoy_tickers) < len(companies):
                    reasons.append(f"partial_yoy_company_coverage={len(yoy_tickers)}/{len(companies)}")
                if len(qoq_tickers) < min_companies:
                    reasons.append(f"partial_qoq_company_coverage={len(qoq_tickers)}/{len(companies)}")
                if stale:
                    reasons.append(f"big_tech_capex_stale_days={stale_days}")
                quality = min(1.0, len(yoy_tickers) / max(1, len(companies)))
                if stale:
                    quality = min(quality, 0.75)
                feature = {
                    "asof_date": asof.isoformat(),
                    "source_id": feature_source_id,
                    "model_family": model_family,
                    "latest_calendar_period": selected_period,
                    "latest_period_end_date": latest_period_end,
                    "latest_filed_date": latest_filed,
                    "companies_expected": len(companies),
                    "companies_current": len({ticker for ticker in companies if (ticker, selected_period) in facts}),
                    "companies_yoy": len(yoy_tickers),
                    "companies_qoq": len(qoq_tickers),
                    "current_capex_usd": current_sum_yoy,
                    "prior_year_capex_usd": prior_year_sum,
                    "prior_quarter_capex_usd": prior_quarter_sum if len(qoq_tickers) >= min_companies else None,
                    "capex_yoy_growth": yoy_growth,
                    "capex_qoq_growth": qoq_growth,
                    "capex_breadth_score": breadth_score,
                    "big_tech_capex_score": score,
                    "component_quality": quality,
                    "stale_data": stale,
                    "source_status": "success" if not reasons else "review",
                    "data_quality_status": "complete" if not reasons else "review",
                    "review_reason": ";".join(reasons),
                }
            upsert_big_tech_capex_feature(conn, feature)
            write_csv(output_csv, [feature], list(feature.keys()))
            finish_run(conn, run_id=run_id, status="success", row_count=1, message=f"score={feature.get('big_tech_capex_score')}")
            LOGGER.info("Built big-tech capex feature score=%s output=%s", feature.get("big_tech_capex_score"), output_csv)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


def upsert_big_tech_capex_feature(conn: Any, feature: dict[str, Any]) -> None:
    now = utc_now()
    fields = [
        "asof_date",
        "source_id",
        "model_family",
        "latest_calendar_period",
        "latest_period_end_date",
        "latest_filed_date",
        "companies_expected",
        "companies_current",
        "companies_yoy",
        "companies_qoq",
        "current_capex_usd",
        "prior_year_capex_usd",
        "prior_quarter_capex_usd",
        "capex_yoy_growth",
        "capex_qoq_growth",
        "capex_breadth_score",
        "big_tech_capex_score",
        "component_quality",
        "stale_data",
        "source_status",
        "data_quality_status",
        "review_reason",
    ]
    values = [feature.get(field) for field in fields] + [now, now]
    update_clause = ", ".join(f"{field}=excluded.{field}" for field in fields[3:])
    conn.execute(
        f"""
        INSERT INTO feature_big_tech_capex_cycle({", ".join(fields)}, created_at, updated_at)
        VALUES ({", ".join("?" for _ in values)})
        ON CONFLICT(asof_date, source_id, model_family) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        values,
    )


def latest_feature(conn: Any, table: str, source_id: str, model_family: str, asof: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE source_id = ? AND model_family = ? AND asof_date <= ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (source_id, model_family, asof),
    ).fetchone()
    return dict(row) if row is not None else None


def apply_semiconductor_overlay_scores() -> None:
    configure_utc_logging()
    args = parse_args("Apply Stage 6B overlay scores to the Stage 6A scoring contract.")
    paths = runtime_paths(args)
    config = paths.config
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    scoring_source_id = str(cfg_get(config, "semiconductor_sector_overlays.overlay_application.scoring_source_id", "semiconductor_scoring_contract"))
    sector_source_id = str(cfg_get(config, "semiconductor_sector_overlays.wsts.feature_source_id", "semiconductor_sector_cycle"))
    capex_source_id = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.feature_source_id", "semiconductor_big_tech_capex_cycle"))
    output_csv = resolve_path(cfg_get(config, "semiconductor_sector_overlays.overlay_application.output_csv"), base_dir=paths.base_dir)
    with connect(paths.db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry(conn, paths)
        run_id = start_run(conn, run_type="apply_semiconductor_overlay_scores", input_path=paths.config_path)
        try:
            asof_text = args.asof or conn.execute(
                "SELECT MAX(asof_date) AS asof_date FROM feature_scoring_input WHERE source_id = ? AND model_family = ?",
                (scoring_source_id, model_family),
            ).fetchone()["asof_date"]
            if not asof_text:
                raise ValueError("No Stage 6A scoring input rows found. Run Stage 6A first.")
            sector = latest_feature(conn, "feature_semiconductor_sector_cycle", sector_source_id, model_family, asof_text)
            capex = latest_feature(conn, "feature_big_tech_capex_cycle", capex_source_id, model_family, asof_text)
            if sector is None or capex is None:
                raise ValueError("Stage 6B feature rows are missing. Build WSTS and big-tech capex features first.")
            tickers = [
                normalize_ticker(row["ticker"])
                for row in conn.execute(
                    """
                    SELECT ticker
                    FROM feature_scoring_input
                    WHERE source_id = ? AND model_family = ? AND asof_date = ?
                    ORDER BY ticker
                    """,
                    (scoring_source_id, model_family, asof_text),
                ).fetchall()
                if normalize_ticker(row["ticker"])
            ]
            now = utc_now()
            sector_score = float(sector["sector_cycle_score"] or 50.0)
            capex_score = float(capex["big_tech_capex_score"] or 50.0)
            sector_quality = float(sector["component_quality"] or 0.0)
            capex_quality = float(capex["component_quality"] or 0.0)
            overlay_score = clamp(sector_score * 0.60 + capex_score * 0.40)
            overlay_quality = max(0.0, min(1.0, sector_quality * 0.60 + capex_quality * 0.40))
            overlay_status = "complete" if sector["data_quality_status"] == "complete" and capex["data_quality_status"] == "complete" else "review"
            with conn:
                for ticker in tickers:
                    cohort = conn.execute(
                        "SELECT calibration_cohort_id FROM feature_scoring_input WHERE ticker = ? AND source_id = ? AND model_family = ? AND asof_date = ?",
                        (ticker, scoring_source_id, model_family, asof_text),
                    ).fetchone()
                    for component_name, component_group, component_score, component_quality, feature in (
                        ("sector_cycle", "sector_overlay", sector_score, sector_quality, sector),
                        ("big_tech_capex", "sector_overlay", capex_score, capex_quality, capex),
                    ):
                        conn.execute(
                            """
                            INSERT INTO feature_scoring_component(
                                ticker, asof_date, source_id, model_family, component_name,
                                component_group, calibration_cohort_id, component_score,
                                universe_percentile, cohort_percentile, component_quality,
                                component_status, available_subfeature_count,
                                missing_subfeature_count, default_applied, review_reason,
                                created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 50.0, 50.0, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(ticker, asof_date, source_id, model_family, component_name)
                            DO UPDATE SET
                                component_score = excluded.component_score,
                                universe_percentile = excluded.universe_percentile,
                                cohort_percentile = excluded.cohort_percentile,
                                component_quality = excluded.component_quality,
                                component_status = excluded.component_status,
                                available_subfeature_count = excluded.available_subfeature_count,
                                missing_subfeature_count = excluded.missing_subfeature_count,
                                default_applied = excluded.default_applied,
                                review_reason = excluded.review_reason,
                                updated_at = excluded.updated_at
                            """,
                            (
                                ticker,
                                asof_text,
                                scoring_source_id,
                                model_family,
                                component_name,
                                component_group,
                                cohort["calibration_cohort_id"] if cohort is not None else "",
                                component_score,
                                component_quality,
                                "complete" if str(feature["data_quality_status"]) == "complete" else str(feature["source_status"] or "review"),
                                1 if component_quality > 0 else 0,
                                0 if component_quality > 0 else 1,
                                0 if component_quality > 0 else 1,
                                str(feature["review_reason"] or ""),
                                now,
                                now,
                            ),
                        )
                    existing = conn.execute(
                        """
                        SELECT core_data_quality_confidence
                        FROM feature_scoring_input
                        WHERE ticker = ? AND source_id = ? AND model_family = ? AND asof_date = ?
                        """,
                        (ticker, scoring_source_id, model_family, asof_text),
                    ).fetchone()
                    core_confidence = float(existing["core_data_quality_confidence"] or 0.0) if existing is not None else 0.0
                    full_confidence = max(0.0, min(1.0, core_confidence * 0.75 + overlay_quality * 0.25))
                    conn.execute(
                        """
                        UPDATE feature_scoring_input
                        SET sector_cycle_score = ?,
                            big_tech_capex_score = ?,
                            sector_overlay_score = ?,
                            sector_overlay_quality = ?,
                            sector_overlay_status = ?,
                            full_data_quality_confidence = ?,
                            updated_at = ?
                        WHERE ticker = ?
                          AND source_id = ?
                          AND model_family = ?
                          AND asof_date = ?
                        """,
                        (
                            sector_score,
                            capex_score,
                            overlay_score,
                            overlay_quality,
                            overlay_status,
                            full_confidence,
                            now,
                            ticker,
                            scoring_source_id,
                            model_family,
                            asof_text,
                        ),
                    )
            report_rows = [
                {
                    "asof_date": asof_text,
                    "tickers": len(tickers),
                    "sector_cycle_score": sector_score,
                    "sector_cycle_quality": sector_quality,
                    "big_tech_capex_score": capex_score,
                    "big_tech_capex_quality": capex_quality,
                    "sector_overlay_score": overlay_score,
                    "sector_overlay_quality": overlay_quality,
                    "sector_overlay_status": overlay_status,
                }
            ]
            write_csv(output_csv, report_rows, list(report_rows[0].keys()))
            finish_run(conn, run_id=run_id, status="success", row_count=len(tickers), message=f"asof={asof_text} tickers={len(tickers)}")
            LOGGER.info("Applied Stage 6B overlays to %d tickers for asof=%s", len(tickers), asof_text)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


def validate_semiconductor_overlays() -> int:
    configure_utc_logging()
    args = parse_args("Validate Stage 6B semiconductor overlays.")
    paths = runtime_paths(args)
    config = paths.config
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    wsts_source = str(cfg_get(config, "semiconductor_sector_overlays.wsts.source_id", "wsts_historical_billings"))
    sector_source = str(cfg_get(config, "semiconductor_sector_overlays.wsts.feature_source_id", "semiconductor_sector_cycle"))
    capex_source = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.source_id", "sec_big_tech_capex"))
    capex_feature_source = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.feature_source_id", "semiconductor_big_tech_capex_cycle"))
    scoring_source = str(cfg_get(config, "semiconductor_sector_overlays.overlay_application.scoring_source_id", "semiconductor_scoring_contract"))
    asof = args.asof
    errors: list[str] = []
    warnings: list[str] = []
    with connect(paths.db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry(conn, paths)
        universe = [
            normalize_ticker(row["ticker"])
            for row in conn.execute(
                """
                SELECT c.ticker
                FROM dim_company c
                JOIN dim_technology_taxonomy t ON t.ticker = c.ticker AND t.model_family = ?
                WHERE c.is_active = 1
                ORDER BY c.ticker
                """,
                (model_family,),
            ).fetchall()
            if normalize_ticker(row["ticker"])
        ]
        asof = asof or conn.execute(
            "SELECT MAX(asof_date) FROM feature_scoring_input WHERE source_id = ? AND model_family = ?",
            (scoring_source, model_family),
        ).fetchone()[0]
        if not asof:
            errors.append("No scoring input rows found.")
            asof = date.today().isoformat()
        for source_id in (wsts_source, sector_source, capex_source, capex_feature_source, scoring_source):
            status = conn.execute("SELECT status FROM source_registry WHERE source_id = ?", (source_id,)).fetchone()
            if status is None or status["status"] != "active":
                errors.append(f"Source {source_id} is not active.")
        wsts_rows = conn.execute(
            "SELECT COUNT(*) AS n, MIN(period_month) AS min_month, MAX(period_month) AS max_month FROM fact_semiconductor_wsts_billings WHERE source_id = ? AND dataset_type = 'monthly'",
            (wsts_source,),
        ).fetchone()
        if int(wsts_rows["n"] or 0) < 10 * 12 * 5:
            errors.append(f"WSTS monthly rows are too low: {int(wsts_rows['n'] or 0)}")
        sector = latest_feature(conn, "feature_semiconductor_sector_cycle", sector_source, model_family, asof)
        if sector is None:
            errors.append("Missing semiconductor sector-cycle feature row.")
        elif float(sector.get("component_quality") or 0.0) <= 0:
            errors.append(f"Sector-cycle feature has zero quality: {sector}")
        capex_tickers = conn.execute(
            "SELECT COUNT(DISTINCT ticker) AS n FROM fact_big_tech_capex WHERE source_id = ?",
            (capex_source,),
        ).fetchone()["n"]
        if int(capex_tickers or 0) < 5:
            errors.append(f"Big-tech capex facts cover only {capex_tickers}/5 tickers.")
        capex = latest_feature(conn, "feature_big_tech_capex_cycle", capex_feature_source, model_family, asof)
        if capex is None:
            errors.append("Missing big-tech capex feature row.")
        elif int(capex.get("companies_yoy") or 0) < int(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.min_current_companies", 4)):
            errors.append(f"Big-tech capex feature has insufficient company coverage: {capex.get('companies_yoy')}")
        ph = qmarks(universe)
        scoring_rows = conn.execute(
            f"""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN sector_overlay_status <> 'not_loaded' THEN 1 ELSE 0 END) AS loaded,
                   SUM(CASE WHEN sector_overlay_quality > 0 THEN 1 ELSE 0 END) AS quality_rows
            FROM feature_scoring_input
            WHERE source_id = ? AND model_family = ? AND asof_date = ? AND ticker IN ({ph})
            """,
            (scoring_source, model_family, asof, *universe),
        ).fetchone()
        if int(scoring_rows["n"] or 0) != len(universe):
            errors.append(f"Scoring input row count mismatch: {scoring_rows['n']}/{len(universe)}")
        if int(scoring_rows["loaded"] or 0) != len(universe):
            errors.append(f"Scoring overlay status not loaded for all tickers: {scoring_rows['loaded']}/{len(universe)}")
        component_rows = conn.execute(
            f"""
            SELECT component_name, COUNT(*) AS n,
                   SUM(CASE WHEN component_status <> 'not_loaded' THEN 1 ELSE 0 END) AS loaded
            FROM feature_scoring_component
            WHERE source_id = ? AND model_family = ? AND asof_date = ?
              AND ticker IN ({ph})
              AND component_name IN ('sector_cycle', 'big_tech_capex')
            GROUP BY component_name
            """,
            (scoring_source, model_family, asof, *universe),
        ).fetchall()
        component_by_name = {str(row["component_name"]): row for row in component_rows}
        for component_name in ("sector_cycle", "big_tech_capex"):
            row = component_by_name.get(component_name)
            if row is None or int(row["n"] or 0) != len(universe) or int(row["loaded"] or 0) != len(universe):
                errors.append(f"{component_name} component coverage invalid: {dict(row) if row is not None else None}")
        warnings.append(f"Universe tickers={len(universe)} scoring_asof={asof}")
        warnings.append(f"WSTS rows={wsts_rows['n']} min={wsts_rows['min_month']} max={wsts_rows['max_month']}")
        warnings.append(f"Sector cycle score={sector.get('sector_cycle_score') if sector else ''} quality={sector.get('component_quality') if sector else ''} status={sector.get('data_quality_status') if sector else ''}")
        warnings.append(f"Big-tech capex tickers={capex_tickers} score={capex.get('big_tech_capex_score') if capex else ''} quality={capex.get('component_quality') if capex else ''} companies_yoy={capex.get('companies_yoy') if capex else ''}")
        warnings.append(f"Scoring overlay rows={scoring_rows['n']} loaded={scoring_rows['loaded']} quality_rows={scoring_rows['quality_rows']}")
    for message in warnings:
        LOGGER.info(message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        return 1
    LOGGER.info("Stage 6B semiconductor overlay validation passed.")
    return 0
