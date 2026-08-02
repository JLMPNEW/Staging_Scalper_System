#!/usr/bin/env python3
"""Sync next-earnings dates for every scored name and sealed IB stock holding.

Provider waterfall (adapted from PROD earnings_release_lookup.py):
  1. Alpha Vantage EARNINGS_CALENDAR in BULK mode (no symbol) -- ONE request covers the
     entire US calendar, so the whole scored universe costs a single API call. The raw
     CSV is cached per fetch-day so re-runs never spend extra free-tier quota.
  2. Yahoo Finance (yfinance) per-ticker, for names the bulk calendar misses.
  3. Gemini with Google Search grounding via REST, last resort only, hard-capped and
     paced to stay inside the free tier.

Outputs: `runs/<as_of>/earnings_dates/earnings_calendar.csv` + manifest, and an
append-only PIT history at `output/earnings_dates/earnings_calendar_history.csv`.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    read_csv,
    sha256_file,
    write_csv,
    write_manifest,
    write_text_atomic,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.earnings_dates.earnings_common import (  # noqa: E402
    EARNINGS_CALENDAR_FIELDS,
    HISTORY_RELATIVE_PATH,
    RUN_ARTIFACT_NAME,
    RUN_MANIFEST_NAME,
    active_query_symbol,
    append_history,
    coerce_iso_date,
    latest_prior_dates,
    latest_accepted_stock_ledger,
    latest_run_with_artifact,
    load_history,
    source_hashes,
    symbol_variants,
)


LOGGER = logging.getLogger("sync_earnings_dates")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["earnings_common.py", "37_sync_earnings_dates.py"]

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def env_key(names: list[str]) -> str | None:
    import os

    for name in names:
        value = os.environ.get(str(name), "").strip()
        if value:
            return value
    return None


# ---------------------------------------------------------------------------
# Provider 1: Alpha Vantage bulk earnings calendar
# ---------------------------------------------------------------------------
def fetch_alpha_vantage_bulk(
    *,
    api_key: str,
    horizon: str,
    cache_path: Path,
    refresh: bool,
    timeout_sec: float,
) -> tuple[str | None, str | None]:
    """Return (raw_csv_text, error). Uses the per-day cache unless `refresh`."""
    if cache_path.exists() and not refresh:
        LOGGER.info("Alpha Vantage bulk calendar: using cached %s", cache_path.name)
        return cache_path.read_text(encoding="utf-8"), None

    import requests

    params = {"function": "EARNINGS_CALENDAR", "horizon": horizon, "apikey": api_key}
    try:
        resp = requests.get(ALPHA_VANTAGE_URL, params=params, timeout=timeout_sec)
    except Exception as exc:  # noqa: BLE001 - provider errors become row-level detail
        return None, f"request_error:{type(exc).__name__}"
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"
    text = (resp.text or "").strip()
    if not text:
        return None, "empty_response"
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            for key in ("Note", "Information", "Error Message"):
                if key in obj and str(obj[key]).strip():
                    message = re.sub(r"\s+", " ", str(obj[key]).strip())
                    return None, f"api_message:{message[:180]}"
        except json.JSONDecodeError:
            pass
        return None, "json_non_calendar_response"
    if "reportDate" not in text.splitlines()[0]:
        return None, "unexpected_csv_header"
    write_text_atomic(cache_path, text)
    LOGGER.info("Alpha Vantage bulk calendar: fetched %d bytes -> %s", len(text), cache_path.name)
    return text, None


def parse_bulk_calendar(raw_csv: str) -> dict[str, list[dict[str, str]]]:
    """symbol(upper) -> rows with reportDate/fiscalDateEnding/estimate."""
    out: dict[str, list[dict[str, str]]] = {}
    for row in csv.DictReader(io.StringIO(raw_csv)):
        symbol = str(row.get("symbol", "")).strip().upper()
        report_date = coerce_iso_date(row.get("reportDate"))
        if not symbol or report_date is None:
            continue
        out.setdefault(symbol, []).append(
            {
                "report_date": report_date,
                "fiscal_date_ending": coerce_iso_date(row.get("fiscalDateEnding")) or "",
                "estimate": str(row.get("estimate", "")).strip(),
            }
        )
    return out


def next_from_bulk(
    calendar: dict[str, list[dict[str, str]]], variants: list[str], floor: date
) -> tuple[dict[str, str] | None, str]:
    """Earliest future calendar row across symbol variants; also returns the matched symbol."""
    best: dict[str, str] | None = None
    matched = ""
    for variant in variants:
        for row in calendar.get(variant, []):
            row_date = date.fromisoformat(row["report_date"])
            if row_date < floor:
                continue
            if best is None or row["report_date"] < best["report_date"]:
                best = row
                matched = variant
    return best, matched


# ---------------------------------------------------------------------------
# Provider 2: Yahoo Finance (yfinance)
# ---------------------------------------------------------------------------
def fetch_next_earnings_yahoo(symbol: str, *, floor: date, cache_dir: Path) -> tuple[str | None, str | None]:
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError as exc:
        return None, f"yahoo_import_error:{type(exc).__name__}"
    import pandas as pd

    try:
        tz_cache = cache_dir / ".yf_tz_cache"
        tz_cache.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(tz_cache))
    except OSError:
        pass

    def future_min(values: Any) -> str | None:
        candidates: list[date] = []
        for value in values:
            stamp = pd.to_datetime(value, errors="coerce")
            if pd.isna(stamp):
                continue
            when = stamp.to_pydatetime().date()
            if when >= floor:
                candidates.append(when)
        return min(candidates).isoformat() if candidates else None

    errors: list[str] = []
    try:
        ticker_obj = yf.Ticker(symbol)
    except Exception as exc:  # noqa: BLE001
        return None, f"yahoo_ticker_error:{type(exc).__name__}"

    try:
        frame = ticker_obj.get_earnings_dates(limit=12)
        if frame is not None and len(frame) > 0:
            found = future_min(getattr(frame, "index", []))
            if found:
                return found, None
        errors.append("yahoo_no_future_from_get_earnings_dates")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"yahoo_get_earnings_dates_error:{type(exc).__name__}")

    try:
        calendar = ticker_obj.calendar
        values: list[Any] = []
        if isinstance(calendar, dict):
            for value in calendar.values():
                values.extend(value if isinstance(value, list) else [value])
        elif calendar is not None:
            try:
                values = list(calendar.values.ravel())
            except AttributeError:
                values = list(calendar)
        found = future_min(values)
        if found:
            return found, None
        errors.append("yahoo_no_future_from_calendar")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"yahoo_calendar_error:{type(exc).__name__}")

    return None, "|".join(errors)


# ---------------------------------------------------------------------------
# Provider 3: Gemini grounded search (REST, free-tier paced)
# ---------------------------------------------------------------------------
def extract_json_obj(text: str) -> dict[str, Any] | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(cleaned[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def fetch_next_earnings_gemini(
    symbol: str, *, floor: date, model: str, api_key: str, timeout_sec: float
) -> tuple[str | None, str | None, str | None, list[str]]:
    """Return (next_date, confidence, error, grounding_urls)."""
    import requests

    prompt = (
        "You are verifying financial events with web search.\n"
        f"Ticker: {symbol}\n"
        f"As-of date: {floor.isoformat()}\n"
        "Task: find the NEXT upcoming earnings release date for this company.\n"
        "Return ONLY valid JSON with this exact schema:\n"
        '{"ticker":"<ticker>","next_earnings_date":"YYYY-MM-DD or null",'
        '"confidence":"high|medium|low","note":"short source-based reasoning"}\n'
        "Rules:\n"
        "- If multiple dates conflict, choose the most credible primary source.\n"
        "- If no reliable date found, set next_earnings_date to null.\n"
        "- Do not include markdown or extra text."
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.0},
    }
    try:
        resp = requests.post(
            GEMINI_URL_TEMPLATE.format(model=model),
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_sec,
        )
    except Exception as exc:  # noqa: BLE001
        return None, None, f"gemini_request_error:{type(exc).__name__}", []
    if resp.status_code != 200:
        detail = re.sub(r"\s+", " ", (resp.text or ""))[:160]
        return None, None, f"gemini_http_{resp.status_code}:{detail}", []
    try:
        body = resp.json()
    except json.JSONDecodeError:
        return None, None, "gemini_non_json_body", []

    candidates = body.get("candidates") or []
    text = ""
    urls: list[str] = []
    if candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text += part["text"]
        grounding = candidates[0].get("groundingMetadata") or {}
        for chunk in grounding.get("groundingChunks") or []:
            uri = str(((chunk or {}).get("web") or {}).get("uri", "")).strip()
            if uri and uri not in urls:
                urls.append(uri)

    obj = extract_json_obj(text)
    if not obj:
        return None, None, "gemini_non_json_response", urls
    next_date = coerce_iso_date(obj.get("next_earnings_date"))
    confidence = str(obj.get("confidence", "")).strip().lower() or None
    if confidence not in (None, "high", "medium", "low"):
        confidence = None
    if next_date is not None and date.fromisoformat(next_date) < floor:
        return None, confidence, "gemini_date_in_past", urls
    return next_date, confidence, None, urls


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", default="", help="Run directory date (default: latest sealed stocks_scores)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing earnings artifact for the run")
    parser.add_argument("--refresh-av-cache", action="store_true", help="Ignore today's cached bulk calendar")
    parser.add_argument("--disable-yahoo", action="store_true")
    parser.add_argument("--disable-gemini", action="store_true")
    parser.add_argument("--max-yahoo-calls", type=int, default=-1, help="-1 uses the config value")
    parser.add_argument("--max-gemini-calls", type=int, default=-1, help="-1 uses the config value")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_utc_logging(getattr(logging, str(args.log_level).upper(), logging.INFO))
    config = load_yaml(args.config)
    paths = resolve_runtime_paths(config, args.config.resolve())
    runs_root = paths.output_dir / "runs"

    run_as_of = str(args.as_of).strip() or latest_run_with_artifact(runs_root, "stocks_scores.csv")
    if not run_as_of:
        LOGGER.error("No run directory with stocks_scores.csv under %s", runs_root)
        return 1
    scores_path = runs_root / run_as_of / "stocks_scores.csv"
    if not scores_path.exists():
        LOGGER.error("Missing %s", scores_path)
        return 1

    out_dir = runs_root / run_as_of / "earnings_dates"
    artifact_path = out_dir / RUN_ARTIFACT_NAME
    manifest_path = out_dir / RUN_MANIFEST_NAME
    # Idempotent under orchestration: a same-day re-run without --force is a clean no-op,
    # so the daily pipeline never spends provider quota twice for one sealed run.
    if artifact_path.exists() and manifest_path.exists() and not args.force:
        LOGGER.info("EARNINGS DATES: already synced for run=%s (%s); use --force to refresh", run_as_of, artifact_path)
        return 0

    include_non_investable = bool(cfg_get(config, "earnings_dates.include_non_investable", True))
    aliases = cfg_get(config, "risk_panel.ticker_aliases", {}) or {}

    score_rows = read_csv(scores_path)
    universe: dict[str, dict[str, str]] = {}
    for row in score_rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker in universe:
            continue
        investable = str(row.get("investable_eligible", "")).strip()
        if not include_non_investable and investable != "1":
            continue
        universe[ticker] = {
            "ticker": ticker,
            "investable_eligible": "1" if investable == "1" else "0",
            "source_pipeline": str(row.get("source_pipeline", "")),
            "sector": str(row.get("sector", "")),
        }
    ledger_run = latest_accepted_stock_ledger(runs_root, run_as_of)
    ledger_path: Path | None = None
    ledger_manifest_path: Path | None = None
    holding_count = 0
    if ledger_run is not None:
        ledger_path = ledger_run / "ledger" / "broker_net_stock_positions.csv"
        ledger_manifest_path = ledger_run / "ledger" / "ledger_manifest.json"
        for row in read_csv(ledger_path):
            ticker = str(row.get("symbol", "")).strip().upper()
            if not ticker:
                continue
            holding_count += 1
            universe.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "investable_eligible": "0",
                    "source_pipeline": "broker_holding_only",
                    "sector": "",
                },
            )
    if not universe:
        LOGGER.error("Universe is empty after filtering %s", scores_path)
        return 1
    LOGGER.info(
        "Universe: %d tickers (%d investable, %d IB stock holdings) from %s",
        len(universe),
        sum(1 for r in universe.values() if r["investable_eligible"] == "1"),
        holding_count,
        scores_path.name,
    )

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    floor = date.today()

    # Provider 1: one bulk call for the whole universe.
    av_error: str | None = None
    calendar: dict[str, list[dict[str, str]]] = {}
    av_cache_path = paths.cache_dir / "earnings_dates" / f"alpha_vantage_earnings_calendar_{floor.isoformat()}.csv"
    av_key = env_key(list(cfg_get(config, "earnings_dates.alpha_vantage.api_key_env", ["ALPHAVANTAGE_API_KEY"])))
    if av_key:
        raw_csv, av_error = fetch_alpha_vantage_bulk(
            api_key=av_key,
            horizon=str(cfg_get(config, "earnings_dates.alpha_vantage.horizon", "12month")),
            cache_path=av_cache_path,
            refresh=bool(args.refresh_av_cache),
            timeout_sec=float(cfg_get(config, "earnings_dates.alpha_vantage.timeout_sec", 120.0)),
        )
        if raw_csv is not None:
            calendar = parse_bulk_calendar(raw_csv)
            LOGGER.info("Alpha Vantage bulk calendar: %d symbols parsed", len(calendar))
    else:
        av_error = "alpha_vantage_api_key_missing"
    if av_error:
        LOGGER.warning("Alpha Vantage bulk calendar unavailable: %s", av_error)

    # Fallback budgets (free-tier discipline).
    yahoo_enabled = bool(cfg_get(config, "earnings_dates.yahoo.enabled", True)) and not args.disable_yahoo
    yahoo_budget = int(args.max_yahoo_calls)
    if yahoo_budget < 0:
        yahoo_budget = int(cfg_get(config, "earnings_dates.yahoo.max_calls_per_run", 400))
    yahoo_pause = float(cfg_get(config, "earnings_dates.yahoo.request_pause_sec", 1.0))

    gemini_enabled = bool(cfg_get(config, "earnings_dates.gemini.enabled", True)) and not args.disable_gemini
    gemini_budget = int(args.max_gemini_calls)
    if gemini_budget < 0:
        gemini_budget = int(cfg_get(config, "earnings_dates.gemini.max_calls_per_run", 15))
    gemini_pause = float(cfg_get(config, "earnings_dates.gemini.request_pause_sec", 6.5))
    gemini_model = str(cfg_get(config, "earnings_dates.gemini.model", "gemini-2.5-flash"))
    gemini_key = env_key(list(cfg_get(config, "earnings_dates.gemini.api_key_env", ["GEMINI_API_KEY"])))
    if gemini_enabled and not gemini_key:
        gemini_enabled = False
        LOGGER.warning("Gemini fallback disabled: no API key in environment")

    history_path = paths.output_dir / HISTORY_RELATIVE_PATH
    history_rows = load_history(history_path)
    prior_dates = latest_prior_dates(history_rows)

    # Investable names first so fallback budgets are spent where it matters.
    ordered = sorted(universe.values(), key=lambda r: (r["investable_eligible"] != "1", r["ticker"]))
    rows: list[dict[str, Any]] = []
    yahoo_calls = gemini_calls = 0

    for entry in ordered:
        ticker = entry["ticker"]
        query = active_query_symbol(ticker, aliases, run_as_of)
        variants = symbol_variants(query)
        errors: list[str] = []
        if av_error:
            errors.append(f"alpha_vantage:{av_error}")

        next_date: str | None = None
        source = "none"
        matched_symbol = variants[0]
        fiscal_end = ""
        av_estimate = ""
        confidence = ""
        source_urls = ""

        bulk_row, matched = next_from_bulk(calendar, variants, floor)
        if bulk_row is not None:
            next_date = bulk_row["report_date"]
            fiscal_end = bulk_row["fiscal_date_ending"]
            av_estimate = bulk_row["estimate"]
            source = "alpha_vantage_bulk"
            matched_symbol = matched
        elif not av_error:
            errors.append("alpha_vantage:not_in_bulk_calendar")

        if next_date is None and yahoo_enabled:
            if yahoo_calls >= yahoo_budget:
                errors.append("yahoo:call_budget_exhausted")
            else:
                yahoo_calls += 1
                found, yahoo_err = fetch_next_earnings_yahoo(variants[0], floor=floor, cache_dir=paths.cache_dir)
                if found:
                    next_date = found
                    source = "yahoo_finance"
                elif yahoo_err:
                    errors.append(f"yahoo:{yahoo_err}")
                if yahoo_pause > 0:
                    time.sleep(yahoo_pause)

        if next_date is None and gemini_enabled:
            if gemini_calls >= gemini_budget:
                errors.append("gemini:call_budget_exhausted")
            else:
                gemini_calls += 1
                found, conf, gem_err, urls = fetch_next_earnings_gemini(
                    variants[0],
                    floor=floor,
                    model=gemini_model,
                    api_key=str(gemini_key),
                    timeout_sec=60.0,
                )
                if found:
                    next_date = found
                    source = "gemini_search_grounded"
                confidence = conf or ""
                if gem_err:
                    errors.append(f"gemini:{gem_err}")
                if urls:
                    source_urls = " | ".join(urls)
                if gemini_pause > 0:
                    time.sleep(gemini_pause)

        prior = prior_dates.get(ticker, "")
        rows.append(
            {
                "run_as_of_date": run_as_of,
                "fetched_at_utc": fetched_at,
                "ticker": ticker,
                "query_symbol": matched_symbol,
                "investable_eligible": entry["investable_eligible"],
                "source_pipeline": entry["source_pipeline"],
                "sector": entry["sector"],
                "next_earnings_date": next_date or "",
                "days_until": (date.fromisoformat(next_date) - floor).days if next_date else "",
                "fiscal_date_ending": fiscal_end,
                "av_eps_estimate": av_estimate,
                "source": source,
                "confidence": confidence,
                "source_urls": source_urls,
                "prior_next_earnings_date": prior,
                "date_changed_flag": "1" if (prior and next_date and prior != next_date) else "0",
                "error_detail": "; ".join(errors),
            }
        )

    rows.sort(key=lambda r: (r["investable_eligible"] != "1", str(r["next_earnings_date"]) or "9999-99-99", r["ticker"]))
    write_csv(artifact_path, EARNINGS_CALENDAR_FIELDS, rows)
    appended = append_history(history_path, history_rows, rows)

    investable_rows = [r for r in rows if r["investable_eligible"] == "1"]
    dated_investable = [r for r in investable_rows if r["next_earnings_date"]]
    source_counts: dict[str, int] = {}
    for row in rows:
        source_counts[str(row["source"])] = source_counts.get(str(row["source"]), 0) + 1
    changed = sum(1 for r in rows if r["date_changed_flag"] == "1")

    meta = {
        "stage": "earnings_dates_sync",
        "run_as_of": run_as_of,
        "generated_at": fetched_at,
        "acceptance": "PASS",
        "advisory_only": True,
        "universe_size": len(rows),
        "investable_count": len(investable_rows),
        "investable_with_date": len(dated_investable),
        "investable_coverage_fraction": round(len(dated_investable) / len(investable_rows), 4) if investable_rows else 0.0,
        "source_counts": dict(sorted(source_counts.items())),
        "dates_changed_vs_prior": changed,
        "yahoo_calls": yahoo_calls,
        "gemini_calls": gemini_calls,
        "alpha_vantage_error": av_error or "",
        "history_rows_total": appended,
        "ledger_as_of": ledger_run.name if ledger_run is not None else "",
        "ib_stock_holding_count": holding_count,
        "input_paths": {
            "stocks_scores": str(scores_path),
            **(
                {
                    "broker_net_stock_positions": str(ledger_path),
                    "ledger_manifest": str(ledger_manifest_path),
                }
                if ledger_path is not None and ledger_manifest_path is not None
                else {}
            ),
        },
        "inputs_sha256": {
            "stocks_scores.csv": sha256_file(scores_path),
            **(
                {
                    "broker_net_stock_positions.csv": sha256_file(ledger_path),
                    "ledger_manifest.json": sha256_file(ledger_manifest_path),
                }
                if ledger_path is not None and ledger_manifest_path is not None
                else {}
            ),
            **({"alpha_vantage_bulk_cache": sha256_file(av_cache_path)} if av_cache_path.exists() else {}),
        },
        "outputs_sha256": {RUN_ARTIFACT_NAME: sha256_file(artifact_path)},
        "source_sha256": source_hashes(PACKAGE_ROOT, SOURCE_FILES),
    }
    write_manifest(manifest_path, meta)

    LOGGER.info(
        "EARNINGS DATES: PASS (run=%s, tickers=%d, investable_coverage=%.1f%%, sources=%s, yahoo=%d, gemini=%d)",
        run_as_of,
        len(rows),
        100.0 * (len(dated_investable) / len(investable_rows)) if investable_rows else 0.0,
        dict(sorted(source_counts.items())),
        yahoo_calls,
        gemini_calls,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
