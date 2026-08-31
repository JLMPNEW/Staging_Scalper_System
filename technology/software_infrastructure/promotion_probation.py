"""Forward 21-session probation for the software Stage 8 v2 promotion."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from technology.core.config import cfg_get, load_yaml, resolve_path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "software_infrastructure_promotion_probation"
PRICE_SOURCE_PRIORITY = ("yahoo_finance_adjusted", "norgate_us_equities_total_return")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f"{path.suffix}.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    temp = path.with_suffix(f"{path.suffix}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        if fields:
            writer.writeheader()
            writer.writerows(rows)
    temp.replace(path)


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def readonly_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def eligible_scores(
    conn: sqlite3.Connection, *, source_id: str, model_version: str, asof: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, final_score, final_rank
        FROM feature_scoring_model_output
        WHERE source_id = ? AND model_family = 'software_infrastructure'
          AND model_version = ? AND asof_date = ?
          AND rank_ready_flag = 1 AND calibration_eligible_flag = 1
          AND model_status = 'complete'
        ORDER BY final_score DESC, ticker
        """,
        (source_id, model_version, asof),
    ).fetchall()
    return [dict(row) for row in rows]


def select_holdings(
    production: list[dict[str, Any]], rollback: list[dict[str, Any]], *, quantile: float, minimum: int
) -> list[dict[str, Any]]:
    common = {str(row["ticker"]) for row in production} & {str(row["ticker"]) for row in rollback}
    if len(common) < minimum:
        return []
    count = min(len(common), max(minimum, int(math.ceil(len(common) * quantile))))
    rows: list[dict[str, Any]] = []
    for role, source_rows in (("promoted", production), ("rollback", rollback)):
        selected = [row for row in source_rows if str(row["ticker"]) in common][:count]
        for position, row in enumerate(selected, start=1):
            rows.append(
                {
                    "model_role": role,
                    "ticker": str(row["ticker"]),
                    "selection_rank": position,
                    "model_rank": row.get("final_rank"),
                    "selection_score": row.get("final_score"),
                    "initial_weight": 1.0 / count,
                }
            )
    return rows


def load_prices(
    conn: sqlite3.Connection, tickers: Iterable[str], *, start: str, end: str
) -> dict[str, dict[str, float]]:
    ticker_list = sorted(set(tickers))
    if not ticker_list:
        return {}
    placeholders = ",".join("?" for _ in ticker_list)
    source_ph = ",".join("?" for _ in PRICE_SOURCE_PRIORITY)
    rows = conn.execute(
        f"""
        SELECT ticker, bar_date, source_id, adj_close, close
        FROM fact_price_ohlcv
        WHERE ticker IN ({placeholders}) AND source_id IN ({source_ph})
          AND bar_date >= ? AND bar_date <= ?
        ORDER BY ticker, source_id, bar_date
        """,
        (*ticker_list, *PRICE_SOURCE_PRIORITY, start, end),
    ).fetchall()
    grouped: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        price = row["adj_close"] if row["adj_close"] is not None else row["close"]
        if price is None or float(price) <= 0:
            continue
        grouped.setdefault(str(row["ticker"]), {}).setdefault(str(row["source_id"]), {})[
            str(row["bar_date"])
        ] = float(price)
    selected: dict[str, dict[str, float]] = {}
    priority = {source: index for index, source in enumerate(PRICE_SOURCE_PRIORITY)}
    for ticker, by_source in grouped.items():
        source, values = max(
            by_source.items(),
            key=lambda item: (max(item[1]), -priority.get(item[0], 999), len(item[1])),
        )
        _ = source
        selected[ticker] = values
    return selected


def max_drawdown(values: list[float]) -> float:
    peak = 0.0
    result = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            result = min(result, value / peak - 1.0)
    return result


def evaluate_holdings(
    holdings: list[dict[str, Any]],
    prices: dict[str, dict[str, float]],
    sessions: list[str],
    *,
    cost_per_side: float,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    if not sessions:
        return [], {}
    entry = sessions[0]
    performance_rows: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, float]] = {}
    for role in ("promoted", "rollback"):
        tickers = [str(row["ticker"]) for row in holdings if row["model_role"] == role]
        valid = [ticker for ticker in tickers if entry in prices.get(ticker, {})]
        path: list[float] = []
        role_rows: list[dict[str, Any]] = []
        for index, session in enumerate(sessions):
            available = [ticker for ticker in valid if session in prices.get(ticker, {})]
            wealth = (
                sum(prices[ticker][session] / prices[ticker][entry] for ticker in available) / len(available)
                if available
                else 1.0
            )
            net_return = wealth - 1.0 - (2.0 * cost_per_side)
            path.append(wealth)
            role_rows.append(
                {
                    "model_role": role,
                    "entry_price_date": entry,
                    "session_date": session,
                    "completed_return_sessions": index,
                    "gross_return": wealth - 1.0,
                    "net_return": net_return,
                    "price_coverage": len(available) / len(tickers) if tickers else 0.0,
                    "covered_positions": len(available),
                    "selected_positions": len(tickers),
                }
            )
        performance_rows.extend(role_rows)
        final = role_rows[-1]
        summaries[role] = {
            "gross_return": float(final["gross_return"]),
            "net_return": float(final["net_return"]),
            "price_coverage": float(final["price_coverage"]),
            "max_drawdown": max_drawdown(path),
        }
    return performance_rows, summaries


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    cfg = cfg_get(config, CONFIG_KEY, {}) or {}
    if not isinstance(cfg, dict) or not bool(cfg.get("enabled")):
        raise RuntimeError("Software promotion probation is not enabled.")
    if bool(cfg.get("automatic_reversion")):
        raise RuntimeError("Probation is advisory and may not automatically rewrite production.")
    asof = str(args.asof or "")[:10]
    effective = str(cfg.get("effective_date") or "")[:10]
    asof_date = datetime.strptime(asof, "%Y-%m-%d").date()
    effective_date = datetime.strptime(effective, "%Y-%m-%d").date()
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=base_dir
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg.get("output_dir"), base_dir=base_dir
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    holdings_path = output_dir / "software_infrastructure_promotion_probation_holdings.csv"
    performance_path = output_dir / "software_infrastructure_promotion_probation_performance.csv"
    status_path = output_dir / "software_infrastructure_promotion_probation_status.json"
    manifest_path = output_dir / "software_infrastructure_promotion_probation_manifest.json"
    receipt_path = resolve_path(
        cfg_get(config, "software_infrastructure_governance_reports.active_promotion_receipt_path"),
        base_dir=base_dir,
    )
    status: dict[str, Any] = {
        "schema_version": "software_infrastructure_promotion_probation_status_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asof_date": asof,
        "effective_date": effective,
        "required_trading_sessions": int(cfg.get("required_trading_sessions") or 21),
        "production_model_version": cfg.get("production_model_version"),
        "rollback_model_version": cfg.get("rollback_model_version"),
        "automatic_reversion": False,
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
    }
    if asof_date < effective_date:
        status.update({"status": "scheduled", "decision": "not_started", "completed_return_sessions": 0})
        atomic_json(status_path, status)
        atomic_json(manifest_path, {**status, "outputs": {"status": str(status_path)}})
        return 0
    holdings_seal_path = output_dir / "software_infrastructure_promotion_probation_holdings_seal.json"
    with readonly_connect(db_path) as conn:
        holdings = read_csv(holdings_path)
        if holdings:
            seal = json.loads(holdings_seal_path.read_text(encoding="utf-8")) if holdings_seal_path.exists() else {}
            if seal.get("holdings_sha256") != sha256_file(holdings_path):
                raise RuntimeError("Frozen probation holdings hash mismatch.")
        else:
            production = eligible_scores(
                conn,
                source_id=str(cfg.get("production_source_id") or ""),
                model_version=str(cfg.get("production_model_version") or ""),
                asof=asof,
            )
            rollback = eligible_scores(
                conn,
                source_id=str(cfg.get("rollback_source_id") or ""),
                model_version=str(cfg.get("rollback_model_version") or ""),
                asof=asof,
            )
            holdings = select_holdings(
                production,
                rollback,
                quantile=float(cfg.get("portfolio_quantile") or 0.20),
                minimum=int(cfg.get("min_positions") or 5),
            )
            if not holdings:
                status.update({
                    "status": "awaiting_scores",
                    "decision": "not_started",
                    "completed_return_sessions": 0,
                    "production_eligible_rows": len(production),
                    "rollback_eligible_rows": len(rollback),
                })
                atomic_json(status_path, status)
                atomic_json(manifest_path, {**status, "outputs": {"status": str(status_path)}})
                return 0
            for row in holdings:
                row["selection_asof_date"] = asof
            write_csv(holdings_path, holdings)
            atomic_json(
                holdings_seal_path,
                {
                    "schema_version": "software_infrastructure_probation_holdings_seal_v1",
                    "selection_asof_date": asof,
                    "holdings_sha256": sha256_file(holdings_path),
                    "receipt_sha256": sha256_file(receipt_path),
                },
            )
        selection_asof = str(holdings[0].get("selection_asof_date") or "")
        benchmark = str(cfg.get("benchmark_ticker") or "QQQ")
        tickers = {str(row["ticker"]) for row in holdings} | {benchmark}
        prices = load_prices(conn, tickers, start=selection_asof, end=asof)
        required = int(cfg.get("required_trading_sessions") or 21)
        sessions = sorted(day for day in prices.get(benchmark, {}) if day > selection_asof)[: required + 1]
        if not sessions:
            status.update({
                "status": "awaiting_entry_price",
                "decision": "not_started",
                "selection_asof_date": selection_asof,
                "completed_return_sessions": 0,
                "holdings_sha256": sha256_file(holdings_path),
            })
            atomic_json(status_path, status)
            atomic_json(manifest_path, {**status, "outputs": {"status": str(status_path)}})
            return 0
        performance_rows, summaries = evaluate_holdings(
            holdings,
            prices,
            sessions,
            cost_per_side=float(cfg.get("transaction_cost_bps_per_side") or 0.0) / 10000.0,
        )
        write_csv(performance_path, performance_rows)

    completed = max(0, len(sessions) - 1)
    promoted = summaries.get("promoted") or {}
    rollback = summaries.get("rollback") or {}
    min_coverage = float(cfg.get("minimum_price_coverage") or 0.90)
    coverage_ok = min(
        float(promoted.get("price_coverage") or 0.0),
        float(rollback.get("price_coverage") or 0.0),
    ) >= min_coverage
    active_return = float(promoted.get("net_return") or 0.0) - float(rollback.get("net_return") or 0.0)
    if completed < required:
        run_status, decision = "monitoring", "pending"
    elif not coverage_ok:
        run_status, decision = "data_quality_hold", "manual_review_required"
    elif active_return >= 0.0:
        run_status, decision = "complete", "keep_promoted_model"
    else:
        run_status, decision = "complete", "revert_to_v1_recommended"
    status.update(
        {
            "status": run_status,
            "decision": decision,
            "decision_rule": cfg.get("decision_rule"),
            "selection_asof_date": selection_asof,
            "entry_price_date": sessions[0],
            "last_evaluated_session": sessions[-1],
            "completed_return_sessions": completed,
            "price_coverage_pass": int(coverage_ok),
            "promoted": promoted,
            "rollback": rollback,
            "promoted_minus_rollback_net_return": active_return,
            "holdings_sha256": sha256_file(holdings_path),
            "performance_sha256": sha256_file(performance_path),
        }
    )
    atomic_json(status_path, status)
    manifest = {
        **status,
        "database_path": str(db_path),
        "config_path": str(config_path),
        "outputs": {
            "holdings": str(holdings_path),
            "holdings_seal": str(holdings_seal_path),
            "performance": str(performance_path),
            "status": str(status_path),
        },
    }
    atomic_json(manifest_path, manifest)
    print(json.dumps(status, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
