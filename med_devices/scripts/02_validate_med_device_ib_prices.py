#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.market_policy import live_validation_primary_source  # noqa: E402
from med_devices.core.text_norm import as_bool, normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_med_device_ib_prices")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_INPUT = PROJECT_ROOT / "ticker_mapping" / "med_dev_tickers_clean_keep.csv"
DEFAULT_SOURCE = "ib_market_data"
DEFAULT_HARD_FAIL_REASONS = {
    "missing_ticker",
    "contract_not_qualified",
    "contract_qualification_failed",
    "non_usd_ib_contract",
    "historical_data_failed",
    "no_usable_bars",
    "stale_ib_bar",
    "insufficient_ib_history",
    "too_few_ib_bars",
    "low_ib_avg_dollar_volume",
}

FIELDNAMES = [
    "ticker",
    "company_name",
    "cik",
    "source_id",
    "price_adjustment",
    "is_adjusted",
    "input_exchange",
    "input_currency",
    "ib_status",
    "contract_status",
    "price_status",
    "recommended_action",
    "review_reason",
    "qualified_symbol",
    "con_id",
    "ib_exchange",
    "primary_exchange",
    "ib_currency",
    "trading_class",
    "what_to_show",
    "fallback_used",
    "first_bar_date",
    "last_bar_date",
    "bar_count",
    "history_years",
    "latest_close",
    "avg_dollar_volume_60d",
    "duration",
    "requested_asof_date",
    "effective_asof_date",
    "asof_guard_reason",
]


@dataclass(frozen=True)
class IbPolicy:
    source_id: str
    host: str
    port: int
    client_id: int
    duration: str
    default_exchange: str
    default_currency: str
    bar_size: str
    format_date: int
    keep_up_to_date: bool
    required_history_years: float
    minimum_history_years: float
    max_bar_staleness_days: int
    min_trading_bars: int
    min_avg_dollar_volume_60d: float
    what_to_show: str
    fallback_what_to_show: str
    adjusted_what_to_show_values: set[str]
    use_rth: bool
    sleep_sec: float
    connect_timeout_sec: float
    market_timezone: str
    market_close_time: dt_time
    market_close_guard: bool
    hard_fail_reasons: set[str]


@dataclass(frozen=True)
class AsofDecision:
    requested_asof: date
    effective_asof: date
    guard_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate IB daily-price coverage for the clean medical-device ticker universe."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Validation date in YYYY-MM-DD. Defaults to market-local today.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Smoke-test limit; 0 means all tickers.")
    parser.add_argument("--duration", type=str, default="", help="Override IB duration string, for example '8 Y'.")
    parser.add_argument("--required-history-years", type=float, default=None)
    parser.add_argument("--minimum-history-years", type=float, default=None)
    parser.add_argument("--allow-partial", action="store_true", help="Exit 0 even when some tickers fail validation.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()
    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.client").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.ib").setLevel(logging.WARNING)


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_clock_time(raw: object, default: str = "16:15") -> dt_time:
    text = str(raw or default).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid market close time: {raw}")


def str_set(raw: object, default: set[str]) -> set[str]:
    values = raw if isinstance(raw, list) else list(default)
    out = {str(value or "").strip().upper() for value in values if str(value or "").strip()}
    return out or set(default)


def to_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def previous_business_day(day: date) -> date:
    out = day - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def resolve_effective_asof(requested_asof: date, policy: IbPolicy, *, now: datetime | None = None) -> AsofDecision:
    tz = ZoneInfo(policy.market_timezone)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    local_today = now_local.date()
    before_close = now_local.time() < policy.market_close_time
    effective_asof = requested_asof
    reason = ""
    if policy.market_close_guard and requested_asof > local_today:
        effective_asof = previous_business_day(local_today) if local_today.weekday() >= 5 or before_close else local_today
        reason = "future_asof_clamped"
    elif policy.market_close_guard and requested_asof >= local_today and local_today.weekday() >= 5:
        effective_asof = previous_business_day(local_today)
        reason = "market_closed_weekend"
    elif policy.market_close_guard and requested_asof >= local_today and before_close:
        effective_asof = previous_business_day(local_today)
        reason = "before_market_close"
    if effective_asof.weekday() >= 5:
        effective_asof = previous_business_day(effective_asof)
        reason = reason or "weekend_asof"
    return AsofDecision(requested_asof=requested_asof, effective_asof=effective_asof, guard_reason=reason)


def requested_asof_from_args(raw: str, policy: IbPolicy) -> date:
    parsed = parse_date(raw)
    if raw and parsed is None:
        raise ValueError(f"Invalid --asof date, expected YYYY-MM-DD: {raw}")
    if parsed is not None:
        return parsed
    return datetime.now(ZoneInfo(policy.market_timezone)).date()


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def default_policy(config: dict[str, Any], args: argparse.Namespace) -> IbPolicy:
    required_history = (
        float(args.required_history_years)
        if args.required_history_years is not None
        else float(cfg_get(config, "ib_price_validation.required_history_years", 7.0))
    )
    minimum_history = (
        float(args.minimum_history_years)
        if args.minimum_history_years is not None
        else float(cfg_get(config, "ib_price_validation.minimum_history_years", 5.0))
    )
    return IbPolicy(
        source_id=str(cfg_get(config, "ib_price_validation.source_id", live_validation_primary_source(config)) or DEFAULT_SOURCE),
        host=str(cfg_get(config, "ib_price_validation.host", "127.0.0.1")),
        port=int(cfg_get(config, "ib_price_validation.port", 7497)),
        client_id=int(cfg_get(config, "ib_price_validation.client_id", 7727)),
        duration=str(args.duration or cfg_get(config, "ib_price_validation.duration", "8 Y")),
        default_exchange=str(cfg_get(config, "ib_price_validation.default_exchange", "SMART") or "SMART").strip().upper(),
        default_currency=str(cfg_get(config, "ib_price_validation.default_currency", "USD") or "USD").strip().upper(),
        bar_size=str(cfg_get(config, "ib_price_validation.bar_size", "1 day") or "1 day").strip(),
        format_date=int(cfg_get(config, "ib_price_validation.format_date", 1)),
        keep_up_to_date=as_bool(cfg_get(config, "ib_price_validation.keep_up_to_date", False)),
        required_history_years=required_history,
        minimum_history_years=minimum_history,
        max_bar_staleness_days=int(cfg_get(config, "ib_price_validation.max_bar_staleness_days", 7)),
        min_trading_bars=int(cfg_get(config, "ib_price_validation.min_trading_bars", 1000)),
        min_avg_dollar_volume_60d=float(
            cfg_get(
                config,
                "ib_price_validation.min_avg_dollar_volume_60d",
                cfg_get(config, "med_devices_universe.avg_dollar_volume_60d_min", 1_000_000),
            )
        ),
        what_to_show=str(cfg_get(config, "ib_price_validation.what_to_show", "ADJUSTED_LAST")).strip().upper(),
        fallback_what_to_show=str(cfg_get(config, "ib_price_validation.fallback_what_to_show", "TRADES")).strip().upper(),
        adjusted_what_to_show_values=str_set(
            cfg_get(config, "ib_price_validation.adjusted_what_to_show_values", ["ADJUSTED_LAST"]),
            {"ADJUSTED_LAST"},
        ),
        use_rth=as_bool(cfg_get(config, "ib_price_validation.use_rth", True)),
        sleep_sec=float(cfg_get(config, "ib_price_validation.sleep_sec", 0.15)),
        connect_timeout_sec=float(cfg_get(config, "ib_price_validation.connect_timeout_sec", 15.0)),
        market_timezone=str(cfg_get(config, "ib_price_validation.market_timezone", "America/New_York")),
        market_close_time=parse_clock_time(cfg_get(config, "ib_price_validation.market_close_time", "16:15")),
        market_close_guard=as_bool(cfg_get(config, "ib_price_validation.market_close_guard", True)),
        hard_fail_reasons={reason.lower() for reason in str_set(
            cfg_get(config, "ib_price_validation.hard_fail_reasons", list(DEFAULT_HARD_FAIL_REASONS)),
            DEFAULT_HARD_FAIL_REASONS,
        )},
    )


def resolve_paths(config: dict[str, Any], config_path: Path, args: argparse.Namespace) -> tuple[Path, Path]:
    base_dir = config_path.parent
    input_csv = (
        args.input.expanduser().resolve()
        if args.input
        else resolve_path(cfg_get(config, "ib_price_validation.input_csv", DEFAULT_INPUT), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "ib_price_validation.output_csv", "../output/med_devices_reports/med_device_ib_price_coverage.csv"),
            base_dir=base_dir,
        )
    )
    return input_csv, output_csv


def ib_end_datetime(asof_date: date, policy: IbPolicy) -> str:
    return f"{asof_date.strftime('%Y%m%d')} 23:59:59 {policy.market_timezone}"


def request_bars(
    ib: Any,
    contract: Any,
    *,
    asof_date: date,
    duration: str,
    what_to_show: str,
    policy: IbPolicy,
) -> list[Any]:
    return list(
        ib.reqHistoricalData(
            contract,
            endDateTime=ib_end_datetime(asof_date, policy),
            durationStr=duration,
            barSizeSetting=policy.bar_size,
            whatToShow=what_to_show,
            useRTH=policy.use_rth,
            formatDate=policy.format_date,
            keepUpToDate=policy.keep_up_to_date,
        )
        or []
    )


def row_base(row: dict[str, str], decision: AsofDecision, policy: IbPolicy) -> dict[str, Any]:
    return {
        "ticker": normalize_ticker(row.get("Name") or row.get("Ticker")),
        "company_name": str(row.get("Company_Name") or row.get("CompanyName") or "").strip(),
        "cik": normalize_cik(row.get("CIK")),
        "source_id": policy.source_id or DEFAULT_SOURCE,
        "price_adjustment": "",
        "is_adjusted": "",
        "input_exchange": str(row.get("Exchange") or "").strip(),
        "input_currency": str(row.get("Currency") or "").strip() or policy.default_currency,
        "duration": policy.duration,
        "requested_asof_date": decision.requested_asof.isoformat(),
        "effective_asof_date": decision.effective_asof.isoformat(),
        "asof_guard_reason": decision.guard_reason,
    }


def bar_day(raw: object) -> date | None:
    if isinstance(raw, (date, datetime)):
        return parse_date(raw.isoformat()[:10])
    return parse_date(str(raw or "")[:10])


def summarize_bars(bars: list[Any], policy: IbPolicy, asof_date: date) -> tuple[dict[str, Any], list[str]]:
    parsed: list[tuple[date, float, float]] = []
    for bar in bars:
        day = bar_day(getattr(bar, "date", None))
        close = to_float(getattr(bar, "close", None))
        volume = to_float(getattr(bar, "volume", None))
        if day is None or close is None or close <= 0:
            continue
        parsed.append((day, close, volume or 0.0))
    parsed.sort(key=lambda item: item[0])

    reasons: list[str] = []
    if not parsed:
        return (
            {
                "first_bar_date": "",
                "last_bar_date": "",
                "bar_count": 0,
                "history_years": 0.0,
                "latest_close": "",
                "avg_dollar_volume_60d": "",
            },
            ["no_usable_bars"],
        )

    first_day = parsed[0][0]
    last_day = parsed[-1][0]
    history_years = round((last_day - first_day).days / 365.25, 2)
    latest_close = parsed[-1][1]
    recent = parsed[-60:]
    avg_dollar_volume = sum(close * volume for _, close, volume in recent) / max(1, len(recent))

    if (asof_date - last_day).days > policy.max_bar_staleness_days:
        reasons.append("stale_ib_bar")
    if len(parsed) < policy.min_trading_bars:
        reasons.append("too_few_ib_bars")
    if history_years < policy.minimum_history_years:
        reasons.append("insufficient_ib_history")
    elif history_years < policy.required_history_years:
        reasons.append("short_ib_history")
    if avg_dollar_volume < policy.min_avg_dollar_volume_60d:
        reasons.append("low_ib_avg_dollar_volume")

    return (
        {
            "first_bar_date": first_day.isoformat(),
            "last_bar_date": last_day.isoformat(),
            "bar_count": len(parsed),
            "history_years": history_years,
            "latest_close": round(latest_close, 6),
            "avg_dollar_volume_60d": round(avg_dollar_volume, 2),
        },
        reasons,
    )


def classify(reasons: list[str], policy: IbPolicy) -> str:
    if not reasons:
        return "pass"
    if any(reason.split(":", 1)[0] in policy.hard_fail_reasons for reason in reasons):
        return "fail"
    return "review"


def validate_one(ib: Any, stock_cls: Any, row: dict[str, str], *, decision: AsofDecision, policy: IbPolicy) -> dict[str, Any]:
    out = row_base(row, decision, policy)
    ticker = str(out["ticker"])
    reasons: list[str] = []
    if not ticker:
        reasons.append("missing_ticker")
        out.update({"ib_status": "fail", "contract_status": "fail", "price_status": "fail"})
        out["recommended_action"] = "fail"
        out["review_reason"] = ";".join(reasons)
        return out

    currency = str(out.get("input_currency") or policy.default_currency).upper()
    contract = stock_cls(ticker, policy.default_exchange, currency or policy.default_currency)
    qualified: list[Any] = []
    try:
        qualified = list(ib.qualifyContracts(contract) or [])
    except Exception as exc:
        reasons.append(f"contract_qualification_failed:{exc}")

    if not qualified:
        reasons.append("contract_not_qualified")
        out.update({"ib_status": "fail", "contract_status": "fail", "price_status": "fail"})
        out["recommended_action"] = classify(reasons, policy)
        out["review_reason"] = ";".join(reasons)
        return out

    q = qualified[0]
    out.update(
        {
            "qualified_symbol": str(getattr(q, "symbol", "") or ""),
            "con_id": str(getattr(q, "conId", "") or ""),
            "ib_exchange": str(getattr(q, "exchange", "") or ""),
            "primary_exchange": str(getattr(q, "primaryExchange", "") or ""),
            "ib_currency": str(getattr(q, "currency", "") or ""),
            "trading_class": str(getattr(q, "tradingClass", "") or ""),
            "contract_status": "pass",
        }
    )
    if str(out.get("ib_currency") or "").upper() != policy.default_currency:
        reasons.append(f"non_usd_ib_contract:{out.get('ib_currency')}")

    attempts = [policy.what_to_show]
    fallback = policy.fallback_what_to_show
    if fallback and fallback not in attempts:
        attempts.append(fallback)
    bars: list[Any] = []
    used_what_to_show = attempts[0]
    last_error: Exception | None = None
    for attempt in attempts:
        try:
            bars = request_bars(
                ib,
                q,
                asof_date=decision.effective_asof,
                duration=policy.duration,
                what_to_show=attempt,
                policy=policy,
            )
            if bars:
                used_what_to_show = attempt
                break
            last_error = ValueError(f"IB returned no bars using {attempt}")
        except Exception as exc:
            last_error = exc
            continue
        finally:
            ib.sleep(policy.sleep_sec)

    out["what_to_show"] = used_what_to_show
    out["fallback_used"] = int(used_what_to_show != policy.what_to_show)
    is_adjusted = used_what_to_show in policy.adjusted_what_to_show_values
    out["price_adjustment"] = "adjusted" if is_adjusted else "raw"
    out["is_adjusted"] = int(is_adjusted)
    if not bars:
        reasons.append(f"historical_data_failed:{last_error or 'empty_response'}")
        out.update({"ib_status": "fail", "price_status": "fail"})
        out["recommended_action"] = classify(reasons, policy)
        out["review_reason"] = ";".join(reasons)
        return out

    summary, price_reasons = summarize_bars(bars, policy, decision.effective_asof)
    reasons.extend(price_reasons)
    out.update(summary)
    out["price_status"] = "pass" if not price_reasons else "fail" if classify(price_reasons, policy) == "fail" else "review"
    out["recommended_action"] = classify(reasons, policy)
    out["ib_status"] = "pass" if out["recommended_action"] == "pass" else out["recommended_action"]
    out["review_reason"] = ";".join(reasons)
    return out


def selected_rows(rows: list[dict[str, str]], args: argparse.Namespace) -> list[dict[str, str]]:
    ticker_filter = {normalize_ticker(item) for item in str(args.tickers or "").split(",") if normalize_ticker(item)}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        ticker = normalize_ticker(row.get("Name") or row.get("Ticker"))
        if not ticker or ticker in seen:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(row)
        seen.add(ticker)
        if int(args.max_tickers) > 0 and len(out) >= int(args.max_tickers):
            break
    return out


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    policy = default_policy(config, args)
    requested_asof = requested_asof_from_args(str(args.asof or ""), policy)
    decision = resolve_effective_asof(requested_asof, policy)
    input_csv, output_csv = resolve_paths(config, config_path, args)

    rows = selected_rows(read_csv_flexible(input_csv), args)
    LOGGER.info("Loaded IB validation rows=%d input=%s", len(rows), input_csv)
    if not rows:
        raise ValueError("No tickers selected for IB price validation")

    try:
        from ib_insync import IB, Stock  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ib_insync is required for IB price validation. Install package 'ib_insync'.") from exc

    ib = IB()
    results: list[dict[str, Any]] = []
    try:
        ib.connect(policy.host, policy.port, clientId=policy.client_id, timeout=policy.connect_timeout_sec)
        for idx, row in enumerate(rows, start=1):
            result = validate_one(ib, Stock, row, decision=decision, policy=policy)
            results.append(result)
            LOGGER.info(
                "[%d/%d] IB %s action=%s bars=%s last_bar=%s reasons=%s",
                idx,
                len(rows),
                result.get("ticker"),
                result.get("recommended_action"),
                result.get("bar_count", ""),
                result.get("last_bar_date", ""),
                result.get("review_reason") or "none",
            )
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            LOGGER.debug("Ignoring IB disconnect error", exc_info=True)

    write_csv(output_csv, results)
    action_counts: dict[str, int] = {}
    for row in results:
        action = str(row.get("recommended_action") or "")
        action_counts[action] = action_counts.get(action, 0) + 1
    LOGGER.info("Wrote IB price validation report: %s", output_csv)
    LOGGER.info("IB validation summary rows=%d action_counts=%s", len(results), action_counts)
    if not args.allow_partial and any(row.get("recommended_action") == "fail" for row in results):
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        configure_logging()
        LOGGER.exception("Fatal med-device IB price validation error: %s", exc)
        raise SystemExit(1) from exc
