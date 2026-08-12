"""Audit Norgate point-in-time membership access for Consumer Defensive.

The audit has two scopes:

1. Every active and curated delisted Consumer Defensive security is checked for
   symbol resolution, permanent identifier access, raw and total-return price
   alignment, major-exchange history, and daily membership history for every
   approved index.
2. With ``--full-watchlists``, every symbol in each approved Norgate
   ``Current & Past`` watchlist is queried over the configured historical
   window. This catches entitlement, corrupt-series, non-binary membership,
   duplicate-date, and unsorted-date failures across the complete provider
   catalog used by the universe policy.

This is a read-only provider audit. It does not load the production database.
"""

from __future__ import annotations

# ruff: noqa: E402

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.norgate_membership import Candidate as MembershipCandidate, resolve_candidate
from consumer_defensive.core.atomic_io import atomic_text_writer, atomic_write_text
from consumer_defensive.core.norgate_runtime import (
    NORGATE_MEMBERSHIP_DATABASES,
    norgate_database_fingerprint,
    require_norgate_snapshot,
)
from consumer_defensive.core.script_runtime import iso_date, require_date_window
from consumer_defensive.core.universe import load_policy


DEFAULT_POLICY = REPO_ROOT / "consumer_defensive" / "data" / "consumer_defensive_universe_policy.yaml"
_POLICY = load_policy(DEFAULT_POLICY)
DEFAULT_CURRENT = _POLICY.resolve("authoritative_current_csv")
DEFAULT_DELISTED = _POLICY.resolve("delisted_seed_csv")
DEFAULT_OUTPUT = REPO_ROOT / "output" / "consumer_defensive" / "preflight" / "norgate_access"
DEFAULT_START = str(_POLICY.payload["history_start"])

APPROVED_INDICES = {
    str(row["vehicle_id"]): {
        "index_name": str(row["norgate_index_name"]),
        "watchlist": str(row["norgate_watchlist_name"]),
    }
    for row in _POLICY.payload["approved_membership_vehicles"]
}


@dataclass(frozen=True)
class Candidate:
    source_set: str
    input_ticker: str
    company_name: str
    cohort: str
    exit_year: str = ""
    explicit_price_symbol: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--start", type=iso_date, default=None)
    parser.add_argument("--end", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--current-csv", type=Path, default=None)
    parser.add_argument("--delisted-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--full-watchlists",
        action="store_true",
        help="Query every symbol in all four Current & Past watchlists.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Print progress after this many full-watchlist queries.",
    )
    return parser.parse_args()


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_candidates(current_path: Path, delisted_path: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    for row in load_csv(current_path):
        candidates.append(
            Candidate(
                source_set="current",
                input_ticker=(row.get("ticker") or "").strip().upper(),
                company_name=(row.get("company_name") or "").strip(),
                cohort=(row.get("industry") or "").strip(),
            )
        )
    for row in load_csv(delisted_path):
        if (row.get("include_in_historical_universe") or "").strip() not in {"1", "true", "TRUE"}:
            continue
        candidates.append(
            Candidate(
                source_set="delisted",
                input_ticker=(row.get("historical_ticker") or "").strip().upper(),
                company_name=(row.get("company_name") or "").strip(),
                cohort=(row.get("cohort") or row.get("industry") or "").strip(),
                exit_year=(row.get("exit_year") or "").strip(),
                explicit_price_symbol=(row.get("price_source_symbol") or "").strip().upper(),
            )
        )
    return candidates


def resolve_symbol(
    provider: Any,
    candidate: Candidate,
    active_symbols: set[str],
    delisted_symbols: set[str],
) -> tuple[str, str, list[str]]:
    resolved = resolve_candidate(
        provider,
        MembershipCandidate(
            ticker=candidate.input_ticker,
            company_name=candidate.company_name,
            cohort_id="",
            cohort_name=candidate.cohort,
            source_set=candidate.source_set,
            exchange="",
            listing_country="",
            currency="USD",
            security_type="Common Stock",
            explicit_price_symbol=candidate.explicit_price_symbol,
            exit_year=candidate.exit_year,
        ),
        active_symbols,
        delisted_symbols,
    )
    return resolved.symbol, resolved.method, list(resolved.alternatives)


def to_iso(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception:
        return str(value)


def frame_dates(frame: pd.DataFrame) -> pd.DatetimeIndex:
    if frame is None or frame.empty:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None)


def date_integrity(frame: pd.DataFrame) -> tuple[bool, bool]:
    dates = frame_dates(frame)
    return bool(dates.is_monotonic_increasing), bool(not dates.has_duplicates)


def binary_values(frame: pd.DataFrame) -> tuple[bool, set[float]]:
    if frame is None or frame.empty:
        return True, set()
    numeric = pd.to_numeric(frame.iloc[:, 0], errors="coerce")
    distinct = set(numeric.dropna().astype(float).tolist())
    return not numeric.isna().any() and distinct.issubset({0.0, 1.0}), distinct


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not materialized:
        atomic_write_text(path, "", encoding="utf-8")
        return
    columns: list[str] = []
    seen: set[str] = set()
    for row in materialized:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    with atomic_text_writer(path, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)


def overall_access_status(
    *,
    candidate_failures: int,
    asset_collisions: int,
    membership_failures: int,
    candidate_nonmembers: int,
    recognized_membership_required: bool,
    full_watchlists_scanned: bool,
    full_watchlist_failures: int,
) -> str:
    """Return the fail-closed preflight status from independently testable counts."""
    passed = (
        candidate_failures == 0
        and asset_collisions == 0
        and membership_failures == 0
        and (not recognized_membership_required or candidate_nonmembers == 0)
        and (not full_watchlists_scanned or full_watchlist_failures == 0)
    )
    return "PASS" if passed else "FAIL"


def audit_candidate(
    provider: Any,
    candidate: Candidate,
    symbol: str,
    resolution: str,
    alternatives: list[str],
    start: str,
    end: str,
    watchlist_sets: dict[str, set[str]],
    *,
    major_exchange_required: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row: dict[str, Any] = {
        **asdict(candidate),
        "norgate_symbol": symbol,
        "resolution_method": resolution,
        "resolution_alternatives": ";".join(alternatives),
        "access_status": "FAIL" if not symbol else "PASS",
        "issue": "unresolved_symbol" if not symbol else "",
    }
    membership_rows: list[dict[str, Any]] = []
    if not symbol:
        return row, membership_rows

    try:
        row["assetid"] = provider.assetid(symbol)
        row["provider_security_name"] = str(provider.security_name(symbol) or "")
        row["first_quoted_date"] = to_iso(provider.first_quoted_date(symbol))
        row["last_quoted_date"] = to_iso(provider.last_quoted_date(symbol))
    except Exception as exc:
        row["access_status"] = "FAIL"
        row["issue"] = f"metadata_error:{type(exc).__name__}:{exc}"
        return row, membership_rows

    first = row["first_quoted_date"] or start
    last = row["last_quoted_date"] or end
    effective_start = max(start, first)
    effective_end = min(end, last)
    row["audit_start"] = effective_start
    row["audit_end"] = effective_end
    if effective_start > effective_end:
        row["access_status"] = "OUT_OF_WINDOW"
        row["issue"] = "no_price_overlap_with_required_window"
        return row, membership_rows

    try:
        raw = provider.price_timeseries(
            symbol,
            stock_price_adjustment_setting=provider.StockPriceAdjustmentType.NONE,
            start_date=effective_start,
            end_date=effective_end,
            timeseriesformat="pandas-dataframe",
        )
        total_return = provider.price_timeseries(
            symbol,
            stock_price_adjustment_setting=provider.StockPriceAdjustmentType.TOTALRETURN,
            start_date=effective_start,
            end_date=effective_end,
            timeseriesformat="pandas-dataframe",
        )
        listed = provider.major_exchange_listed_timeseries(
            symbol,
            start_date=effective_start,
            end_date=effective_end,
            timeseriesformat="pandas-dataframe",
        )
        raw_dates = frame_dates(raw)
        total_return_dates = frame_dates(total_return)
        listed_dates = frame_dates(listed)
        row["raw_price_rows"] = len(raw_dates)
        row["total_return_rows"] = len(total_return_dates)
        row["major_exchange_rows"] = len(listed_dates)
        row["price_first_date"] = to_iso(raw_dates.min()) if len(raw_dates) else ""
        row["price_last_date"] = to_iso(raw_dates.max()) if len(raw_dates) else ""
        row["raw_total_return_dates_match"] = int(raw_dates.equals(total_return_dates))
        row["raw_major_exchange_dates_match"] = int(raw_dates.equals(listed_dates))
        sorted_dates, unique_dates = date_integrity(raw)
        row["price_dates_sorted"] = int(sorted_dates)
        row["price_dates_unique"] = int(unique_dates)
        listed_binary, listed_values = binary_values(listed)
        listed_numeric = pd.to_numeric(listed.iloc[:, 0], errors="coerce")
        listed_days = int((listed_numeric == 1).sum())
        row["major_exchange_binary"] = int(listed_binary)
        row["major_exchange_values"] = ";".join(map(str, sorted(listed_values)))
        row["major_exchange_listed_days"] = listed_days
        row["major_exchange_listed_in_window"] = int(listed_days > 0)
        if (
            len(raw_dates) == 0
            or not raw_dates.equals(total_return_dates)
            or not raw_dates.equals(listed_dates)
            or not sorted_dates
            or not unique_dates
            or not listed_binary
            or (major_exchange_required and listed_days == 0)
        ):
            row["access_status"] = "FAIL"
            row["issue"] = (
                "major_exchange_requirement_not_met"
                if major_exchange_required and listed_days == 0
                else "price_or_listing_series_alignment_failure"
            )
    except Exception as exc:
        row["access_status"] = "FAIL"
        row["issue"] = f"price_or_listing_error:{type(exc).__name__}:{exc}"
        return row, membership_rows

    any_member_dates: set[pd.Timestamp] = set()
    for index_id, config in APPROVED_INDICES.items():
        try:
            membership = provider.index_constituent_timeseries(
                symbol,
                config["index_name"],
                start_date=effective_start,
                end_date=effective_end,
                timeseriesformat="pandas-dataframe",
            )
            membership_dates = frame_dates(membership)
            binary, values = binary_values(membership)
            sorted_dates, unique_dates = date_integrity(membership)
            member_mask = (
                pd.to_numeric(membership.iloc[:, 0], errors="coerce") == 1
                if membership is not None and not membership.empty
                else pd.Series(dtype=bool)
            )
            member_dates = set(membership_dates[member_mask.to_numpy()]) if len(member_mask) else set()
            any_member_dates.update(member_dates)
            membership_row = {
                "source_set": candidate.source_set,
                "input_ticker": candidate.input_ticker,
                "norgate_symbol": symbol,
                "assetid": row.get("assetid", ""),
                "index_id": index_id,
                "index_name": config["index_name"],
                "in_current_past_watchlist": int(symbol in watchlist_sets[index_id]),
                "series_rows": len(membership_dates),
                "price_dates_match": int(membership_dates.equals(raw_dates)),
                "binary_values": int(binary),
                "observed_values": ";".join(map(str, sorted(values))),
                "dates_sorted": int(sorted_dates),
                "dates_unique": int(unique_dates),
                "member_days": len(member_dates),
                "first_member_date": to_iso(min(member_dates)) if member_dates else "",
                "last_member_date": to_iso(max(member_dates)) if member_dates else "",
                "status": "PASS",
                "issue": "",
            }
            if (
                not membership_dates.equals(raw_dates)
                or not binary
                or not sorted_dates
                or not unique_dates
            ):
                membership_row["status"] = "FAIL"
                membership_row["issue"] = "membership_series_alignment_or_integrity_failure"
                row["access_status"] = "FAIL"
                row["issue"] = "one_or_more_membership_series_failed"
            membership_rows.append(membership_row)
        except Exception as exc:
            membership_rows.append(
                {
                    "source_set": candidate.source_set,
                    "input_ticker": candidate.input_ticker,
                    "norgate_symbol": symbol,
                    "assetid": row.get("assetid", ""),
                    "index_id": index_id,
                    "index_name": config["index_name"],
                    "status": "FAIL",
                    "issue": f"{type(exc).__name__}:{exc}",
                }
            )
            row["access_status"] = "FAIL"
            row["issue"] = "one_or_more_membership_series_errored"

    row["approved_index_member_days"] = len(any_member_dates)
    row["approved_index_member_in_window"] = int(bool(any_member_dates))
    return row, membership_rows


def audit_full_watchlists(
    provider: Any,
    watchlist_symbols: dict[str, list[str]],
    active_symbols: set[str],
    delisted_symbols: set[str],
    start: str,
    end: str,
    progress_every: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index_id, config in APPROVED_INDICES.items():
        symbols = watchlist_symbols[index_id]
        started = time.perf_counter()
        counts: Counter[str] = Counter()
        for position, symbol in enumerate(symbols, start=1):
            counts["queries"] += 1
            if symbol in active_symbols:
                counts["active_symbols"] += 1
            elif symbol in delisted_symbols:
                counts["delisted_symbols"] += 1
            else:
                counts["catalog_unresolved"] += 1
                failures.append(
                    {
                        "index_id": index_id,
                        "index_name": config["index_name"],
                        "symbol": symbol,
                        "failure": "watchlist_symbol_absent_from_active_and_delisted_databases",
                    }
                )
            try:
                frame = provider.index_constituent_timeseries(
                    symbol,
                    config["index_name"],
                    start_date=start,
                    end_date=end,
                    timeseriesformat="pandas-dataframe",
                )
                if frame is None or frame.empty:
                    counts["empty_in_window"] += 1
                else:
                    counts["nonempty_in_window"] += 1
                    counts["series_rows"] += len(frame)
                    binary, values = binary_values(frame)
                    sorted_dates, unique_dates = date_integrity(frame)
                    if not binary or not sorted_dates or not unique_dates:
                        counts["integrity_failures"] += 1
                        failures.append(
                            {
                                "index_id": index_id,
                                "index_name": config["index_name"],
                                "symbol": symbol,
                                "failure": "nonbinary_unsorted_or_duplicate_membership_series",
                                "values": ";".join(map(str, sorted(values))),
                                "dates_sorted": int(sorted_dates),
                                "dates_unique": int(unique_dates),
                            }
                        )
                    member_values = pd.to_numeric(frame.iloc[:, 0], errors="coerce").fillna(0)
                    if (member_values == 1).any():
                        counts["member_in_window"] += 1
                    else:
                        counts["not_member_in_window"] += 1
            except Exception as exc:
                counts["query_errors"] += 1
                failures.append(
                    {
                        "index_id": index_id,
                        "index_name": config["index_name"],
                        "symbol": symbol,
                        "failure": f"query_error:{type(exc).__name__}:{exc}",
                    }
                )
            if progress_every > 0 and position % progress_every == 0:
                print(
                    f"{config['index_name']}: {position:,}/{len(symbols):,} queried; "
                    f"errors={counts['query_errors']:,}; integrity={counts['integrity_failures']:,}",
                    flush=True,
                )
        summaries.append(
            {
                "index_id": index_id,
                "index_name": config["index_name"],
                "watchlist": config["watchlist"],
                "start": start,
                "end": end,
                **dict(counts),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "status": (
                    "PASS"
                    if counts["query_errors"] == 0
                    and counts["integrity_failures"] == 0
                    and counts["catalog_unresolved"] == 0
                    else "FAIL"
                ),
            }
        )
    return summaries, failures


def main() -> int:
    global APPROVED_INDICES
    args = parse_args()
    policy = load_policy(args.policy)
    args.start = args.start or str(policy.payload["history_start"])
    args.current_csv = (args.current_csv or policy.resolve("authoritative_current_csv")).expanduser().resolve()
    args.delisted_csv = (args.delisted_csv or policy.resolve("delisted_seed_csv")).expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    require_date_window(args.start, args.end)
    if args.progress_every <= 0:
        raise ValueError("--progress-every must be a positive integer.")
    APPROVED_INDICES = {
        str(row["vehicle_id"]): {
            "index_name": str(row["norgate_index_name"]),
            "watchlist": str(row["norgate_watchlist_name"]),
        }
        for row in policy.payload["approved_membership_vehicles"]
    }
    try:
        import norgatedata as provider
    except ImportError as exc:
        raise SystemExit("norgatedata is not installed in this Python environment") from exc

    run_started = datetime.now(timezone.utc)
    candidates = load_candidates(args.current_csv, args.delisted_csv)
    provider_fingerprint = norgate_database_fingerprint(
        provider,
        NORGATE_MEMBERSHIP_DATABASES,
    )
    active_symbols = set(provider.database_symbols("US Equities"))
    delisted_symbols = set(provider.database_symbols("US Equities Delisted"))
    watchlist_symbols = {
        index_id: list(provider.watchlist_symbols(config["watchlist"]))
        for index_id, config in APPROVED_INDICES.items()
    }
    watchlist_sets = {key: set(value) for key, value in watchlist_symbols.items()}
    provider_fingerprint_after_catalog = require_norgate_snapshot(
        provider,
        provider_fingerprint,
        context="while reading preflight catalogs and watchlists",
    )

    print(
        f"Auditing {len(candidates)} Consumer Defensive candidates from {args.start} to {args.end}",
        flush=True,
    )

    candidate_rows: list[dict[str, Any]] = []
    membership_rows: list[dict[str, Any]] = []
    for position, candidate in enumerate(candidates, start=1):
        symbol, resolution, alternatives = resolve_symbol(
            provider, candidate, active_symbols, delisted_symbols
        )
        row, memberships = audit_candidate(
            provider,
            candidate,
            symbol,
            resolution,
            alternatives,
            args.start,
            args.end,
            watchlist_sets,
            major_exchange_required=bool(policy.payload["major_exchange_required"]),
        )
        candidate_rows.append(row)
        membership_rows.extend(memberships)
        require_norgate_snapshot(
            provider,
            provider_fingerprint,
            context="during candidate history preflight",
        )
        if position % 25 == 0 or position == len(candidates):
            print(f"Candidate histories: {position}/{len(candidates)}", flush=True)

    asset_candidates: dict[str, list[str]] = defaultdict(list)
    for row in candidate_rows:
        assetid = str(row.get("assetid") or "")
        if assetid:
            asset_candidates[assetid].append(
                f"{row.get('source_set')}:{row.get('input_ticker')}:{row.get('norgate_symbol')}"
            )
    asset_collisions = [
        {"assetid": assetid, "candidates": ";".join(values), "count": len(values)}
        for assetid, values in asset_candidates.items()
        if len(values) > 1
    ]

    full_summaries: list[dict[str, Any]] = []
    full_failures: list[dict[str, Any]] = []
    if args.full_watchlists:
        print("Starting full Current & Past watchlist membership scan", flush=True)
        full_summaries, full_failures = audit_full_watchlists(
            provider,
            watchlist_symbols,
            active_symbols,
            delisted_symbols,
            args.start,
            args.end,
            args.progress_every,
        )
        require_norgate_snapshot(
            provider,
            provider_fingerprint,
            context="during full-watchlist preflight",
        )

    provider_fingerprint_end = require_norgate_snapshot(
        provider,
        provider_fingerprint,
        context="before preflight artifact publication",
    )

    candidate_counts = Counter(str(row.get("access_status") or "") for row in candidate_rows)
    unresolved = [row for row in candidate_rows if not row.get("norgate_symbol")]
    relevant_rows = [row for row in candidate_rows if row.get("access_status") != "OUT_OF_WINDOW"]
    nonmembers = [
        row
        for row in relevant_rows
        if row.get("access_status") == "PASS"
        and int(row.get("approved_index_member_in_window") or 0) == 0
    ]
    summary = {
        "audit": "consumer_defensive_norgate_history_access",
        "run_started_utc": run_started.isoformat(),
        "run_finished_utc": datetime.now(timezone.utc).isoformat(),
        "norgatedata_version": getattr(provider, "__version__", "unknown"),
        "required_start": args.start,
        "required_end": args.end,
        "database_updates": provider_fingerprint,
        "database_updates_after_catalog": provider_fingerprint_after_catalog,
        "database_updates_end": provider_fingerprint_end,
        "database_symbol_counts": {
            "US Equities": len(active_symbols),
            "US Equities Delisted": len(delisted_symbols),
        },
        "watchlist_counts": {
            index_id: len(symbols) for index_id, symbols in watchlist_symbols.items()
        },
        "watchlist_union_count": len(set().union(*watchlist_sets.values())),
        "candidate_count": len(candidate_rows),
        "candidate_status_counts": dict(candidate_counts),
        "candidate_unresolved_count": len(unresolved),
        "candidate_asset_collision_count": len(asset_collisions),
        "candidate_nonmember_count": len(nonmembers),
        "recognized_membership_required": bool(policy.payload["recognized_membership_required"]),
        "membership_series_count": len(membership_rows),
        "membership_series_failure_count": sum(
            1 for row in membership_rows if row.get("status") == "FAIL"
        ),
        "full_watchlists_scanned": bool(args.full_watchlists),
        "full_watchlist_summaries": full_summaries,
        "full_watchlist_failure_count": len(full_failures),
    }
    summary["overall_access_status"] = overall_access_status(
        candidate_failures=candidate_counts["FAIL"],
        asset_collisions=len(asset_collisions),
        membership_failures=summary["membership_series_failure_count"],
        candidate_nonmembers=len(nonmembers),
        recognized_membership_required=bool(policy.payload["recognized_membership_required"]),
        full_watchlists_scanned=bool(args.full_watchlists),
        full_watchlist_failures=len(full_failures),
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "candidate_history_audit.csv", candidate_rows)
    write_csv(args.output_dir / "candidate_membership_audit.csv", membership_rows)
    write_csv(args.output_dir / "candidate_asset_collisions.csv", asset_collisions)
    write_csv(args.output_dir / "full_watchlist_summary.csv", full_summaries)
    write_csv(args.output_dir / "full_watchlist_failures.csv", full_failures)
    summary_path = args.output_dir / "summary.json"
    atomic_write_text(
        summary_path,
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return 0 if summary["overall_access_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
