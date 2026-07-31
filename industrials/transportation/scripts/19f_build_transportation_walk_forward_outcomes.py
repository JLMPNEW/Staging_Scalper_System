#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.transportation.oos_outcomes import (  # noqa: E402
    ACTIVE_PRICE_SOURCE,
    DEFAULT_FORWARD_TRADING_DAYS,
    AliasPolicy,
    ContinuityPolicy,
    MembershipEvent,
    OUTCOME_PANEL_VERSION,
    PricePoint,
    finite_float,
    fmt,
    optional_date,
    outcome_window,
    parse_date,
    price_source_order,
    resolve_price_ticker,
    write_gzip_csv_atomic,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    iter_gzip_csv,
    read_csv,
    read_json,
    read_only_connection,
    sha256,
    verify_artifact,
    write_manifest,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


PANEL_FIELDS = (
    "asof_date",
    "ticker",
    "model_family",
    "calibration_cohort",
    "industry",
    "universe_role",
    "primary_archetype",
    "metric_id",
    "metric_value",
    "direction",
    "direction_multiplier",
    "direction_adjusted_metric_value",
    "unit",
    "period_start",
    "period_end",
    "availability_date",
    "availability_status",
    "source_id",
    "source_record_id",
    "confidence",
    "split_name",
    "price_ticker",
    "alias_resolution",
    "price_source_id",
    "price_basis",
    "price_adjustment",
    "price_asof_date",
    "price_asof_value",
    "price_forward_date",
    "price_forward_value",
    "forward_trading_days",
    "security_forward_session_count",
    "security_forward_return",
    "outcome_method",
    "terminal_type",
    "membership_end_date",
    "continuity_policy",
    "history_treatment",
    "current_security_start_date",
    "structural_break_date",
    "IYT_price_source_id",
    "IYT_price_asof_date",
    "IYT_price_asof_value",
    "IYT_price_forward_date",
    "IYT_price_forward_value",
    "IYT_forward_return",
    "forward_excess_return_vs_IYT",
    "XTN_price_source_id",
    "XTN_price_asof_date",
    "XTN_price_asof_value",
    "XTN_price_forward_date",
    "XTN_price_forward_value",
    "XTN_forward_return",
    "forward_excess_return_vs_XTN",
    "SPY_price_source_id",
    "SPY_price_asof_date",
    "SPY_price_asof_value",
    "SPY_price_forward_date",
    "SPY_price_forward_value",
    "SPY_forward_return",
    "forward_excess_return_vs_SPY",
    "security_return_available_flag",
    "all_benchmark_returns_available_flag",
    "return_available_flag",
    "return_unavailable_reason",
    "metric_value_available_flag",
    "panel_row_eligible_flag",
    "panel_row_eligible_reason",
)
BENCHMARKS = ("IYT", "XTN", "SPY")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the read-only, survivorship-corrected transportation "
            "63-session outcome panel from the frozen v3 feature panel."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(
            "output/industrials/transportation/historical_features/"
            "v3_conflict_resolved"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def _artifact(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {
        "path": str(path.resolve()),
        "sha256": sha256(path),
    }
    if rows is not None:
        output["row_count"] = rows
    return output


def _load_directions(config_path: Path) -> dict[str, str]:
    registry = (
        config_path.parent
        / "transportation"
        / "data"
        / "transportation_specialized_metric_discovery_registry.csv"
    ).resolve()
    rows = read_csv(registry)
    return {
        row["metric_id"]: row["scoring_posture"]
        for row in rows
        if row.get("metric_id")
    }


def _load_aliases(
    connection: sqlite3.Connection,
) -> dict[str, list[AliasPolicy]]:
    output: dict[str, list[AliasPolicy]] = {}
    rows = connection.execute(
        """
        SELECT contract_ticker, active_ticker, predecessor_ticker,
               effective_date
        FROM dim_ticker_alias
        WHERE verified_flag=1
          AND source_id='transportation_ticker_alias_seed'
        ORDER BY contract_ticker, effective_date
        """
    )
    for row in rows:
        policy = AliasPolicy(
            contract_ticker=str(row["contract_ticker"]).upper(),
            active_ticker=str(row["active_ticker"]).upper(),
            predecessor_ticker=str(
                row["predecessor_ticker"] or ""
            ).upper(),
            effective_date=parse_date(
                row["effective_date"],
                field="alias.effective_date",
            ),
        )
        output.setdefault(policy.contract_ticker, []).append(policy)
    return output


def _load_memberships(
    connection: sqlite3.Connection,
    *,
    source_id: str,
) -> dict[str, MembershipEvent]:
    rows = connection.execute(
        """
        SELECT m.ticker, m.start_date, m.end_date, m.membership_status,
               COALESCE(s.terminal_type, '') AS terminal_type,
               COALESCE(s.exit_type, '') AS exit_type
        FROM dim_universe_membership AS m
        LEFT JOIN dim_delisted_calibration_seed AS s
          ON s.model_family=m.model_family
         AND s.internal_ticker=m.ticker
        WHERE m.model_family=?
          AND m.membership_source_id=?
          AND m.point_in_time_flag=1
        ORDER BY m.ticker
        """,
        (MODEL_FAMILY, source_id),
    )
    return {
        str(row["ticker"]).upper(): MembershipEvent(
            ticker=str(row["ticker"]).upper(),
            start_date=parse_date(
                row["start_date"],
                field="membership.start_date",
            ),
            end_date=optional_date(
                row["end_date"],
                field="membership.end_date",
            ),
            membership_status=str(row["membership_status"] or ""),
            terminal_type=str(row["terminal_type"] or "").lower(),
            exit_type=str(row["exit_type"] or "").lower(),
        )
        for row in rows
    }


def _load_continuity(
    connection: sqlite3.Connection,
) -> dict[str, ContinuityPolicy]:
    rows = connection.execute(
        """
        SELECT ticker, current_security_start_date, continuity_policy,
               structural_break_date, history_treatment
        FROM dim_security_continuity_policy
        WHERE model_family=?
        ORDER BY ticker
        """,
        (MODEL_FAMILY,),
    )
    return {
        str(row["ticker"]).upper(): ContinuityPolicy(
            ticker=str(row["ticker"]).upper(),
            current_security_start_date=parse_date(
                row["current_security_start_date"],
                field="continuity.current_security_start_date",
            ),
            continuity_policy=str(row["continuity_policy"] or ""),
            structural_break_date=optional_date(
                row["structural_break_date"],
                field="continuity.structural_break_date",
            ),
            history_treatment=str(row["history_treatment"] or ""),
        )
        for row in rows
    }


def _load_prices(
    connection: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    sources: Sequence[str],
) -> dict[str, dict[str, list[PricePoint]]]:
    clean_tickers = sorted({ticker.upper() for ticker in tickers if ticker})
    clean_sources = list(dict.fromkeys(source for source in sources if source))
    ticker_slots = ",".join("?" for _ in clean_tickers)
    source_slots = ",".join("?" for _ in clean_sources)
    rows = connection.execute(
        f"""
        SELECT ticker, source_id, bar_date, adj_close, close,
               price_adjustment
        FROM fact_price_ohlcv
        WHERE UPPER(ticker) IN ({ticker_slots})
          AND source_id IN ({source_slots})
          AND is_adjusted=1
          AND adj_close IS NOT NULL
          AND adj_close >= 0
        ORDER BY ticker, source_id, bar_date
        """,
        (*clean_tickers, *clean_sources),
    )
    output: dict[str, dict[str, list[PricePoint]]] = {}
    for row in rows:
        value = finite_float(row["adj_close"])
        if value is None or value < 0:
            continue
        ticker = str(row["ticker"]).upper()
        source = str(row["source_id"])
        output.setdefault(ticker, {}).setdefault(source, []).append(
            PricePoint(
                bar_date=parse_date(row["bar_date"], field="bar_date"),
                value=value,
                source_id=source,
                price_basis="adj_close",
                price_adjustment=str(row["price_adjustment"] or ""),
            )
        )
    return output


def _benchmark_fields(
    ticker: str,
    window: Any,
    forward_return: float | None,
    security_return: float | None,
) -> dict[str, str]:
    prefix = ticker
    return {
        f"{prefix}_price_source_id": (
            window.anchor.source_id if window.anchor else ""
        ),
        f"{prefix}_price_asof_date": (
            window.anchor.bar_date.isoformat() if window.anchor else ""
        ),
        f"{prefix}_price_asof_value": fmt(
            window.anchor.value if window.anchor else None
        ),
        f"{prefix}_price_forward_date": (
            window.forward.bar_date.isoformat() if window.forward else ""
        ),
        f"{prefix}_price_forward_value": fmt(
            window.forward.value if window.forward else None
        ),
        f"{prefix}_forward_return": fmt(forward_return),
        f"forward_excess_return_vs_{prefix}": fmt(
            security_return - forward_return
            if security_return is not None and forward_return is not None
            else None
        ),
    }


def _existing_valid(
    panel_path: Path,
    manifest_path: Path,
    *,
    input_hash: str,
    state_hash: str,
    contract_hash: str,
    calendar_hash: str,
    raw_validation_hash: str,
    generator_hash: str,
    outcome_module_hash: str,
) -> bool:
    if not panel_path.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    artifact = (manifest.get("artifacts") or {}).get("outcome_panel") or {}
    return (
        manifest.get("acceptance") == "PASS"
        and manifest.get("source_panel_sha256") == input_hash
        and manifest.get("market_membership_input_sha256") == state_hash
        and str(
            ((manifest.get("inputs") or {}).get(
                "calibration_contract"
            ) or {}).get("sha256") or ""
        ) == contract_hash
        and str(
            ((manifest.get("inputs") or {}).get(
                "evaluation_calendar"
            ) or {}).get("sha256") or ""
        ) == calendar_hash
        and str(
            ((manifest.get("inputs") or {}).get(
                "historical_raw_load_validation"
            ) or {}).get("sha256") or ""
        ) == raw_validation_hash
        and str(
            ((manifest.get("generator") or {}).get(
                "script_sha256"
            ) or "")
        ) == generator_hash
        and str(
            ((manifest.get("generator") or {}).get(
                "outcome_module_sha256"
            ) or "")
        ) == outcome_module_hash
        and str(artifact.get("sha256") or "") == sha256(panel_path)
    )


def _market_membership_state_hash(
    *,
    prices: Mapping[str, Mapping[str, Sequence[PricePoint]]],
    memberships: Mapping[str, MembershipEvent],
    continuity: Mapping[str, ContinuityPolicy],
    aliases: Mapping[str, Sequence[AliasPolicy]],
) -> str:
    digest = hashlib.sha256()

    def add(*values: object) -> None:
        digest.update(
            ("\x1f".join(str(value) for value in values) + "\n").encode(
                "utf-8"
            )
        )

    for ticker, by_source in sorted(prices.items()):
        for source, points in sorted(by_source.items()):
            for point in points:
                add(
                    "price",
                    ticker,
                    source,
                    point.bar_date.isoformat(),
                    format(point.value, ".17g"),
                    point.price_basis,
                    point.price_adjustment,
                )
    for ticker, event in sorted(memberships.items()):
        add(
            "membership",
            ticker,
            event.start_date.isoformat(),
            event.end_date.isoformat() if event.end_date else "",
            event.membership_status,
            event.terminal_type,
            event.exit_type,
        )
    for ticker, policy in sorted(continuity.items()):
        add(
            "continuity",
            ticker,
            policy.current_security_start_date.isoformat(),
            policy.continuity_policy,
            (
                policy.structural_break_date.isoformat()
                if policy.structural_break_date
                else ""
            ),
            policy.history_treatment,
        )
    for ticker, policies in sorted(aliases.items()):
        for policy in sorted(
            policies,
            key=lambda item: item.effective_date,
        ):
            add(
                "alias",
                ticker,
                policy.active_ticker,
                policy.predecessor_ticker,
                policy.effective_date.isoformat(),
            )
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    generator_path = Path(__file__).resolve()
    outcome_module_path = (
        PROJECT_ROOT / "industrials" / "transportation" / "oos_outcomes.py"
    ).resolve()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = cfg_get(config, "model_families.transportation", {}) or {}
    universe = family.get("universe") or {}
    historical_load = family.get("historical_load") or {}
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else input_dir
    )
    contract_path = (
        input_dir / "transportation_walk_forward_calibration_contract.json"
    )
    panel_manifest_path = (
        input_dir / "transportation_v3_panel_manifest.json"
    )
    coverage_path = (
        input_dir / "transportation_v3_historical_coverage.csv"
    )
    raw_load_dir = resolve_path(
        historical_load.get(
            "output_dir",
            "../output/industrials/transportation/historical_load",
        ),
        base_dir=base_dir,
    )
    raw_validation_path = (
        raw_load_dir / "transportation_historical_raw_load_validation.json"
    )
    for path in (
        contract_path,
        panel_manifest_path,
        coverage_path,
        raw_validation_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    contract = read_json(contract_path)
    panel_manifest = read_json(panel_manifest_path)
    raw_validation = read_json(raw_validation_path)
    if (
        contract.get("acceptance") != "PASS"
        or contract.get("single_calibration_authorized") is not True
        or panel_manifest.get("acceptance") != "PASS"
        or panel_manifest.get("panel_status") != "HASH_FROZEN"
        or raw_validation.get("acceptance") != "PASS"
    ):
        raise ValueError(
            "historical raw load, DP9, and DP10 must be passing and frozen"
        )
    raw_coverage_path = Path(
        str(raw_validation.get("coverage_csv") or "")
    ).resolve()
    if (
        not raw_coverage_path.is_file()
        or sha256(raw_coverage_path)
        != str(raw_validation.get("coverage_csv_sha256") or "")
    ):
        raise ValueError("historical raw-load coverage hash mismatch")
    complete_reference = (
        panel_manifest.get("artifacts") or {}
    ).get("complete_panel") or {}
    complete_path = verify_artifact(
        complete_reference,
        label="frozen complete panel",
    )
    complete_hash = sha256(complete_path)
    if complete_hash != str(contract.get("panel_sha256") or ""):
        raise ValueError("DP10 panel hash does not match DP9 complete panel")
    calendar_reference = (
        contract.get("artifacts") or {}
    ).get("evaluation_calendar") or {}
    calendar_path = verify_artifact(
        calendar_reference,
        label="frozen evaluation calendar",
    )
    calendar_rows = read_csv(calendar_path)
    split_map = {
        row["asof_date"]: row["split_name"] for row in calendar_rows
    }
    candidates = tuple(
        str(value) for value in contract.get("candidate_metric_ids", [])
    )
    overlay = {
        str(key): str(value or "")
        for key, value in (
            contract.get("cohort_specific_overlay") or {}
        ).items()
    }
    forward_days = int(
        (contract.get("outcome_contract") or {}).get(
            "forward_return_trading_days",
            DEFAULT_FORWARD_TRADING_DAYS,
        )
    )
    benchmarks = (
        str(
            (contract.get("outcome_contract") or {}).get(
                "primary_benchmark",
                "IYT",
            )
        ),
        *tuple(
            str(value)
            for value in (
                contract.get("outcome_contract") or {}
            ).get("robustness_benchmarks", [])
        ),
    )
    if tuple(benchmarks) != BENCHMARKS:
        raise ValueError(f"benchmark contract changed={benchmarks}")
    directions = _load_directions(config_path)
    missing_directions = [
        metric for metric in candidates if directions.get(metric) not in {
            "positive",
            "negative",
        }
    ]
    if missing_directions:
        raise ValueError(f"candidate directions missing={missing_directions}")

    source_rows = [
        row
        for row in iter_gzip_csv(complete_path)
        if row.get("metric_id") in candidates
        and row.get("calibration_candidate") == "1"
        and row.get("applicability_status") == "APPLICABLE"
    ]
    if not source_rows:
        raise ValueError("frozen panel has no applicable candidate rows")
    source_rows.sort(
        key=lambda row: (
            row["asof_date"],
            row["ticker"],
            row["metric_id"],
        )
    )
    panel_path = (
        output_dir / "transportation_walk_forward_outcome_panel.csv.gz"
    )
    manifest_path = (
        output_dir
        / "transportation_walk_forward_outcome_panel_manifest.json"
    )
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=base_dir,
        )
    )
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    history_source = str(
        universe.get("historical_membership_source_id") or ""
    )
    active_source = str(
        historical_load.get("active_price_source_id")
        or ACTIVE_PRICE_SOURCE
    )
    delisted_source = str(
        historical_load.get("delisted_price_source_id")
        or "norgate_us_equities_total_return"
    )
    if active_source != ACTIVE_PRICE_SOURCE:
        raise ValueError(f"active price source changed={active_source}")

    with read_only_connection(db_path) as connection:
        aliases = _load_aliases(connection)
        memberships = _load_memberships(
            connection,
            source_id=history_source,
        )
        continuity = _load_continuity(connection)
        price_tickers = set(benchmarks)
        for row in source_rows:
            ticker = row["ticker"].upper()
            price_tickers.add(ticker)
            for alias in aliases.get(ticker, ()):
                price_tickers.add(alias.active_ticker)
                price_tickers.add(alias.predecessor_ticker)
        prices = _load_prices(
            connection,
            tickers=sorted(price_tickers),
            sources=(active_source, delisted_source),
        )
    state_hash = _market_membership_state_hash(
        prices=prices,
        memberships=memberships,
        continuity=continuity,
        aliases=aliases,
    )
    if not args.allow_overwrite and (
        panel_path.exists() or manifest_path.exists()
    ):
        if _existing_valid(
            panel_path,
            manifest_path,
            input_hash=complete_hash,
            state_hash=state_hash,
            contract_hash=sha256(contract_path),
            calendar_hash=sha256(calendar_path),
            raw_validation_hash=sha256(raw_validation_path),
            generator_hash=sha256(generator_path),
            outcome_module_hash=sha256(outcome_module_path),
        ):
            print(f"PASS: keeping sealed outcome panel {panel_path}")
            return 0
        raise FileExistsError(
            "Outcome artifacts exist but are not valid for the frozen input; "
            "use --allow-overwrite after review"
        )

    benchmark_windows: dict[str, dict[str, Any]] = {}
    for asof in split_map:
        benchmark_windows[asof] = {}
        for benchmark in benchmarks:
            window = outcome_window(
                prices.get(benchmark, {}),
                asof=asof,
                forward_trading_days=forward_days,
                source_order=(active_source, delisted_source),
            )
            benchmark_windows[asof][benchmark] = window

    output_rows: list[dict[str, object]] = []
    reason_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    eligible_counts: Counter[str] = Counter()
    for source_row in source_rows:
        asof = source_row["asof_date"]
        ticker = source_row["ticker"].upper()
        metric = source_row["metric_id"]
        split = split_map.get(asof, "")
        if not split:
            raise ValueError(f"candidate row is outside frozen calendar={asof}")
        direction = directions[metric]
        multiplier = -1.0 if direction == "negative" else 1.0
        metric_value = finite_float(source_row.get("metric_value"))
        adjusted_value = (
            metric_value * multiplier if metric_value is not None else None
        )
        asof_date = parse_date(asof, field="asof_date")
        price_ticker, alias_resolution = resolve_price_ticker(
            ticker,
            asof_date,
            aliases,
        )
        membership = memberships.get(ticker)
        policy = continuity.get(ticker)
        primary_benchmark = benchmark_windows[asof]["IYT"]
        horizon_end = (
            primary_benchmark.forward.bar_date
            if primary_benchmark.forward is not None
            else None
        )
        security_window = outcome_window(
            prices.get(price_ticker, {}),
            asof=asof,
            forward_trading_days=forward_days,
            source_order=price_source_order(
                source_row.get("universe_role", "")
            ),
            membership=membership,
            horizon_end=horizon_end,
            continuity=policy,
        )
        security_return = security_window.forward_return
        benchmark_returns = {
            benchmark: benchmark_windows[asof][
                benchmark
            ].forward_return
            for benchmark in benchmarks
        }
        all_benchmarks = all(
            value is not None for value in benchmark_returns.values()
        )
        return_available = security_return is not None and all_benchmarks
        availability_date = optional_date(
            source_row.get("availability_date"),
            field="availability_date",
        )
        source_lookahead = bool(
            availability_date is not None and availability_date > asof_date
        )
        expected_metric = overlay.get(
            source_row.get("calibration_cohort", ""),
            "",
        )
        correct_cohort_metric = expected_metric == metric
        reasons: list[str] = []
        if metric_value is None:
            reasons.append("missing_metric_value")
        if source_lookahead:
            reasons.append("metric_availability_after_asof")
        if not correct_cohort_metric:
            reasons.append("candidate_not_mapped_to_cohort")
        if not return_available:
            reasons.append(
                security_window.unavailable_reason
                or next(
                    (
                        f"{benchmark}_"
                        f"{benchmark_windows[asof][benchmark].unavailable_reason}"
                        for benchmark in benchmarks
                        if benchmark_returns[benchmark] is None
                    ),
                    "missing_forward_return",
                )
            )
        if split == "embargo":
            reasons.append("purged_embargo_snapshot")
        eligible = not reasons
        return_reason = (
            ""
            if return_available
            else security_window.unavailable_reason
            or next(
                (
                    f"{benchmark}_"
                    f"{benchmark_windows[asof][benchmark].unavailable_reason}"
                    for benchmark in benchmarks
                    if benchmark_returns[benchmark] is None
                ),
                "missing_forward_return",
            )
        )
        record: dict[str, object] = {
            "asof_date": asof,
            "ticker": ticker,
            "model_family": MODEL_FAMILY,
            "calibration_cohort": source_row["calibration_cohort"],
            "industry": source_row["industry"],
            "universe_role": source_row["universe_role"],
            "primary_archetype": source_row["primary_archetype"],
            "metric_id": metric,
            "metric_value": fmt(metric_value),
            "direction": direction,
            "direction_multiplier": fmt(multiplier),
            "direction_adjusted_metric_value": fmt(adjusted_value),
            "unit": source_row["unit"],
            "period_start": source_row["period_start"],
            "period_end": source_row["period_end"],
            "availability_date": source_row["availability_date"],
            "availability_status": source_row["availability_status"],
            "source_id": source_row["source_id"],
            "source_record_id": source_row["source_record_id"],
            "confidence": source_row["confidence"],
            "split_name": split,
            "price_ticker": price_ticker,
            "alias_resolution": alias_resolution,
            "price_source_id": (
                security_window.anchor.source_id
                if security_window.anchor
                else ""
            ),
            "price_basis": (
                security_window.anchor.price_basis
                if security_window.anchor
                else ""
            ),
            "price_adjustment": (
                security_window.anchor.price_adjustment
                if security_window.anchor
                else ""
            ),
            "price_asof_date": (
                security_window.anchor.bar_date.isoformat()
                if security_window.anchor
                else ""
            ),
            "price_asof_value": fmt(
                security_window.anchor.value
                if security_window.anchor
                else None
            ),
            "price_forward_date": (
                security_window.forward.bar_date.isoformat()
                if security_window.forward
                else ""
            ),
            "price_forward_value": fmt(
                security_window.forward.value
                if security_window.forward
                else None
            ),
            "forward_trading_days": forward_days,
            "security_forward_session_count": (
                security_window.session_count
                if security_window.session_count is not None
                else ""
            ),
            "security_forward_return": fmt(security_return),
            "outcome_method": security_window.outcome_method,
            "terminal_type": security_window.terminal_type,
            "membership_end_date": (
                membership.end_date.isoformat()
                if membership and membership.end_date
                else ""
            ),
            "continuity_policy": (
                policy.continuity_policy if policy else ""
            ),
            "history_treatment": (
                policy.history_treatment if policy else ""
            ),
            "current_security_start_date": (
                policy.current_security_start_date.isoformat()
                if policy
                else ""
            ),
            "structural_break_date": (
                policy.structural_break_date.isoformat()
                if policy and policy.structural_break_date
                else ""
            ),
            "security_return_available_flag": int(
                security_return is not None
            ),
            "all_benchmark_returns_available_flag": int(all_benchmarks),
            "return_available_flag": int(return_available),
            "return_unavailable_reason": return_reason,
            "metric_value_available_flag": int(metric_value is not None),
            "panel_row_eligible_flag": int(eligible),
            "panel_row_eligible_reason": (
                "eligible"
                if eligible
                else ";".join(dict.fromkeys(reasons))
            ),
        }
        for benchmark in benchmarks:
            record.update(
                _benchmark_fields(
                    benchmark,
                    benchmark_windows[asof][benchmark],
                    benchmark_returns[benchmark],
                    security_return,
                )
            )
        output_rows.append(record)
        reason_counts[
            str(record["panel_row_eligible_reason"])
        ] += 1
        if security_window.outcome_method:
            method_counts[security_window.outcome_method] += 1
        if security_window.anchor:
            source_counts[security_window.anchor.source_id] += 1
        if eligible:
            eligible_counts[metric] += 1

    panel_row_count = write_gzip_csv_atomic(
        panel_path,
        PANEL_FIELDS,
        output_rows,
    )
    errors: list[str] = []
    if panel_row_count != len(source_rows):
        errors.append("outcome row count differs from applicable candidate rows")
    duplicate_count = len(output_rows) - len(
        {
            (row["asof_date"], row["ticker"], row["metric_id"])
            for row in output_rows
        }
    )
    if duplicate_count:
        errors.append(f"duplicate candidate keys={duplicate_count}")
    acceptance = "PASS" if not errors else "FAIL"
    manifest = {
        "acceptance": acceptance,
        "gate": "DP11_BUILD_WALK_FORWARD_OUTCOMES",
        "panel_version": OUTCOME_PANEL_VERSION,
        "generator": {
            "script_path": str(generator_path),
            "script_sha256": sha256(generator_path),
            "outcome_module_path": str(outcome_module_path),
            "outcome_module_sha256": sha256(outcome_module_path),
        },
        "model_family": MODEL_FAMILY,
        "source_panel_sha256": complete_hash,
        "market_membership_input_sha256": state_hash,
        "forward_trading_days": forward_days,
        "candidate_metric_ids": list(candidates),
        "candidate_metric_count": len(candidates),
        "benchmark_tickers": list(benchmarks),
        "primary_benchmark": "IYT",
        "snapshot_count": len(split_map),
        "first_snapshot_date": min(split_map),
        "last_snapshot_date": max(split_map),
        "applicable_candidate_row_count": len(source_rows),
        "outcome_panel_row_count": panel_row_count,
        "eligible_row_count": sum(eligible_counts.values()),
        "eligible_rows_by_metric": dict(sorted(eligible_counts.items())),
        "outcome_method_counts": dict(sorted(method_counts.items())),
        "price_source_counts": dict(sorted(source_counts.items())),
        "eligibility_reason_counts": dict(sorted(reason_counts.items())),
        "terminal_event_contract": {
            "trigger": (
                "membership end after signal and on or before the primary "
                "benchmark 63-session horizon end"
            ),
            "acquisition": "last verified adjusted close",
            "distressed_nonzero": "last verified adjusted close",
            "wipeout": "reviewed explicit zero",
            "post_terminal_horizon_treatment": (
                "terminal proceeds held as zero-return cash through the "
                "full benchmark horizon"
            ),
            "maximum_terminal_price_staleness_days": 10,
        },
        "security_continuity_contract": {
            "verified_alias_rows_loaded": sum(
                len(values) for values in aliases.values()
            ),
            "continuity_policy_rows_loaded": len(continuity),
            "no_cross_source_window_stitch": True,
            "no_structural_break_stitch": True,
            "cross_listing_predecessor_proxy_used": False,
        },
        "artifacts": {
            "outcome_panel": _artifact(
                panel_path,
                rows=panel_row_count,
            )
        },
        "inputs": {
            "calibration_contract": _artifact(contract_path),
            "panel_manifest": _artifact(panel_manifest_path),
            "complete_panel": _artifact(
                complete_path,
                rows=int(complete_reference.get("row_count") or 0),
            ),
            "evaluation_calendar": _artifact(
                calendar_path,
                rows=len(calendar_rows),
            ),
            "historical_coverage": _artifact(
                coverage_path,
                rows=len(read_csv(coverage_path)),
            ),
            "historical_raw_load_validation": _artifact(
                raw_validation_path,
            ),
            "historical_raw_load_coverage": _artifact(
                raw_coverage_path,
                rows=len(read_csv(raw_coverage_path)),
            ),
            "database_path": str(db_path),
        },
        "operations": {
            "database_mode": "read_only",
            "database_writes": 0,
            "parser_invocations": 0,
            "source_document_opens": 0,
            "network_requests": 0,
            "feature_rebuilds": 0,
            "membership_rebuilds": 0,
            "portfolio_writes": 0,
            "calibration_invocations": 0,
        },
        "calibration_executed": False,
        "production_promotion_authorized": False,
        "errors": errors,
        "next_gate": (
            "VALIDATE_WALK_FORWARD_OUTCOME_READINESS"
            if acceptance == "PASS"
            else "REVIEW_OUTCOME_BUILD_ERRORS"
        ),
    }
    write_manifest(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
