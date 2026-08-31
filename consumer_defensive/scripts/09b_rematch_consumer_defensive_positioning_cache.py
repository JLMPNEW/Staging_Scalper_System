#!/usr/bin/env python3
# ruff: noqa: E402
"""Rematch Consumer identifiers against sealed neutral 13F and FINRA caches."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import zipfile
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.atomic_io import atomic_text_writer
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import iso_date
from market_positioning.api_collectors import (
    sync_finra_equity_short_interest_files,
    sync_sec_13f_data_sets,
)
from market_positioning.core import connect, init_db
from market_positioning.core import aggregate_13f_ownership_for_tickers


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
STATE_FILENAME = "consumer_defensive_positioning_rematch_state.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_content_digest(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        rows = [
            (item.filename, item.CRC, item.file_size, item.compress_size)
            for item in archive.infolist()
        ]
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _load_state(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        "consumer_defensive_positioning_rematch_state_v1"
    ):
        raise RuntimeError(f"Invalid Consumer positioning rematch state: {path}")
    return payload


def _state_cache_rows(state: dict[str, object] | None) -> list[dict[str, object]]:
    if state is None:
        return []
    value = state.get("cache_files")
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise RuntimeError("Consumer positioning rematch state has invalid cache_files")
    return [row for row in value if isinstance(row, dict)]


def _cache_inventory(
    cache_root: Path,
    prior_state: dict[str, object] | None,
) -> list[dict[str, object]]:
    prior_rows = _state_cache_rows(prior_state)
    prior = {
        str(row.get("path")): row
        for row in prior_rows
        if isinstance(row, dict) and row.get("path")
    }
    paths = sorted(
        [*(cache_root / "sec_13f").glob("*_form13f.zip")]
        + [*(cache_root / "finra_short_interest").glob("*.csv")]
    )
    rows: list[dict[str, object]] = []
    for path in paths:
        stat = path.stat()
        relative = path.relative_to(cache_root).as_posix()
        old = prior.get(relative)
        if (
            old
            and isinstance(old.get("size"), int)
            and old["size"] == stat.st_size
            and isinstance(old.get("mtime_ns"), int)
            and old["mtime_ns"] == stat.st_mtime_ns
            and str(old.get("content_sha256") or "")
        ):
            content_sha256 = str(old["content_sha256"])
        else:
            content_sha256 = (
                _zip_content_digest(path) if path.suffix.lower() == ".zip" else _sha256(path)
            )
        rows.append(
            {
                "path": relative,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "content_sha256": content_sha256,
            }
        )
    if not any(str(row["path"]).startswith("sec_13f/") for row in rows) or not any(
        str(row["path"]).startswith("finra_short_interest/") for row in rows
    ):
        raise FileNotFoundError(
            f"Sealed neutral positioning caches are incomplete under {cache_root}"
        )
    return rows


def _fingerprint(
    *,
    universe_csv: Path,
    form13f_start: date,
    short_start: date,
    cache_files: list[dict[str, object]],
) -> str:
    evidence = {
        "universe_sha256": _sha256(universe_csv),
        "form13f_start": form13f_start.isoformat(),
        "short_start": short_start.isoformat(),
        "cache_files": [
            {
                "path": row["path"],
                "size": row["size"],
                "content_sha256": row["content_sha256"],
            }
            for row in cache_files
        ],
    }
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_universe(
    path: Path,
    *,
    as_of: date,
) -> tuple[list[dict[str, str]], set[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        all_rows = list(csv.DictReader(handle))
    rows = [
        row
        for row in all_rows
        if str(row.get("membership_start_date") or "0001-01-01") <= as_of.isoformat()
        and str(row.get("membership_end_date") or "9999-12-31") >= as_of.isoformat()
    ]
    tickers = {str(row.get("ticker") or "").strip().upper() for row in rows}
    tickers.discard("")
    if not rows or not tickers:
        raise RuntimeError(f"Consumer positioning universe is empty: {path}")
    return rows, tickers


def _write_scope(
    path: Path,
    rows: list[dict[str, str]],
    tickers: set[str],
) -> None:
    selected = [
        row for row in rows if str(row.get("ticker") or "").strip().upper() in tickers
    ]
    if not selected:
        raise RuntimeError("Consumer positioning rematch scope is empty")
    fieldnames = list(rows[0])
    with atomic_text_writer(path, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(selected)


def _covered(
    conn,
    *,
    table: str,
    date_column: str,
    source: str,
    floor: str,
    as_of: str,
) -> set[str]:
    return {
        str(row[0]).strip().upper()
        for row in conn.execute(
            f"""SELECT DISTINCT ticker FROM {table}
                 WHERE source=? AND {date_column}>=? AND {date_column}<=?""",
            (source, floor, as_of),
        )
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Accepted for refresh argv parity; the Consumer DB is not mutated.",
    )
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    upstream_db = resolve_path(
        cfg_get(bundle.payload, "positioning.market_positioning_upstream_db"),
        base_dir=bundle.base_dir,
    ).resolve()
    universe_csv = resolve_path(
        cfg_get(bundle.payload, "positioning.upstream_universe_csv"),
        base_dir=bundle.base_dir,
    ).resolve()
    cache_root = resolve_path(
        cfg_get(bundle.payload, "positioning.market_positioning_cache_root"),
        base_dir=bundle.base_dir,
    ).resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(bundle.payload, "paths.output_dir"),
            base_dir=bundle.base_dir,
        ).resolve()
        / "stage5"
        / args.as_of
    )
    if not universe_csv.is_file():
        raise FileNotFoundError(
            "Consumer positioning handoff is missing; run Stage 09a first: "
            f"{universe_csv}"
        )
    form13f_start = date.fromisoformat(
        str(cfg_get(bundle.payload, "positioning.source_birthdates.institutional_13f"))
    )
    short_start = date.fromisoformat(
        str(cfg_get(bundle.payload, "positioning.source_birthdates.short_interest"))
    )
    as_of = date.fromisoformat(args.as_of)
    state_path = universe_csv.parent / STATE_FILENAME
    prior_state = _load_state(state_path)
    cache_files = _cache_inventory(cache_root, prior_state)
    fingerprint = _fingerprint(
        universe_csv=universe_csv,
        form13f_start=form13f_start,
        short_start=short_start,
        cache_files=cache_files,
    )
    prior_files = {
        str(row.get("path")): str(row.get("content_sha256") or "")
        for row in _state_cache_rows(prior_state)
        if isinstance(row, dict)
    }
    current_files = {
        str(row["path"]): str(row["content_sha256"]) for row in cache_files
    }
    removed_files = sorted(set(prior_files) - set(current_files))
    if removed_files:
        raise RuntimeError(
            "Sealed positioning cache files were removed: " + ", ".join(removed_files[:10])
        )
    changed_sec_paths = [
        cache_root / path
        for path, digest in current_files.items()
        if path.startswith("sec_13f/") and prior_files.get(path) != digest
    ]
    changed_finra = any(
        path.startswith("finra_short_interest/") and prior_files.get(path) != digest
        for path, digest in current_files.items()
    )
    universe_rows, tickers = _read_universe(universe_csv, as_of=as_of)
    source_13f = str(
        cfg_get(bundle.payload, "positioning.upstream_source_names.institutional_13f")
    )
    source_short = str(
        cfg_get(bundle.payload, "positioning.upstream_source_names.short_interest")
    )
    max_13f_age = int(
        cfg_get(bundle.payload, "positioning.maximum_age_days.institutional_13f")
    )
    max_short_age = int(
        cfg_get(bundle.payload, "positioning.maximum_age_days.short_interest")
    )
    floor_13f = date.fromordinal(as_of.toordinal() - max_13f_age).isoformat()
    floor_short = date.fromordinal(as_of.toordinal() - max_short_age).isoformat()
    actions: list[str] = []
    rematch_tickers: set[str] = set()
    short_result = None
    form13f_result = None
    with connect(upstream_db) as conn:
        init_db(conn)
        covered_13f = _covered(
            conn,
            table="institutional_13f_ownership_snapshots",
            date_column="asof_date",
            source=source_13f,
            floor=floor_13f,
            as_of=args.as_of,
        )
        covered_short = _covered(
            conn,
            table="short_interest_snapshots",
            date_column="publication_date",
            source=source_short,
            floor=floor_short,
            as_of=args.as_of,
        )
        missing_13f = tickers - covered_13f
        missing_short = tickers - covered_short

        raw_13f = {
            str(row[0]).strip().upper()
            for row in conn.execute(
                "SELECT DISTINCT ticker FROM institutional_13f_holdings"
                " WHERE ticker IN ("
                + ",".join("?" for _ in missing_13f)
                + ")",
                tuple(sorted(missing_13f)),
            )
        } if missing_13f else set()
        if raw_13f:
            aggregate_13f_ownership_for_tickers(conn, raw_13f, source=source_13f)
            actions.append("aggregate_existing_13f_holdings")
            covered_13f = _covered(
                conn,
                table="institutional_13f_ownership_snapshots",
                date_column="asof_date",
                source=source_13f,
                floor=floor_13f,
                as_of=args.as_of,
            )
            missing_13f = tickers - covered_13f

        fingerprint_changed = bool(
            prior_state and prior_state.get("fingerprint_sha256") != fingerprint
        )
        if fingerprint_changed and changed_sec_paths:
            rematch_tickers.update(tickers)
        else:
            rematch_tickers.update(missing_13f)
        if rematch_tickers:
            scope_csv = output_dir / "positioning_rematch_scope.csv"
            _write_scope(scope_csv, universe_rows, rematch_tickers)
            archives = (
                changed_sec_paths
                if fingerprint_changed and changed_sec_paths
                else sorted((cache_root / "sec_13f").glob("*_form13f.zip"))
            )
            form13f_result = sync_sec_13f_data_sets(
                conn,
                tickers_csv=scope_csv,
                cusip_ticker_map_csv=scope_csv,
                history_start_date=form13f_start,
                end_date=as_of,
                cache_dir=cache_root / "sec_13f",
                sleep_sec=0.0,
                force_reprocess_archives=True,
                cache_only=True,
                archive_paths=archives,
            )
            actions.append("rematch_changed_13f_archives")

        if missing_short or (fingerprint_changed and changed_finra):
            short_result = sync_finra_equity_short_interest_files(
                conn,
                tickers_csv=universe_csv,
                history_start_date=short_start,
                end_date=as_of,
                cache_dir=cache_root / "finra_short_interest",
                sleep_sec=0.0,
                cache_only=True,
            )
            actions.append("rematch_finra_cache")

        final_13f = _covered(
            conn,
            table="institutional_13f_ownership_snapshots",
            date_column="asof_date",
            source=source_13f,
            floor=floor_13f,
            as_of=args.as_of,
        )
        final_short = _covered(
            conn,
            table="short_interest_snapshots",
            date_column="publication_date",
            source=source_short,
            floor=floor_short,
            as_of=args.as_of,
        )
    missing_final_13f = sorted(tickers - final_13f)
    missing_final_short = sorted(tickers - final_short)
    if missing_final_13f or missing_final_short:
        raise RuntimeError(
            "Consumer positioning cache rematch remains incomplete: "
            f"13f={missing_final_13f} short={missing_final_short}"
        )

    state = {
        "schema_version": "consumer_defensive_positioning_rematch_state_v1",
        "status": "PASS",
        "as_of": args.as_of,
        "database": str(upstream_db),
        "universe_csv": str(universe_csv),
        "universe_sha256": _sha256(universe_csv),
        "fingerprint_sha256": fingerprint,
        "cache_files": cache_files,
        "coverage": {
            "expected": len(tickers),
            "institutional_13f": len(final_13f & tickers),
            "short_interest": len(final_short & tickers),
        },
    }
    write_json(state_path, state)

    payload = {
        "status": "PASS",
        "as_of": args.as_of,
        "network_access": "forbidden",
        "database": str(upstream_db),
        "universe_csv": str(universe_csv),
        "cache_root": str(cache_root),
        "state_path": str(state_path),
        "fingerprint_sha256": fingerprint,
        "actions": actions or ["verified_current_no_rematch"],
        "rematch_tickers": sorted(rematch_tickers),
        "coverage": state["coverage"],
        "short_interest": None
        if short_result is None
        else {"rows": short_result.rows, "message": short_result.message},
        "institutional_13f": None
        if form13f_result is None
        else {"rows": form13f_result.rows, "message": form13f_result.message},
    }
    report = output_dir / "positioning_cache_rematch.json"
    write_json(report, payload)
    print(json.dumps({**payload, "report": str(report)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
