#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import requests


LOGGER = logging.getLogger("google_screener")
PROMPT_VERSION = "google_confirmation_v1"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_COLUMNS = [
    "ticker",
    "company_name",
    "classification",
    "status",
    "confirmed",
    "confidence",
    "company_name_match",
    "event_count",
    "ratio_date",
    "evidence_url",
    "evidence_title",
    "notes",
    "checked_at_utc",
    "model",
    "prompt_version",
    "source",
    "error",
]


@dataclass(frozen=True)
class GoogleScreenerConfig:
    enabled: bool
    api_key_env: str
    model: str
    fallback_model: str
    use_search_grounding: bool
    batch_size: int
    max_calls_per_run: int
    min_seconds_between_calls: float
    cache_file: Path
    raw_output_file: Path
    cache_ttl_days: float
    min_confidence_for_confirmed: str
    require_company_name_match: bool
    require_primary_source: bool
    rerun_missing_tickers: bool
    max_missing_rerun_calls: int
    as_of_date: str
    lookback_years: float


class GoogleBatchError(RuntimeError):
    def __init__(self, message: str, *, calls_made: int = 0) -> None:
        super().__init__(message)
        self.calls_made = calls_made


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def normalize_status(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in {"true", "confirmed", "yes"}:
        return "confirmed"
    if text in {"false", "not_confirmed", "no", "none"}:
        return "not_confirmed"
    if text in {"unknown", "null", "possible"}:
        return "possible"
    if text in {"failed", "error"}:
        return "failed"
    if text in {"skipped"}:
        return "skipped"
    return text or "possible"


def confidence_rank(value: Any) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(str(value or "").strip().lower(), 0)


def boolish(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def redact_google_key(text: Any) -> str:
    return re.sub(r"([?&]key=)[^&\s]+", r"\1<redacted>", str(text or ""))


NON_PRIMARY_SOURCE_HINTS = (
    "benzinga.",
    "marketbeat.",
    "marketscreener.",
    "seekingalpha.",
    "stocktitan.",
    "streetinsider.",
    "youtube.",
)
GOING_CONCERN_PRIMARY_FORM_PATTERN = re.compile(r"\b(?:10-K|10-Q|20-F|40-F)\b", re.IGNORECASE)


def has_primary_source_evidence(*, classification: str, evidence_url: Any, evidence_title: Any, notes: Any) -> bool:
    url = str(evidence_url or "").strip().lower()
    title = str(evidence_title or "").strip().lower()
    note_text = str(notes or "").strip().lower()
    combined = " ".join([url, title, note_text])
    if not combined.strip():
        return False
    if any(hint in url or hint in title for hint in NON_PRIMARY_SOURCE_HINTS):
        return False
    if classification == "going_concern":
        return ("sec.gov" in combined or "archives/edgar" in combined) and bool(
            GOING_CONCERN_PRIMARY_FORM_PATTERN.search(combined)
        )
    if classification == "reverse_split":
        return any(
            hint in combined
            for hint in (
                "sec.gov",
                "archives/edgar",
                "nasdaqtrader.com",
                "nasdaq.com",
                "investors.",
                "investor.",
                "/investor-relations/",
                "/news-releases/",
            )
        )
    return False


def chunks(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    size = max(1, int(size))
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def empty_result(
    *,
    ticker: str,
    company_name: str,
    classification: str,
    status: str,
    model: str,
    as_of_date: str,
    source: str,
    error: str = "",
) -> dict[str, Any]:
    return {
        "ticker": normalize_ticker(ticker),
        "company_name": str(company_name or "").strip(),
        "classification": str(classification or "").strip(),
        "status": status,
        "confirmed": False,
        "confidence": "",
        "company_name_match": False,
        "event_count": 0,
        "ratio_date": "",
        "evidence_url": "",
        "evidence_title": "",
        "notes": "",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "source": source,
        "error": error,
    }


def read_cache(cache_file: Path) -> pd.DataFrame:
    if not cache_file.exists():
        return pd.DataFrame(columns=DEFAULT_COLUMNS)
    try:
        df = pd.read_csv(cache_file, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=DEFAULT_COLUMNS)
    for col in DEFAULT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[DEFAULT_COLUMNS].copy()


def write_cache(cache_file: Path, cache_df: pd.DataFrame) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_df = cache_df.copy()
    for col in DEFAULT_COLUMNS:
        if col not in cache_df.columns:
            cache_df[col] = ""
    cache_df = cache_df[DEFAULT_COLUMNS]
    cache_df["ticker_norm"] = cache_df["ticker"].map(normalize_ticker)
    cache_df["classification_norm"] = cache_df["classification"].astype(str).str.strip().str.lower()
    cache_df["prompt_version_norm"] = cache_df["prompt_version"].astype(str)
    cache_df = (
        cache_df.sort_values("checked_at_utc")
        .drop_duplicates(["ticker_norm", "classification_norm", "prompt_version_norm"], keep="last")
        .drop(columns=["ticker_norm", "classification_norm", "prompt_version_norm"])
    )
    cache_df.to_csv(cache_file, index=False)


def is_cache_fresh(row: pd.Series, ttl_days: float, model: str) -> bool:
    if str(row.get("model", "")).strip() != model:
        return False
    if str(row.get("prompt_version", "")).strip() != PROMPT_VERSION:
        return False
    checked_at = str(row.get("checked_at_utc", "")).strip()
    if not checked_at:
        return False
    try:
        parsed = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed <= timedelta(days=float(ttl_days))


def cached_result(
    cache_df: pd.DataFrame,
    *,
    ticker: str,
    classification: str,
    config: GoogleScreenerConfig,
) -> Optional[dict[str, Any]]:
    if cache_df.empty:
        return None
    ticker_norm = normalize_ticker(ticker)
    classification_norm = str(classification or "").strip().lower()
    mask = (
        cache_df["ticker"].map(normalize_ticker).eq(ticker_norm)
        & cache_df["classification"].astype(str).str.strip().str.lower().eq(classification_norm)
    )
    rows = cache_df.loc[mask].copy()
    if rows.empty:
        return None
    rows = rows.sort_values("checked_at_utc")
    row = rows.iloc[-1]
    if not is_cache_fresh(row, config.cache_ttl_days, config.model):
        return None
    out = row.to_dict()
    out["source"] = "cache"
    out["confirmed"] = boolish(out.get("confirmed"))
    out["company_name_match"] = boolish(out.get("company_name_match"))
    return out


def parse_json_array(text: str) -> list[Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, flags=re.I | re.S)
    if fence:
        return json.loads(fence.group(1))
    start = text.find("[")
    if start < 0:
        raise ValueError("No JSON array found in Google response")
    parsed, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(parsed, list):
        raise ValueError("Google response JSON root is not an array")
    return parsed


def build_prompt(classification: str, batch: list[dict[str, Any]], config: GoogleScreenerConfig) -> str:
    payload = [
        {
            "ticker": normalize_ticker(row.get("ticker")),
            "company_name": str(row.get("company_name") or "").strip(),
        }
        for row in batch
    ]
    if classification == "reverse_split":
        task = (
            "For each ticker/company pair, determine whether that exact company completed "
            f"a reverse stock split within the last {config.lookback_years:g} years."
        )
        event_rules = (
            "Do not count proposed, authorized, approved, planned, or potential reverse splits. "
            "Count only completed/effective reverse splits for the same company, not a previous "
            "or different issuer that reused the ticker."
        )
        required_detail = "ratio_date should include split ratio and effective date when confirmed."
    elif classification == "going_concern":
        task = (
            "For each ticker/company pair, determine whether the exact company currently has an "
            "unresolved going-concern warning or explicit substantial-doubt disclosure in its latest "
            "10-K, 10-Q, 20-F, or 40-F filing."
        )
        event_rules = (
            "Do not confirm if the filing only describes generic accounting policy, risk-factor "
            "boilerplate, historical concerns that were resolved, or liquidity language saying cash "
            "is sufficient for at least 12 months."
        )
        required_detail = "ratio_date should be null; use notes for the filing date/form and finding."
    else:
        raise ValueError(f"Unsupported Google classification: {classification}")
    return f"""
As of {config.as_of_date}, use Google Search grounding to review this list:
{json.dumps(payload, ensure_ascii=True)}

Task: {task}
Rules: {event_rules}
Prefer primary sources: Nasdaq Trader corporate action notices, company press releases, SEC filings, exchange notices, or company investor-relations pages.
The ticker and company_name must match the evidence issuer. If the evidence is for a different company or old ticker user, return company_name_match=false and confirmed=false.
{required_detail}

Return ONLY one JSON array with exactly one object per input row, in the same order if possible. Do not skip any ticker.
Each object must contain these keys:
- ticker: string
- classification: "{classification}"
- confirmed: boolean or null if evidence is insufficient
- confidence: "high", "medium", or "low"
- company_name_match: boolean
- event_count: integer count of completed/current confirmed events or 0
- ratio_date: string or null
- evidence_url: string or null
- evidence_title: string or null
- notes: short string

No markdown and no text outside the JSON array.
""".strip()


def normalize_google_item(
    item: dict[str, Any],
    *,
    fallback: dict[str, Any],
    classification: str,
    config: GoogleScreenerConfig,
) -> dict[str, Any]:
    ticker = normalize_ticker(item.get("ticker") or fallback.get("ticker"))
    company_name = str(fallback.get("company_name") or "").strip()
    confirmed_raw = item.get("confirmed")
    confidence = str(item.get("confidence") or "").strip().lower()
    company_name_match = boolish(item.get("company_name_match"))
    event_count_raw = item.get("event_count", 0)
    try:
        event_count = int(float(str(event_count_raw or "0").strip()))
    except ValueError:
        event_count = 0
    evidence_url = str(item.get("evidence_url") or "").strip()
    evidence_title = str(item.get("evidence_title") or "").strip()
    notes = str(item.get("notes") or "").strip()
    primary_source_ok = has_primary_source_evidence(
        classification=classification,
        evidence_url=evidence_url,
        evidence_title=evidence_title,
        notes=notes,
    )
    confirmed = (
        boolish(confirmed_raw)
        and confidence_rank(confidence) >= confidence_rank(config.min_confidence_for_confirmed)
        and (company_name_match or not config.require_company_name_match)
        and event_count > 0
    )
    if confirmed:
        status = "confirmed"
    elif confirmed_raw is None or str(confirmed_raw).strip().lower() in {"", "null", "none"}:
        status = "possible"
    elif boolish(confirmed_raw):
        status = "possible"
    else:
        status = "not_confirmed"
    return {
        "ticker": ticker,
        "company_name": company_name,
        "classification": classification,
        "status": status,
        "confirmed": confirmed,
        "confidence": confidence,
        "company_name_match": company_name_match,
        "event_count": event_count,
        "ratio_date": str(item.get("ratio_date") or "").strip(),
        "evidence_url": evidence_url,
        "evidence_title": evidence_title,
        "notes": notes if primary_source_ok or not boolish(confirmed_raw) else f"{notes} [non-primary source confirmation]".strip(),
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": config.model,
        "prompt_version": PROMPT_VERSION,
        "source": "google",
        "error": "",
    }


def append_raw_response(raw_output_file: Path, payload: dict[str, Any]) -> None:
    raw_output_file.parent.mkdir(parents=True, exist_ok=True)
    records: list[Any] = []
    if raw_output_file.exists():
        try:
            records = json.loads(raw_output_file.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                records = []
        except Exception:
            records = []
    records.append(payload)
    raw_output_file.write_text(json.dumps(records, ensure_ascii=True, indent=2), encoding="utf-8")


def call_google_batch(
    *,
    session: requests.Session,
    api_key: str,
    classification: str,
    batch: list[dict[str, Any]],
    config: GoogleScreenerConfig,
) -> tuple[list[dict[str, Any]], int]:
    prompt = build_prompt(classification, batch, config)
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0},
    }
    if config.use_search_grounding:
        payload["tools"] = [{"google_search": {}}]
    models = [config.model]
    if config.fallback_model and config.fallback_model not in models:
        models.append(config.fallback_model)
    calls_made = 0
    last_error = ""
    for model in models:
        url = GEMINI_ENDPOINT.format(model=model)
        try:
            response = session.post(url, params={"key": api_key}, json=payload, timeout=180)
            calls_made += 1
            response.raise_for_status()
            data = response.json()
            text = "".join(
                part.get("text", "")
                for candidate in data.get("candidates", [])
                for part in candidate.get("content", {}).get("parts", [])
                if isinstance(part, dict)
            )
            append_raw_response(
                config.raw_output_file,
                {
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "classification": classification,
                    "tickers": [row.get("ticker") for row in batch],
                    "model": model,
                    "prompt_version": PROMPT_VERSION,
                    "response_text": text,
                    "response": data,
                },
            )
            if not text.strip():
                raise ValueError(f"empty Google response text from {model}")
            parsed = parse_json_array(text)
            by_ticker: dict[str, dict[str, Any]] = {}
            for item in parsed:
                if isinstance(item, dict):
                    by_ticker[normalize_ticker(item.get("ticker"))] = item
            results: list[dict[str, Any]] = []
            effective_config = GoogleScreenerConfig(
                **{**config.__dict__, "model": model}
            )
            for fallback in batch:
                ticker = normalize_ticker(fallback.get("ticker"))
                item = by_ticker.get(ticker)
                if item is None:
                    results.append(
                        empty_result(
                            ticker=ticker,
                            company_name=str(fallback.get("company_name") or ""),
                            classification=classification,
                            status="failed",
                            model=model,
                            as_of_date=config.as_of_date,
                            source="google",
                            error="missing_ticker_in_response",
                        )
                    )
                    continue
                results.append(normalize_google_item(item, fallback=fallback, classification=classification, config=effective_config))
            return results, calls_made
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {redact_google_key(exc)}"
            LOGGER.warning(
                "Google confirmation model failed: model=%s classification=%s err=%s",
                model,
                classification,
                redact_google_key(exc),
            )
            continue
    raise GoogleBatchError(last_error or "Google confirmation failed", calls_made=calls_made)


def confirm_candidates(
    candidates: list[dict[str, Any]],
    *,
    classifications: list[str],
    config: GoogleScreenerConfig,
) -> pd.DataFrame:
    if not config.enabled or not candidates or not classifications:
        return pd.DataFrame(columns=DEFAULT_COLUMNS)
    api_key = os.environ.get(config.api_key_env, "").strip()
    if not api_key:
        LOGGER.warning("Google confirmation skipped: environment variable %s is not set", config.api_key_env)
        rows = [
            empty_result(
                ticker=str(candidate.get("ticker") or ""),
                company_name=str(candidate.get("company_name") or ""),
                classification=classification,
                status="failed",
                model=config.model,
                as_of_date=config.as_of_date,
                source="skipped",
                error=f"missing_env:{config.api_key_env}",
            )
            for classification in classifications
            for candidate in candidates
        ]
        return pd.DataFrame(rows, columns=DEFAULT_COLUMNS)

    cache_df = read_cache(config.cache_file)
    output_rows: list[dict[str, Any]] = []
    cache_rows: list[dict[str, Any]] = []
    calls_used = 0
    last_call_at = 0.0

    with requests.Session() as session:
        for classification in classifications:
            eligible = [row for row in candidates if classification in set(row.get("classifications", []))]
            pending: list[dict[str, Any]] = []
            for row in eligible:
                cached = cached_result(
                    cache_df,
                    ticker=str(row.get("ticker") or ""),
                    classification=classification,
                    config=config,
                )
                if cached is not None:
                    output_rows.append(cached)
                else:
                    pending.append(row)
            for batch in chunks(pending, config.batch_size):
                if calls_used >= config.max_calls_per_run:
                    for row in batch:
                        output_rows.append(
                            empty_result(
                                ticker=str(row.get("ticker") or ""),
                                company_name=str(row.get("company_name") or ""),
                                classification=classification,
                                status="skipped",
                                model=config.model,
                                as_of_date=config.as_of_date,
                                source="skipped",
                                error="max_calls_per_run_exceeded",
                            )
                        )
                    continue
                elapsed = time.perf_counter() - last_call_at
                if last_call_at and elapsed < config.min_seconds_between_calls:
                    time.sleep(config.min_seconds_between_calls - elapsed)
                try:
                    batch_results, calls_made = call_google_batch(
                        session=session,
                        api_key=api_key,
                        classification=classification,
                        batch=batch,
                        config=config,
                    )
                    calls_used += calls_made
                    last_call_at = time.perf_counter()
                except Exception as exc:
                    calls_used += int(getattr(exc, "calls_made", 1) or 1)
                    LOGGER.warning("Google confirmation batch failed: classification=%s err=%s", classification, redact_google_key(exc))
                    batch_results = [
                        empty_result(
                            ticker=str(row.get("ticker") or ""),
                            company_name=str(row.get("company_name") or ""),
                            classification=classification,
                            status="failed",
                            model=config.model,
                            as_of_date=config.as_of_date,
                            source="google",
                            error=f"{type(exc).__name__}: {redact_google_key(exc)}",
                        )
                        for row in batch
                    ]
                output_rows.extend(batch_results)
                cache_rows.extend([row for row in batch_results if row.get("source") == "google" and not row.get("error")])

    if cache_rows:
        cache_df = pd.concat([cache_df, pd.DataFrame(cache_rows)], ignore_index=True)
        write_cache(config.cache_file, cache_df)
    out_df = pd.DataFrame(output_rows)
    for col in DEFAULT_COLUMNS:
        if col not in out_df.columns:
            out_df[col] = ""
    return out_df[DEFAULT_COLUMNS].copy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Confirm biotech screener risk flags with grounded Gemini search.")
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--cache-file", type=Path, required=True)
    parser.add_argument("--raw-output-file", type=Path, required=True)
    parser.add_argument("--classification", action="append", choices=("reverse_split", "going_concern"), required=True)
    parser.add_argument("--api-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--model", default="gemini-2.5-flash-lite")
    parser.add_argument("--fallback-model", default="gemini-2.5-flash")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-calls-per-run", type=int, default=30)
    parser.add_argument("--min-seconds-between-calls", type=float, default=5.0)
    parser.add_argument("--cache-ttl-days", type=float, default=30.0)
    parser.add_argument("--as-of-date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--lookback-years", type=float, default=2.0)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args()
    df = pd.read_csv(args.input_csv, dtype=str).fillna("")
    candidates: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        reason_codes = str(row.get("reason_codes") or "")
        classes: list[str] = []
        if "possible_reverse_split" in reason_codes or int(float(str(row.get("reverse_split_hits_2y") or 0))) > 0:
            classes.append("reverse_split")
        if str(row.get("going_concern_status") or "").lower() in {"confirmed", "possible"}:
            classes.append("going_concern")
        selected = [c for c in classes if c in set(args.classification)]
        if selected:
            candidates.append(
                {
                    "ticker": row.get("ticker"),
                    "company_name": row.get("company_name"),
                    "classifications": selected,
                }
            )
    config = GoogleScreenerConfig(
        enabled=True,
        api_key_env=args.api_key_env,
        model=args.model,
        fallback_model=args.fallback_model,
        use_search_grounding=True,
        batch_size=args.batch_size,
        max_calls_per_run=args.max_calls_per_run,
        min_seconds_between_calls=args.min_seconds_between_calls,
        cache_file=args.cache_file,
        raw_output_file=args.raw_output_file,
        cache_ttl_days=args.cache_ttl_days,
        min_confidence_for_confirmed="high",
        require_company_name_match=True,
        require_primary_source=True,
        rerun_missing_tickers=True,
        max_missing_rerun_calls=3,
        as_of_date=args.as_of_date,
        lookback_years=args.lookback_years,
    )
    result = confirm_candidates(candidates, classifications=list(args.classification), config=config)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output_csv, index=False)
    LOGGER.info("Wrote %s rows=%d", args.output_csv, len(result))


if __name__ == "__main__":
    main()
